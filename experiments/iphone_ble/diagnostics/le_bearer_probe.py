#!/usr/bin/env python3
"""Read-only, privacy-preserving probe for one configured Bluetooth peer.

``snapshot`` reads the kernel connection table and a small allowlist of BlueZ
properties.  ``monitor`` additionally observes target-only HCI metadata and
allowlisted D-Bus traffic for a bounded time.  Neither command connects,
disconnects, scans, pairs, registers GATT, or changes adapter state.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import datetime as dt
import fcntl
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Iterable

sys.dont_write_bytecode = True

AF_BLUETOOTH, BTPROTO_HCI = 31, 1
HCI_CHANNEL_RAW, HCI_CHANNEL_MONITOR = 0, 2
HCIGETCONNLIST = 0x800448D4
ACL_LINK, LE_LINK = 0x01, 0x80
ADDRESS = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
MAX_MONITOR_LINE = 1024 * 1024
DEVICE_PROPERTIES = frozenset(
    ("Connected", "Paired", "Bonded", "Trusted", "ServicesResolved")
)
ADAPTER_PROPERTIES = frozenset(("Powered",))
RESOURCE_METHODS = frozenset(
    (
        "RegisterApplication",
        "UnregisterApplication",
        "RegisterAdvertisement",
        "UnregisterAdvertisement",
    )
)


class ProbeError(RuntimeError):
    """An operational failure whose detail is safe to report."""


def redact(value: object) -> str:
    """Remove addresses, D-Bus device paths, and unique bus names from text."""
    text = str(value)
    text = ADDRESS.sub("<address>", text)
    text = re.sub(r"/(?:home|Users)/[^\s\"']+", "<personal-path>", text)
    text = re.sub(r"/org/bluez/[^\s\"']*/dev_[^\s\"']+", "<device-path>", text)
    return re.sub(r"(?<![\w:]):\d+(?:\.\d+)*(?![\w.])", "<bus-sender>", text)


def load_target(path: str) -> bytes:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise ProbeError("cannot read address file") from error
    if not ADDRESS.fullmatch(text):
        raise ProbeError("address file does not contain one Bluetooth address")
    return bytes.fromhex(text.replace(":", ""))[::-1]


def device_path(adapter: str, raw_address: bytes) -> str:
    return f"/org/bluez/{adapter}/dev_" + "_".join(
        f"{item:02X}" for item in raw_address[::-1]
    )


def open_hci(channel: int) -> socket.socket:
    sock = socket.socket(
        AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI
    )
    try:
        address = ctypes.create_string_buffer(
            struct.pack("=HHH", AF_BLUETOOTH, 0xFFFF, channel)
        )
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.bind(sock.fileno(), address, 6):
            raise OSError(ctypes.get_errno(), "cannot bind HCI channel")
        # Monitor/control channels report every controller; filter in userspace.
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def connection_snapshot(
    raw_address: bytes, adapter_index: int
) -> list[dict[str, object]]:
    if not 0 <= adapter_index < 0xFFFF:
        raise ProbeError("adapter index is out of range")
    buffer = bytearray(4 + 16 * 512)
    struct.pack_into("HH", buffer, 0, adapter_index, 512)
    with socket.socket(
        AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_CLOEXEC, BTPROTO_HCI
    ) as sock:
        fcntl.ioctl(sock, HCIGETCONNLIST, buffer, True)
    device, count = struct.unpack_from("HH", buffer)
    if device != adapter_index or count >= 512:
        raise ProbeError("kernel returned an incomplete connection snapshot")
    rows: list[dict[str, object]] = []
    for offset in range(4, 4 + count * 16, 16):
        entry = buffer[offset : offset + 16]
        if (
            len(entry) != 16
            or entry[8] not in (ACL_LINK, LE_LINK)
            or entry[2:8] != raw_address
        ):
            continue
        transport = "LE" if entry[8] == LE_LINK else "BR/EDR"
        state = struct.unpack_from("H", entry, 10)[0]
        mode = struct.unpack_from("I", entry, 12)[0]
        rows.append(
            {
                "handle": struct.unpack_from("H", entry)[0],
                "transport": transport,
                "connected": state == 1,
                "encrypted": bool(mode & 4),
            }
        )
    return rows


def parse_hci(
    packet: bytes, adapter_index: int, raw_address: bytes, handles: set[int]
) -> list[dict[str, object]]:
    """Return privacy-safe metadata for target links only."""
    if len(packet) < 6:
        return []
    kind, index, length = struct.unpack_from("<HHH", packet)
    body = packet[6:]
    if index != adapter_index or len(body) != length:
        return []
    events: list[dict[str, object]] = []
    if kind == 3 and len(body) >= 2:  # HCI event
        event, payload = body[0], body[2:]
        if body[1] != len(payload):
            return []
        if event == 0x03 and len(payload) >= 11 and payload[0] == 0:
            handle = struct.unpack_from("<H", payload, 1)[0] & 0x0FFF
            if payload[3:9] == raw_address and payload[9] == ACL_LINK:
                handles.add(handle)
                events.append(
                    {
                        "event": "classic_connection_complete",
                        "handle": handle,
                        "status": payload[0],
                    }
                )
            else:
                handles.discard(handle)
        # 0x29 is deliberately not parsed: its layout must be verified against
        # the controller's Enhanced Connection Complete v2 definition before
        # it can safely identify an address or role.
        elif event == 0x3E and payload and payload[0] in (0x01, 0x0A):
            if len(payload) != (19 if payload[0] == 0x01 else 31) or payload[1] != 0:
                return []
            handle = struct.unpack_from("<H", payload, 2)[0] & 0x0FFF
            if payload[6:12] == raw_address:
                handles.add(handle)
                events.append(
                    {
                        "event": "le_connection_complete",
                        "handle": handle,
                        "status": payload[1],
                        "pc_role": "central" if payload[4] == 0 else "peripheral",
                    }
                )
            else:
                handles.discard(handle)
        elif event == 0x05 and len(payload) == 4 and payload[0] == 0:
            handle = struct.unpack_from("<H", payload, 1)[0] & 0x0FFF
            if handle in handles:
                events.append(
                    {"event": "disconnection", "handle": handle, "reason": payload[3]}
                )
                handles.discard(handle)
        elif event == 0x08 and len(payload) >= 4:
            handle = struct.unpack_from("<H", payload, 1)[0] & 0x0FFF
            if handle in handles:
                events.append(
                    {
                        "event": "encryption_change",
                        "handle": handle,
                        "status": payload[0],
                        "enabled": bool(payload[3]),
                    }
                )
    elif kind == 2 and len(body) >= 3:  # host command
        opcode, payload = struct.unpack_from("<H", body)[0], body[3:]
        if body[2] != len(payload):
            return []
        if opcode == 0x0406 and len(payload) >= 3:
            handle = struct.unpack_from("<H", payload)[0] & 0x0FFF
            if handle in handles:
                events.append(
                    {
                        "event": "pc_disconnect_command",
                        "handle": handle,
                        "reason": payload[2],
                    }
                )
        elif opcode == 0x0C03:  # HCI Reset invalidates every controller handle.
            handles.clear()
            events.append({"event": "pc_hci_reset"})
        elif (
            opcode in (0x0405, 0x0409)
            and len(payload) >= 6
            and payload[:6] == raw_address
        ):
            events.append(
                {
                    "event": "pc_classic_connect_command",
                    "opcode": opcode,
                    "operation": "initiate" if opcode == 0x0405 else "accept_inbound",
                }
            )
        elif (
            opcode == 0x200D
            and len(payload) == 25
            and payload[4] == 0
            and payload[6:12] == raw_address
        ):
            events.append({"event": "pc_le_connect_command", "opcode": opcode})
        elif (
            opcode == 0x2043
            and len(payload) >= 26
            and payload[0] == 0
            and payload[3:9] == raw_address
        ):
            events.append({"event": "pc_le_connect_command", "opcode": opcode})
        elif opcode in (0x200A, 0x2039) and payload:
            events.append(
                {"event": "pc_advertising_command", "enabled": bool(payload[0])}
            )
    elif kind in (4, 5) and len(body) >= 9:  # ACL only (not SCO); retain no value bytes
        packed_handle, acl_length, l2cap_length, cid = struct.unpack_from("<HHHH", body)
        handle = packed_handle & 0x0FFF
        # Only complete, unfragmented ATT PDUs. Continuations have no L2CAP header.
        if (
            ((packed_handle >> 12) & 3) == 1
            or acl_length != len(body) - 4
            or l2cap_length != len(body) - 8
        ):
            return []
        if handle in handles and cid == 4:
            att = body[8:]
            if att:
                events.append(
                    {
                        "event": "att_metadata",
                        "handle": handle,
                        "direction": "pc_to_peer" if kind == 4 else "peer_to_pc",
                        "opcode": att[0],
                        "length": len(att),
                    }
                )
    return events


def safe_properties(
    path: str, interface: str, changed: object, target_path: str, adapter_path: str
) -> dict[str, object] | None:
    if not isinstance(changed, dict):
        return None
    allowed = (
        DEVICE_PROPERTIES
        if path == target_path
        else ADAPTER_PROPERTIES
        if path == adapter_path
        else frozenset()
    )
    values = {
        key: unwrap(value)
        for key, value in changed.items()
        if key in allowed and isinstance(unwrap(value), bool)
    }
    return (
        {
            "event": "properties_changed",
            "scope": "target" if path == target_path else "adapter",
            "interface": interface,
            "properties": values,
        }
        if values
        else None
    )


def unwrap(value: object) -> object:
    return (
        value.get("data")
        if isinstance(value, dict) and set(value) >= {"data"}
        else value
    )


def dbus_event(
    message: object, target_path: str, adapter_path: str
) -> tuple[dict[str, object], str | None] | None:
    if not isinstance(message, dict):
        return None
    path, member, interface = (
        message.get("path"),
        message.get("member"),
        message.get("interface"),
    )
    sender = message.get("sender") if isinstance(message.get("sender"), str) else None
    data = (
        message.get("payload", {}).get("data", [])
        if isinstance(message.get("payload"), dict)
        else []
    )
    if (
        path == "/org/freedesktop/DBus"
        and interface == "org.freedesktop.DBus"
        and member == "NameOwnerChanged"
        and isinstance(data, list)
        and data
        and data[0] == "org.bluez"
    ):
        return {"event": "bluez_owner_changed"}, None
    if (
        path == target_path
        and interface
        in {"org.bluez.Device1", "org.bluez.Bearer.LE1", "org.bluez.Bearer.BREDR1"}
        and member in {"Connect", "Disconnect", "Pair"}
    ):
        return {
            "event": "target_method_call",
            "interface": interface,
            "member": member,
        }, sender
    if (
        path == target_path
        and interface == "org.freedesktop.DBus.Properties"
        and member == "Set"
        and isinstance(data, list)
        and len(data) == 3
        and data[0] == "org.bluez.Device1"
    ):
        name, value = data[1], unwrap(data[2])
        if (name == "Trusted" and isinstance(value, bool)) or (
            name == "PreferredBearer"
            and isinstance(value, str)
            and value in {"last-used", "le", "bredr"}
        ):
            return {
                "event": "target_method_call",
                "interface": interface,
                "member": member,
                "property": name,
                "value": value,
            }, sender
    if (
        path in (target_path, adapter_path)
        and member == "PropertiesChanged"
        and isinstance(data, list)
        and len(data) >= 2
        and isinstance(data[0], str)
    ):
        event = safe_properties(path, data[0], data[1], target_path, adapter_path)
        return (event, sender) if event else None
    if (
        path == adapter_path
        and interface in {"org.bluez.GattManager1", "org.bluez.LEAdvertisingManager1"}
        and member in RESOURCE_METHODS
    ):
        return {
            "event": "resource_method_call",
            "interface": interface,
            "member": member,
        }, sender
    return None


async def caller_attribution(sender: str | None) -> dict[str, object]:
    if not sender:
        return {}
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            "busctl",
            "--system",
            "call",
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "GetConnectionUnixProcessID",
            "s",
            sender,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(process.communicate(), 3)
        if process.returncode:
            return {"caller": "unavailable"}
        match = re.search(rb"\bu\s+(\d+)", out)
        if not match:
            return {"caller": "unavailable"}
        pid = int(match.group(1))
        comm = (
            Path(f"/proc/{pid}/comm")
            .read_text(encoding="utf-8", errors="replace")
            .strip()
        )
        return {"caller_pid": pid, "caller_comm": redact(comm)[:80]}
    except (OSError, asyncio.TimeoutError):
        return {"caller": "unavailable"}
    finally:
        if process and process.returncode is None:
            await terminate(process)


class Emitter:
    def __init__(self, output: str | None):
        self.stream = (
            open(output, "a", encoding="utf-8", buffering=1) if output else sys.stdout
        )

    def emit(self, event: str, **values: object) -> None:
        record = {
            "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "monotonic": round(time.monotonic(), 3),
            "event": event,
            **values,
        }
        self.stream.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.stream.flush()

    def close(self) -> None:
        if self.stream is not sys.stdout:
            self.stream.close()


def busctl_json(arguments: Iterable[str]) -> object:
    result = subprocess.run(
        ["busctl", "--system", "--json=short", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=8,
    )
    if result.returncode:
        raise ProbeError("D-Bus query failed")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except json.JSONDecodeError as error:
        raise ProbeError("D-Bus returned invalid JSON") from error


def snapshot(raw_address: bytes, adapter: str) -> dict[str, object]:
    index = adapter_index(adapter)
    path = device_path(adapter, raw_address)
    adapter_path = f"/org/bluez/{adapter}"
    properties: dict[str, object] = {}
    for name in sorted(DEVICE_PROPERTIES):
        try:
            value = unwrap(
                busctl_json(
                    ("get-property", "org.bluez", path, "org.bluez.Device1", name)
                )
            )
            properties[name] = value if isinstance(value, bool) else None
        except ProbeError:
            properties[name] = None
    resources: dict[str, object] = {}
    for interface, name, output in (
        ("org.bluez.Adapter1", "Powered", "powered"),
        ("org.bluez.LEAdvertisingManager1", "ActiveInstances", "advertising_instances"),
    ):
        try:
            value = unwrap(
                busctl_json(
                    ("get-property", "org.bluez", adapter_path, interface, name)
                )
            )
            resources[output] = (
                value
                if isinstance(value, (bool, int)) and not isinstance(value, str)
                else None
            )
        except ProbeError:
            resources[output] = None
    try:
        uuids = unwrap(
            busctl_json(
                (
                    "get-property",
                    "org.bluez",
                    adapter_path,
                    "org.bluez.Adapter1",
                    "UUIDs",
                )
            )
        )
        resources["hid_uuid_present"] = isinstance(uuids, list) and any(
            str(item).lower().startswith("00001812-") or str(item).lower() == "1812"
            for item in uuids
        )
    except ProbeError:
        resources["hid_uuid_present"] = None
    return {
        "links": connection_snapshot(raw_address, index),
        "device": properties,
        "resources": resources,
    }


def adapter_index(adapter: str) -> int:
    match = re.fullmatch(r"hci(\d+)", adapter)
    if not match or int(match.group(1)) >= 0xFFFF:
        raise ProbeError("adapter must be named hci followed by its index")
    return int(match.group(1))


async def read_json_lines(reader: asyncio.StreamReader):
    pending = bytearray()
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            if pending:
                raise ProbeError("D-Bus monitor ended with an incomplete record")
            return
        pending.extend(chunk)
        while b"\n" in pending:
            line, _, remaining = pending.partition(b"\n")
            pending = bytearray(remaining)
            if len(line) > MAX_MONITOR_LINE:
                raise ProbeError("D-Bus monitor record exceeds safety limit")
            yield line
        if len(pending) > MAX_MONITOR_LINE:
            raise ProbeError("D-Bus monitor record exceeds safety limit")


async def terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 3)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def start_dbus_monitor() -> tuple[
    asyncio.subprocess.Process, object, asyncio.Task[bytes]
]:
    # These rules do not pin a unique owner: BlueZ traffic remains observable
    # across NameOwnerChanged without admitting unrelated system-bus traffic.
    process = await asyncio.create_subprocess_exec(
        "busctl",
        "--system",
        "--json=short",
        "--match=path_namespace='/org/bluez'",
        "--match=type='signal',path='/org/freedesktop/DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'",
        "monitor",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout and process.stderr
    return (
        process,
        read_json_lines(process.stdout).__aiter__(),
        asyncio.create_task(process.stderr.read()),
    )


async def monitor(
    raw_address: bytes, adapter: str, seconds: float, emit: Emitter
) -> None:
    index, target_path = adapter_index(adapter), device_path(adapter, raw_address)
    adapter_path = f"/org/bluez/{adapter}"
    handles = {int(row["handle"]) for row in connection_snapshot(raw_address, index)}
    hci = open_hci(HCI_CHANNEL_MONITOR)
    process = None
    stderr_task = None
    readers = []
    try:
        process, bus_iter, stderr_task = await start_dbus_monitor()
        loop = asyncio.get_running_loop()

        async def hci_events():
            while True:
                packet = await loop.sock_recv(hci, 65535)
                if not packet:
                    raise ProbeError("HCI monitor reached EOF")
                for event in parse_hci(packet, index, raw_address, handles):
                    emit.emit(**event)

        async def dbus_events():
            async for item in bus_iter:
                try:
                    message = json.loads(item)
                except json.JSONDecodeError:
                    emit.emit("monitor_parse_error")
                    continue
                selected = dbus_event(message, target_path, adapter_path)
                if selected:
                    event, sender = selected
                    # Timestamp receipt before a caller lookup; HCI reading runs
                    # independently and does not wait for this D-Bus round trip.
                    event.update(
                        time=dt.datetime.now(dt.timezone.utc).isoformat(
                            timespec="milliseconds"
                        ),
                        monotonic=round(time.monotonic(), 3),
                    )
                    if event["event"].endswith("method_call"):
                        event.update(await caller_attribution(sender))
                    emit.emit(**event)
            raise ProbeError("D-Bus monitor reached EOF")

        emit.emit("monitor_snapshot", phase="initial", **snapshot(raw_address, adapter))
        emit.emit(
            "monitor_started",
            seconds=seconds,
            identity_mapping="Only configured identity matches are attributed; unresolved RPAs and Enhanced Connection Complete v2 are not attributed",
        )
        readers = [
            asyncio.create_task(hci_events()),
            asyncio.create_task(dbus_events()),
        ]
        done, _ = await asyncio.wait(
            readers, timeout=seconds, return_when=asyncio.FIRST_EXCEPTION
        )
        for reader in done:
            reader.result()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        emit.emit("monitor_snapshot", phase="final", **snapshot(raw_address, adapter))
        emit.emit("monitor_finished")
    except BaseException as error:
        emit.emit("monitor_error", error=redact(type(error).__name__))
        raise
    finally:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        hci.close()
        if process:
            await terminate(process)
        if stderr_task:
            await stderr_task  # Never echo possibly private daemon text.


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address-file",
        required=True,
        help="file containing the target identity address",
    )
    parser.add_argument("--adapter", default="hci0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("snapshot")
    monitor_parser = subparsers.add_parser("monitor")
    monitor_parser.add_argument("--seconds", type=float, required=True)
    monitor_parser.add_argument(
        "--output", help="append sanitized JSONL here instead of stdout"
    )
    args = parser.parse_args(argv)
    emitter: Emitter | None = None
    try:
        raw_address = load_target(args.address_file)
        adapter_index(args.adapter)
        if args.command == "snapshot":
            print(
                json.dumps(
                    {"event": "snapshot", **snapshot(raw_address, args.adapter)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                flush=True,
            )
        else:
            if not 0 < args.seconds <= 3600:
                raise ProbeError("seconds must be greater than zero and at most 3600")
            emitter = Emitter(args.output)
            asyncio.run(monitor(raw_address, args.adapter, args.seconds, emitter))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ProbeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"le-bearer-probe: {redact(error)}", file=sys.stderr)
        return 1
    finally:
        if emitter:
            emitter.close()


if __name__ == "__main__":
    raise SystemExit(main())
