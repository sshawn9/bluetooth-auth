"""Narrow Linux Bluetooth MGMT control-channel helper for coexistence cleanup.

The control channel (``HCI_CHANNEL_CONTROL``), unlike the HCI user channel,
does not take ownership of an adapter.  This module intentionally sends only
MGMT Get Connections (0x0015) and Disconnect (0x0014).  It never discovers,
connects, pairs, advertises, changes adapter settings, or reads BlueZ storage.

``open(index, baseline=...)`` accepts the connections that existed before this
experiment.  Disconnect is disabled unless that baseline is supplied, and it
refuses every baseline connection.  Callers must therefore explicitly snapshot
connections before creating their experiment-owned LE link.

Wire layout is from BlueZ 5.87 ``doc/mgmt-protocol.rst``: headers and fields
are little-endian, addresses are sent least-significant octet first, Get
Connections returns ``u16 count`` followed by ``bdaddr, type`` entries, and
Disconnect uses ``bdaddr, type``.  The kernel requires CAP_NET_ADMIN to bind
the control channel.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import socket
import struct
from collections.abc import Iterable
from typing import Final

AF_BLUETOOTH: Final = 31
BTPROTO_HCI: Final = 1
HCI_DEV_NONE: Final = 0xFFFF
HCI_CHANNEL_CONTROL: Final = 3

MGMT_EV_CMD_COMPLETE: Final = 0x0001
MGMT_EV_CMD_STATUS: Final = 0x0002
MGMT_EV_DEVICE_DISCONNECTED: Final = 0x000C
MGMT_OP_DISCONNECT: Final = 0x0014
MGMT_OP_GET_CONNECTIONS: Final = 0x0015
COMMAND_TIMEOUT: Final = 5.0


class MgmtError(RuntimeError):
    """A safe MGMT operation could not be completed."""


class MgmtCapabilityError(MgmtError):
    """The process/kernel cannot open the privileged MGMT control channel."""


class MgmtCommandError(MgmtError):
    def __init__(self, opcode: int, status: int):
        self.opcode, self.status = opcode, status
        super().__init__(f"MGMT opcode 0x{opcode:04x} failed with status 0x{status:02x}")


class MgmtProtocolError(MgmtError):
    pass


def _address(address: str) -> tuple[str, bytes]:
    if not isinstance(address, str):
        raise ValueError("Bluetooth address must be a string")
    pieces = address.split(":")
    if len(pieces) != 6 or any(len(piece) != 2 for piece in pieces):
        raise ValueError("Bluetooth address must be six hexadecimal octets")
    try:
        raw = bytes(int(piece, 16) for piece in pieces)
    except ValueError as error:
        raise ValueError("Bluetooth address must be six hexadecimal octets") from error
    if any(len(piece) != 2 or not all(char in "0123456789abcdefABCDEF" for char in piece) for piece in pieces):
        raise ValueError("Bluetooth address must be six hexadecimal octets")
    return ":".join(piece.upper() for piece in pieces), raw[::-1]


def _connection(item: tuple[str, int]) -> tuple[str, int]:
    if not isinstance(item, tuple) or len(item) != 2:
        raise ValueError("baseline connections must be (address, address_type) pairs")
    address, address_type = item
    canonical, _ = _address(address)
    if type(address_type) is not int or address_type not in (0, 1, 2):
        raise ValueError("address type must be 0 (BR/EDR), 1 (LE public), or 2 (LE random)")
    return canonical, address_type


class MgmtLink:
    """One serial MGMT control socket for one explicitly selected hci index."""

    def __init__(self, raw_socket, index: int, baseline: Iterable[tuple[str, int]] | None):
        self._socket = raw_socket
        self.index = index
        self._baseline = None if baseline is None else frozenset(_connection(item) for item in baseline)
        self._lock = asyncio.Lock()
        self._waiting: tuple[int, asyncio.Future[tuple[int, int, bytes]]] | None = None
        # Passive event history for the caller's coexistence verdict.  MGMT
        # Device Disconnected has bdaddr (6), address type (1), reason (1).
        self.disconnect_events: list[dict[str, int | str]] = []
        self._closed = False
        self._reader_installed = False

    @classmethod
    async def open(cls, index: int, *, baseline: Iterable[tuple[str, int]] | None = None) -> "MgmtLink":
        if type(index) is not int or not 0 <= index < HCI_DEV_NONE:
            raise ValueError("controller index must be an integer from 0 through 65534")
        raw_socket = None
        try:
            raw_socket = socket.socket(AF_BLUETOOTH, socket.SOCK_RAW | socket.SOCK_NONBLOCK, BTPROTO_HCI)
            libc = ctypes.CDLL(None, use_errno=True)
            libc.bind.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_char), ctypes.c_int)
            libc.bind.restype = ctypes.c_int
            address = struct.pack("<HHH", AF_BLUETOOTH, HCI_DEV_NONE, HCI_CHANNEL_CONTROL)
            if libc.bind(raw_socket.fileno(), ctypes.create_string_buffer(address), len(address)):
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number))
            result = cls(raw_socket, index, baseline)
            result._install_reader()
            return result
        except (OSError, AttributeError) as error:
            if raw_socket is not None:
                raw_socket.close()
            raise MgmtCapabilityError(
                "cannot open Bluetooth MGMT control channel (Linux and CAP_NET_ADMIN are required)"
            ) from error
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
            except OSError as error:
                self._terminate(MgmtError(f"MGMT control socket read failed: {error}"))
                return
            if not packet:
                self._terminate(MgmtError("MGMT control socket closed by kernel"))
                return
            self._receive(packet)

    def _receive(self, packet: bytes) -> None:
        """Consume one packet; tests use this with a fake non-Bluetooth socket."""
        if len(packet) < 6:
            return
        event, index, length = struct.unpack_from("<HHH", packet)
        if len(packet) != 6 + length or index != self.index:
            return  # unrelated asynchronous MGMT traffic is deliberately discarded
        body = packet[6:]
        if event == MGMT_EV_DEVICE_DISCONNECTED:
            if len(body) == 8 and body[6] in (0, 1, 2):
                address = ":".join(f"{octet:02X}" for octet in body[:6][::-1])
                self.disconnect_events.append({"address": address, "address_type": body[6], "reason": body[7]})
            return
        if event not in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS):
            return  # all other asynchronous MGMT traffic is deliberately discarded
        if len(body) < 3:
            return
        opcode, status = struct.unpack_from("<HB", body)
        waiting = self._waiting
        if waiting is not None and waiting[0] == opcode and not waiting[1].done():
            waiting[1].set_result((event, status, body[3:]))

    def _fail_waiter(self, error: BaseException) -> None:
        if self._waiting is not None and not self._waiting[1].done():
            self._waiting[1].set_exception(error)

    def _terminate(self, error: BaseException | None = None) -> None:
        """Synchronously make this one-shot channel unusable and release its fd."""
        if self._closed:
            return
        self._closed = True
        if error is not None:
            self._fail_waiter(error)
        if self._reader_installed:
            try:
                asyncio.get_running_loop().remove_reader(self._socket.fileno())
            except (OSError, ValueError):
                pass
            self._reader_installed = False
        self._socket.close()

    async def _command(self, opcode: int, parameters: bytes = b"") -> bytes:
        async with self._lock:
            if self._closed:
                raise MgmtError("MGMT control channel is closed")
            response = asyncio.get_running_loop().create_future()
            self._waiting = (opcode, response)
            try:
                packet = struct.pack("<HHH", opcode, self.index, len(parameters)) + parameters
                try:
                    written = self._socket.send(packet)
                except OSError as error:
                    response.cancel()
                    await self.close()
                    raise MgmtError(f"MGMT control socket write failed: {error}") from error
                if written != len(packet):
                    response.cancel()
                    await self.close()
                    raise MgmtError("MGMT control socket did not accept a complete command")
                event, status, data = await asyncio.wait_for(asyncio.shield(response), COMMAND_TIMEOUT)
            except asyncio.TimeoutError as error:
                response.cancel()
                await self.close()
                raise MgmtError(f"MGMT opcode 0x{opcode:04x} timed out after {COMMAND_TIMEOUT:g} seconds") from error
            except asyncio.CancelledError:
                # MGMT has no request id.  Closing prevents a late reply from being
                # mistaken for the next same-opcode request after cancellation.
                response.cancel()
                await self.close()
                raise
            finally:
                if self._waiting is not None and self._waiting[1] is response:
                    self._waiting = None
            if event == MGMT_EV_CMD_STATUS:
                if status == 0:
                    await self.close()
                    raise MgmtProtocolError(f"MGMT opcode 0x{opcode:04x} reported unexpected successful Command Status")
                raise MgmtCommandError(opcode, status)
            if status:
                raise MgmtCommandError(opcode, status)
            return data

    async def connections(self) -> set[tuple[str, int]]:
        data = await self._command(MGMT_OP_GET_CONNECTIONS)
        if len(data) < 2:
            raise MgmtProtocolError("Get Connections response lacks a connection count")
        count = struct.unpack_from("<H", data)[0]
        if len(data) != 2 + count * 7:
            raise MgmtProtocolError("Get Connections response has an invalid length")
        result = set()
        for offset in range(2, len(data), 7):
            address = ":".join(f"{octet:02X}" for octet in data[offset:offset + 6][::-1])
            address_type = data[offset + 6]
            if address_type not in (0, 1, 2):
                raise MgmtProtocolError(f"Get Connections returned invalid address type {address_type}")
            result.add((address, address_type))
        return result

    async def disconnect_le(self, address: str, address_type: int) -> None:
        canonical, wire_address = _address(address)
        if type(address_type) is not int or address_type not in (1, 2):
            raise ValueError("disconnect_le accepts only LE public (1) or LE random (2), never BR/EDR")
        if self._baseline is None:
            raise MgmtError("disconnect is disabled without an explicit pre-experiment baseline")
        target = (canonical, address_type)
        if target in self._baseline:
            raise MgmtError("refusing to disconnect a connection present in the pre-experiment baseline")
        if target not in await self.connections():
            raise MgmtError("refusing to disconnect a link that is not currently connected")
        returned = await self._command(MGMT_OP_DISCONNECT, wire_address + bytes((address_type,)))
        if len(returned) != 7 or returned != wire_address + bytes((address_type,)):
            raise MgmtProtocolError("Disconnect response did not echo the requested LE address and type")

    async def close(self) -> None:
        self._terminate(MgmtError("MGMT control channel was closed"))


async def open(index: int, *, baseline: Iterable[tuple[str, int]] | None = None) -> MgmtLink:
    """Open the MGMT control channel for ``hci<index>`` without taking it over."""
    return await MgmtLink.open(index, baseline=baseline)
