"""Read-only, privacy-minimising HCI monitor for local advertising diagnosis.

This opens Linux ``HCI_CHANNEL_MONITOR`` (2), never the user or control
channels.  Monitor records use BlueZ's monitor header (type, controller index,
length), followed by an HCI command/event packet.  Only local controller
advertising commands are decoded.  ACL, SMP, keys, addresses, unknown AD data,
and every other monitor record are discarded before parsing or retention.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import struct
from collections.abc import Callable
from typing import Any, Final

AF_BLUETOOTH: Final = 31
BTPROTO_HCI: Final = 1
HCI_DEV_NONE: Final = 0xFFFF
HCI_CHANNEL_MONITOR: Final = 2

HCI_MON_COMMAND_PKT: Final = 0x0002
HCI_MON_EVENT_PKT: Final = 0x0003
HCI_EV_COMMAND_COMPLETE: Final = 0x0E
HCI_EV_COMMAND_STATUS: Final = 0x0F

LE_SET_ADVERTISING_PARAMETERS: Final = 0x2006
LE_SET_ADVERTISING_DATA: Final = 0x2008
LE_SET_SCAN_RESPONSE_DATA: Final = 0x2009
LE_SET_ADVERTISING_ENABLE: Final = 0x200A
LE_SET_ADVERTISING_SET_RANDOM_ADDRESS: Final = 0x2035
LE_SET_EXTENDED_ADVERTISING_PARAMETERS: Final = 0x2036
LE_SET_EXTENDED_ADVERTISING_DATA: Final = 0x2037
LE_SET_EXTENDED_SCAN_RESPONSE_DATA: Final = 0x2038
LE_SET_EXTENDED_ADVERTISING_ENABLE: Final = 0x2039

_ALLOWED = frozenset((
    LE_SET_ADVERTISING_PARAMETERS, LE_SET_ADVERTISING_DATA,
    LE_SET_SCAN_RESPONSE_DATA, LE_SET_ADVERTISING_ENABLE,
    LE_SET_ADVERTISING_SET_RANDOM_ADDRESS,
    LE_SET_EXTENDED_ADVERTISING_PARAMETERS, LE_SET_EXTENDED_ADVERTISING_DATA,
    LE_SET_EXTENDED_SCAN_RESPONSE_DATA, LE_SET_EXTENDED_ADVERTISING_ENABLE,
))


class MonitorCapabilityError(RuntimeError):
    """The Linux monitor channel cannot be opened by this process."""


def _interval_ms(value: int) -> float:
    return round(value * 0.625, 3)


def _ad_summary(payload: bytes, location: str) -> dict[str, Any]:
    """Return only the explicitly safe advertising-data fields.

    AD structures with malformed length are ignored; their bytes are never
    represented in a result.  HID is recognised only from the standard 16-bit
    service UUID list types.
    """
    result: dict[str, Any] = {"location": location, "length": len(payload), "hid_service": False}
    offset = 0
    flags: list[int] = []
    appearances: list[int] = []
    names: list[str] = []
    while offset < len(payload):
        size = payload[offset]
        offset += 1
        if size == 0:
            continue
        if offset + size > len(payload):
            break
        kind = payload[offset]
        value = payload[offset + 1:offset + size]
        offset += size
        if kind == 0x01 and len(value) == 1:
            flags.append(value[0])
        elif kind in (0x02, 0x03) and len(value) % 2 == 0:
            if any(value[item:item + 2] == b"\x12\x18" for item in range(0, len(value), 2)):
                result["hid_service"] = True
        elif kind == 0x19 and len(value) == 2:
            appearances.append(struct.unpack("<H", value)[0])
        elif kind in (0x08, 0x09):
            # A local name is intentionally included, but only as text and
            # never as raw advertising bytes.
            names.append(value.decode("utf-8", "replace"))
    if flags:
        result["flags"] = flags
    if appearances:
        result["appearance"] = appearances
    if names:
        result["local_name"] = names
    return result


class CoexistTrace:
    """A passive local-controller command trace; this class has no send API."""

    def __init__(self, raw_socket, index: int, emit: Callable[[str, dict[str, Any]], None]):
        self._socket = raw_socket
        self.index = index
        self._emit = emit
        self._closed = False
        self._reader_installed = False
        self._commands: list[dict[str, Any]] = []
        self._statuses: list[dict[str, Any]] = []

    @classmethod
    async def open(cls, index: int, emit: Callable[[str, dict[str, Any]], None]) -> "CoexistTrace":
        if type(index) is not int or not 0 <= index < HCI_DEV_NONE:
            raise ValueError("controller index must be an integer from 0 through 65534")
        if not callable(emit):
            raise TypeError("emit must be callable")
        raw_socket = None
        try:
            raw_socket = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_NONBLOCK, BTPROTO_HCI)
            libc = ctypes.CDLL(None, use_errno=True)
            libc.bind.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_char), ctypes.c_int)
            libc.bind.restype = ctypes.c_int
            # A monitor socket receives all controllers; record headers carry
            # the controller index, which we strictly filter below.
            address = struct.pack("<HHH", AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_MONITOR)
            if libc.bind(raw_socket.fileno(), ctypes.create_string_buffer(address), len(address)):
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
            result = cls(raw_socket, index, emit)
            result._install_reader()
            return result
        except (OSError, AttributeError) as error:
            if raw_socket is not None:
                raw_socket.close()
            raise MonitorCapabilityError("cannot open Linux HCI monitor channel (suitable Bluetooth capability is required)") from error
        except BaseException:
            if raw_socket is not None:
                raw_socket.close()
            raise

    def _install_reader(self) -> None:
        asyncio.get_running_loop().add_reader(self._socket.fileno(), self._read_available)
        self._reader_installed = True

    def _read_available(self) -> None:
        while not self._closed:
            try:
                packet = self._socket.recv(4096)
            except BlockingIOError:
                return
            except OSError:
                self._terminate()
                return
            if not packet:
                self._terminate()
                return
            self._receive(packet)

    def _terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_installed:
            try:
                asyncio.get_running_loop().remove_reader(self._socket.fileno())
            except (OSError, ValueError):
                pass
            self._reader_installed = False
        self._socket.close()

    def _record(self, event: str, details: dict[str, Any]) -> None:
        target = self._commands if event == "advertising_command" else self._statuses
        target.append(details)
        try:
            self._emit(event, dict(details))
        except Exception:
            # A diagnostic sink must not impair the passive monitor fd.
            pass

    def _receive(self, packet: bytes) -> None:
        """Consume one BlueZ monitor record; usable by offline fake-socket tests."""
        if len(packet) < 6:
            return
        record_type, index, length = struct.unpack_from("<HHH", packet)
        if index != self.index or len(packet) != 6 + length:
            return
        hci = packet[6:]
        if record_type == HCI_MON_COMMAND_PKT:
            self._command(hci)
        elif record_type == HCI_MON_EVENT_PKT:
            self._event(hci)

    def _command(self, hci: bytes) -> None:
        if len(hci) < 3:
            return
        opcode, length = struct.unpack_from("<HB", hci)
        # This whitelist precedes every parameter/AD parse.
        if opcode not in _ALLOWED or len(hci) != 3 + length:
            return
        parameters = hci[3:]
        details = _decode_command(opcode, parameters)
        if details is not None:
            self._record("advertising_command", details)

    def _event(self, hci: bytes) -> None:
        if len(hci) < 2 or len(hci) != 2 + hci[1]:
            return
        event, parameters = hci[0], hci[2:]
        if event == HCI_EV_COMMAND_COMPLETE:
            if len(parameters) < 4:
                return
            opcode = struct.unpack_from("<H", parameters, 1)[0]
            if opcode in _ALLOWED:
                self._record("advertising_status", {"opcode": f"0x{opcode:04x}", "status": parameters[3], "event": "complete"})
        elif event == HCI_EV_COMMAND_STATUS:
            if len(parameters) != 4:
                return
            opcode = struct.unpack_from("<H", parameters, 2)[0]
            if opcode in _ALLOWED:
                self._record("advertising_status", {"opcode": f"0x{opcode:04x}", "status": parameters[0], "event": "status"})

    def summary(self) -> dict[str, Any]:
        return {"scope": "local_controller_commands", "controller_index": self.index,
                "commands": [dict(item) for item in self._commands],
                "statuses": [dict(item) for item in self._statuses], "closed": self._closed}

    async def close(self) -> None:
        self._terminate()


def _decode_command(opcode: int, p: bytes) -> dict[str, Any] | None:
    details: dict[str, Any] = {"opcode": f"0x{opcode:04x}"}
    if opcode == LE_SET_ADVERTISING_PARAMETERS and len(p) == 15:
        minimum, maximum = struct.unpack_from("<HH", p)
        details.update(handle=0, pdu="legacy", connectable=p[4] in (0, 1), scannable=p[4] in (0, 2),
                       interval_ms={"min": _interval_ms(minimum), "max": _interval_ms(maximum)},
                       channel_map=p[13], filter_policy=p[14], own_address_type=p[5])
    elif opcode in (LE_SET_ADVERTISING_DATA, LE_SET_SCAN_RESPONSE_DATA) and len(p) == 32 and p[0] <= 31:
        details.update(handle=0, pdu="legacy", data=_ad_summary(p[1:1 + p[0]], "primary" if opcode == LE_SET_ADVERTISING_DATA else "scan_response"))
    elif opcode == LE_SET_ADVERTISING_ENABLE and len(p) == 1:
        details.update(handle=0, pdu="legacy", enable=bool(p[0]))
    elif opcode == LE_SET_ADVERTISING_SET_RANDOM_ADDRESS and len(p) == 7:
        details.update(handle=p[0], configured_random_address=True)
    elif opcode == LE_SET_EXTENDED_ADVERTISING_PARAMETERS and len(p) == 25:
        properties = struct.unpack_from("<H", p, 1)[0]
        minimum = int.from_bytes(p[3:6], "little")
        maximum = int.from_bytes(p[6:9], "little")
        details.update(handle=p[0], pdu="legacy" if properties & 0x0010 else "extended",
                       connectable=bool(properties & 0x0001), scannable=bool(properties & 0x0002),
                       interval_ms={"min": _interval_ms(minimum), "max": _interval_ms(maximum)},
                       channel_map=p[9], filter_policy=p[18], own_address_type=p[10])
    elif opcode in (LE_SET_EXTENDED_ADVERTISING_DATA, LE_SET_EXTENDED_SCAN_RESPONSE_DATA) and len(p) >= 4 and p[3] == len(p) - 4:
        # The Extended HCI API can configure either an extended or a legacy PDU.
        # Only Set Extended Advertising Parameters carries the properties bit
        # that tells us which PDU is actually selected.
        details.update(handle=p[0], controller_api="extended", operation=p[1], data=_ad_summary(p[4:], "primary" if opcode == LE_SET_EXTENDED_ADVERTISING_DATA else "scan_response"))
    elif opcode == LE_SET_EXTENDED_ADVERTISING_ENABLE and len(p) >= 2 and len(p) == 2 + p[1] * 4:
        sets = [{"handle": p[offset], "duration_ms": struct.unpack_from("<H", p, offset + 1)[0] * 10,
                 "max_events": p[offset + 3]} for offset in range(2, len(p), 4)]
        details.update(controller_api="extended", enable=bool(p[0]), sets=sets)
    else:
        return None
    return details


async def open(index: int, emit: Callable[[str, dict[str, Any]], None]) -> CoexistTrace:
    """Open a passive monitor filtered to ``hci<index>`` records."""
    return await CoexistTrace.open(index, emit)
