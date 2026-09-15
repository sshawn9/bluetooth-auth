#!/usr/bin/env python3
"""Explicit, target-scoped LE/BR control recipes; no action on import/help."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).parent))

from le_bearer_probe import (
    ProbeError,
    adapter_index,
    connection_snapshot,
    device_path,
    load_target,
    open_hci,
    redact,
)

MGMT_GET_CONNECTIONS = 0x0015
MGMT_DISCONNECT = 0x0014
MGMT_UNPAIR_DEVICE = 0x001B
LE_KEY_GROUPS = (
    "LongTermKey",
    "PeripheralLongTermKey",
    "SlaveLongTermKey",
    "IdentityResolvingKey",
    "LocalSignatureKey",
    "RemoteSignatureKey",
)


class ControlError(RuntimeError):
    pass


def emit(**record):
    print(json.dumps(record, ensure_ascii=False, separators=(",", ":")), flush=True)


def fail(error):
    return redact(str(error) or "operation failed")


def busctl(*args):
    try:
        r = subprocess.run(
            ["busctl", "--system", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ControlError("D-Bus operation failed") from e
    if r.returncode:
        raise ControlError("D-Bus operation failed")
    return r.stdout.decode("utf-8", "replace")


def mgmt_request(index, opcode, payload, timeout=10.0):
    packet = struct.pack("<HHH", opcode, index, len(payload)) + payload
    deadline = time.monotonic() + timeout
    try:
        with open_hci(3) as sock:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            sock.send(packet)
            while time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                response = sock.recv(4096)
                if len(response) < 6:
                    continue
                (event, got, length) = struct.unpack_from("<HHH", response)
                body = response[6:]
                if (
                    got != index
                    or len(body) != length
                    or len(body) < 3
                    or (struct.unpack_from("<H", body)[0] != opcode)
                ):
                    continue
                status = body[2]
                if event == 2:
                    if status:
                        raise ControlError("kernel rejected target operation")
                    continue
                if event == 1:
                    if status:
                        raise ControlError("kernel rejected target operation")
                    return body[3:]
    except (OSError, TimeoutError) as e:
        raise ControlError("kernel management operation failed") from e
    raise ControlError("kernel management operation timed out")


def addr(raw, kind):
    if kind not in (0, 1, 2):
        raise ControlError("invalid Bluetooth address type")
    return raw + bytes((kind,))


def target_connections(raw, index):
    data = mgmt_request(index, MGMT_GET_CONNECTIONS, b"")
    if len(data) < 2:
        raise ControlError("kernel returned invalid connection list")
    count = struct.unpack_from("<H", data)[0]
    if len(data) != 2 + count * 7:
        raise ControlError("kernel returned invalid connection list")
    return {data[i + 6] for i in range(2, len(data), 7) if data[i : i + 6] == raw}


def reply_target(reply, raw, kind):
    if len(reply) != 7 or reply != addr(raw, kind):
        raise ControlError("kernel returned mismatched target reply")


def run_connect(adapter, raw, le):
    busctl(
        "call",
        "org.bluez",
        device_path(adapter, raw),
        "org.bluez.Bearer.LE1" if le else "org.bluez.Device1",
        "Connect",
    )
    emit(ok=True, action="connect-le" if le else "connect")


def dbus_value(adapter, raw, name, allowed):
    text = busctl(
        "get-property",
        "org.bluez",
        device_path(adapter, raw),
        "org.bluez.Device1",
        name,
    )
    value = (
        text.split('"')[1]
        if '"' in text
        else text.strip().split()[-1]
        if text.strip()
        else ""
    )
    if value not in allowed:
        raise ControlError("cannot read target property")
    return value


def run_prefer(adapter, raw, value):
    if value is None:
        emit(
            ok=True,
            action="prefer-get",
            value=dbus_value(
                adapter, raw, "PreferredBearer", {"last-used", "le", "bredr"}
            ),
        )
        return
    busctl(
        "set-property",
        "org.bluez",
        device_path(adapter, raw),
        "org.bluez.Device1",
        "PreferredBearer",
        "s",
        value,
    )
    emit(ok=True, action="prefer", value=value)


def run_trusted(adapter, raw, value):
    busctl(
        "set-property",
        "org.bluez",
        device_path(adapter, raw),
        "org.bluez.Device1",
        "Trusted",
        "b",
        str(value).lower(),
    )
    emit(ok=True, action="trusted", value=value)


def run_disconnect(raw, index, transport):
    matches = target_connections(raw, index)
    kinds = [
        k
        for k in matches
        if transport == "bredr" and k == 0 or (transport == "le" and k in (1, 2))
    ]
    if len(kinds) != 1:
        raise ControlError("target transport is absent or ambiguous")
    kind = kinds[0]
    reply = mgmt_request(index, MGMT_DISCONNECT, addr(raw, kind))
    reply_target(reply, raw, kind)
    emit(ok=True, action="disconnect", transport=transport)


def adapter_address(adapter):
    text = busctl(
        "get-property",
        "org.bluez",
        f"/org/bluez/{adapter}",
        "org.bluez.Adapter1",
        "Address",
    )
    words = text.split('"')
    if len(words) < 2 or not re.fullmatch(
        "(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", words[1]
    ):
        raise ControlError("cannot read adapter identity")
    return words[1]


def info_path(adapter, raw):
    return (
        Path("/var/lib/bluetooth")
        / adapter_address(adapter).upper()
        / ":".join((f"{x:02X}" for x in raw[::-1]))
        / "info"
    )


def record(path):
    try:
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        raise ControlError("cannot read target pairing record") from e
    p = configparser.ConfigParser(interpolation=None)
    try:
        p.read_string(data)
    except configparser.Error as e:
        raise ControlError("invalid target pairing record") from e
    return p


def le_groups(p):
    return {
        n: hashlib.sha256(
            "\n".join((f"{k}={v}" for (k, v) in sorted(p.items(n)))).encode()
        ).digest()
        for n in LE_KEY_GROUPS
        if p.has_section(n)
    }


def key_state(path, require_link=True):
    p = record(path)
    if require_link and (not p.has_section("LinkKey")):
        raise ControlError("target has no classic link key")
    groups = le_groups(p)
    if (
        not any((n in groups for n in LE_KEY_GROUPS[:3]))
        or "IdentityResolvingKey" not in groups
    ):
        raise ControlError("target lacks required LE key sections")
    link = (
        hashlib.sha256(
            "\n".join((f"{k}={v}" for (k, v) in sorted(p.items("LinkKey")))).encode()
        ).digest()
        if p.has_section("LinkKey")
        else None
    )
    return (link, groups)


def bond_info(adapter, raw):
    p = record(info_path(adapter, raw))
    pref = p.get("General", "PreferredBearer", fallback=None)
    trust = p.get("General", "Trusted", fallback=None)
    if pref not in {None, "last-used", "last-seen", "le", "bredr"}:
        pref = None
    if trust not in {"true", "false"}:
        trust = None
    emit(
        ok=True,
        action="bond-info",
        has_classic_key=p.has_section("LinkKey"),
        has_le_ltk=any((p.has_section(x) for x in LE_KEY_GROUPS[:3])),
        has_irk=p.has_section("IdentityResolvingKey"),
        preferred_bearer=pref,
        trusted=None if trust is None else trust == "true",
    )


def backup(path, data):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 384)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
    except OSError as e:
        raise ControlError("cannot create private backup") from e


def remove_classic_pairing(adapter, raw, index, backup_file):
    if 0 in target_connections(raw, index):
        raise ControlError("classic transport must already be disconnected")
    if not any(
        (
            x["transport"] == "LE" and x["connected"] and x["encrypted"]
            for x in connection_snapshot(raw, index)
        )
    ):
        raise ControlError("an encrypted LE link is required")
    path = info_path(adapter, raw)
    try:
        original = path.read_bytes()
    except OSError as e:
        raise ControlError("cannot read target pairing record") from e
    before = key_state(path)
    backup(backup_file, original)
    try:
        reply = mgmt_request(index, MGMT_UNPAIR_DEVICE, addr(raw, 0) + b"\x00")
        reply_target(reply, raw, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            after = key_state(path, False)
            if after[1] != before[1]:
                raise ControlError(
                    "LE key verification failed; private backup retained"
                )
            if after[0] is None:
                try:
                    os.unlink(backup_file)
                except OSError as e:
                    raise ControlError(
                        "verification passed but private backup retained"
                    ) from e
                emit(ok=True, action="remove-classic-pairing", le_keys_unchanged=True)
                return
            time.sleep(0.1)
        raise ControlError("verification failed; private backup retained")
    except ControlError as e:
        if "private backup retained" in str(e):
            raise
        raise ControlError("operation failed; private backup retained") from e


def boolean(v):
    if v not in ("true", "false"):
        raise argparse.ArgumentTypeError("value must be true or false")
    return v == "true"


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--address-file", required=True)
    p.add_argument("--adapter", default="hci0")
    s = p.add_subparsers(dest="command", required=True)
    s.add_parser("connect")
    s.add_parser("connect-le")
    x = s.add_parser("prefer")
    x.add_argument("--value", choices=("last-used", "le", "bredr"))
    x = s.add_parser("trusted")
    x.add_argument("--value", required=True, type=boolean)
    x = s.add_parser("disconnect")
    x.add_argument("--transport", required=True, choices=("le", "bredr"))
    x = s.add_parser("remove-classic-pairing")
    x.add_argument("--backup-file", required=True)
    s.add_parser("bond-info")
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        (raw, index) = (load_target(a.address_file), adapter_index(a.adapter))
        if a.command == "connect":
            run_connect(a.adapter, raw, False)
        elif a.command == "connect-le":
            run_connect(a.adapter, raw, True)
        elif a.command == "prefer":
            run_prefer(a.adapter, raw, a.value)
        elif a.command == "trusted":
            run_trusted(a.adapter, raw, a.value)
        elif a.command == "disconnect":
            run_disconnect(raw, index, a.transport)
        elif a.command == "bond-info":
            bond_info(a.adapter, raw)
        else:
            remove_classic_pairing(a.adapter, raw, index, a.backup_file)
        return 0
    except (ControlError, ProbeError, ValueError) as e:
        emit(ok=False, error=fail(e))
        return 1
    except OSError:
        emit(ok=False, error="system access failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
