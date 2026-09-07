from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coexist_link as link


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False
        self.send_error = None
        self.send_result = None
        self.recv_error = None
        self.recv_value = None

    def send(self, packet):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(packet)
        return len(packet) if self.send_result is None else self.send_result

    def recv(self, _size):
        if self.recv_error is not None:
            raise self.recv_error
        if self.recv_value is not None:
            return self.recv_value
        raise BlockingIOError

    def close(self):
        self.closed = True


def event(code, index, opcode, status=0, data=b""):
    body = struct.pack("<HB", opcode, status) + data
    return struct.pack("<HHH", code, index, len(body)) + body


class CoexistLinkTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, *, baseline=()):
        return link.MgmtLink(FakeSocket(), 2, baseline)

    async def test_connections_serializes_little_endian_and_reverses_address(self):
        backend = self.backend()
        operation = asyncio.create_task(backend.connections())
        await asyncio.sleep(0)
        self.assertEqual(backend._socket.sent, [struct.pack("<HHH", 0x0015, 2, 0)])
        data = struct.pack("<H", 2) + bytes.fromhex("FFEEDDCCBBAA01") + bytes.fromhex("66554433221102")
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 2, 0x0015, data=data))
        self.assertEqual(await operation, {("AA:BB:CC:DD:EE:FF", 1), ("11:22:33:44:55:66", 2)})

    async def test_unrelated_events_do_not_complete_concurrent_requests(self):
        backend = self.backend()
        first = asyncio.create_task(backend.connections())
        second = asyncio.create_task(backend.connections())
        await asyncio.sleep(0)
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 9, 0x0015, data=struct.pack("<H", 0)))
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 2, 0x0014, data=b"x" * 7))
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 2, 0x0015, data=struct.pack("<H", 0)))
        self.assertEqual(await first, set())
        await asyncio.sleep(0)
        self.assertEqual(len(backend._socket.sent), 2)
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 2, 0x0015, data=struct.pack("<H", 0)))
        self.assertEqual(await second, set())

    async def test_disconnect_rejects_classic_and_preexisting_link_without_sending(self):
        backend = self.backend(baseline={("AA:BB:CC:DD:EE:FF", 1)})
        with self.assertRaises(ValueError):
            await backend.disconnect_le("AA:BB:CC:DD:EE:FF", 0)
        with self.assertRaises(link.MgmtError):
            await backend.disconnect_le("AA:BB:CC:DD:EE:FF", 1)
        self.assertEqual(backend._socket.sent, [])

    async def test_disconnect_checks_new_link_then_exactly_serializes_disconnect(self):
        backend = self.backend(baseline=set())
        operation = asyncio.create_task(backend.disconnect_le("AA:BB:CC:DD:EE:FF", 2))
        await asyncio.sleep(0)
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 2, 0x0015, data=struct.pack("<H", 1) + bytes.fromhex("FFEEDDCCBBAA02")))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(backend._socket.sent[-1], struct.pack("<HHH", 0x0014, 2, 7) + bytes.fromhex("FFEEDDCCBBAA02"))
        backend._receive(event(link.MGMT_EV_CMD_COMPLETE, 2, 0x0014, data=bytes.fromhex("FFEEDDCCBBAA02")))
        await operation

    async def test_records_device_disconnected_event_without_completing_command(self):
        backend = self.backend()
        operation = asyncio.create_task(backend.connections())
        await asyncio.sleep(0)
        body = bytes.fromhex("FFEEDDCCBBAA02") + b"\x16"
        backend._receive(struct.pack("<HHH", link.MGMT_EV_DEVICE_DISCONNECTED, 2, len(body)) + body)
        self.assertEqual(backend.disconnect_events, [{"address": "AA:BB:CC:DD:EE:FF", "address_type": 2, "reason": 0x16}])
        self.assertFalse(operation.done())
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation

    async def test_cancellation_closes_socket_and_releases_lock(self):
        backend = self.backend()
        operation = asyncio.create_task(backend.connections())
        await asyncio.sleep(0)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertTrue(backend._socket.closed)
        self.assertFalse(backend._lock.locked())
        with self.assertRaises(link.MgmtError):
            await backend.connections()

    async def test_timeout_closes_socket_and_releases_lock(self):
        backend = self.backend()
        with mock.patch.object(link, "COMMAND_TIMEOUT", 0.001):
            with self.assertRaisesRegex(link.MgmtError, "timed out"):
                await backend.connections()
        self.assertTrue(backend._socket.closed)
        self.assertFalse(backend._lock.locked())

    async def test_write_error_or_short_write_closes_without_orphaned_future(self):
        for failure in (OSError("no route"), None):
            backend = self.backend()
            backend._socket.send_error = failure
            if failure is None:
                backend._socket.send_result = 1
            with self.assertRaises(link.MgmtError):
                await backend.connections()
            self.assertTrue(backend._socket.closed)
            self.assertIsNone(backend._waiting)

    async def test_read_error_closes_and_wakes_waiter(self):
        backend = self.backend()
        operation = asyncio.create_task(backend.connections())
        await asyncio.sleep(0)
        backend._socket.recv_error = OSError("lost")
        backend._read_available()
        with self.assertRaisesRegex(link.MgmtError, "read failed"):
            await operation
        self.assertTrue(backend._socket.closed)
        self.assertIsNone(backend._waiting)

    async def test_read_eof_closes_and_wakes_waiter(self):
        backend = self.backend()
        operation = asyncio.create_task(backend.connections())
        await asyncio.sleep(0)
        backend._socket.recv_value = b""
        backend._read_available()
        with self.assertRaisesRegex(link.MgmtError, "closed by kernel"):
            await operation
        self.assertTrue(backend._socket.closed)
