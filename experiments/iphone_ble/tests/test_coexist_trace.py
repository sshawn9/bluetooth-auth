from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coexist_trace as trace


class FakeSocket:
    def __init__(self): self.closed, self.recv_value, self.recv_error = False, None, None
    def close(self): self.closed = True
    def recv(self, _size):
        if self.recv_error: raise self.recv_error
        if self.recv_value is not None: return self.recv_value
        raise BlockingIOError


def record(kind, index, payload):
    return struct.pack("<HHH", kind, index, len(payload)) + payload


def command(opcode, parameters):
    return struct.pack("<HB", opcode, len(parameters)) + parameters


class CoexistTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.socket = FakeSocket()
        self.monitor = trace.CoexistTrace(self.socket, 1, lambda name, data: self.events.append((name, data)))

    def feed_command(self, opcode, parameters, index=1):
        self.monitor._receive(record(trace.HCI_MON_COMMAND_PKT, index, command(opcode, parameters)))

    def test_legacy_parameters_and_safe_ad_summary(self):
        self.feed_command(0x2006, struct.pack("<HH", 0x20, 0x40) + bytes((0, 1, 0)) + b"secret" + bytes((7, 2)))
        ad = bytes((2, 1, 6, 3, 3, 0x12, 0x18, 3, 0x19, 0xC0, 3, 4, 9)) + b"HID"
        self.feed_command(0x2008, bytes((len(ad),)) + ad + bytes(31 - len(ad)))
        parameters, data = self.events[0][1], self.events[1][1]["data"]
        self.assertEqual(parameters["interval_ms"], {"min": 20.0, "max": 40.0})
        self.assertTrue(parameters["connectable"])
        self.assertEqual(data, {"location": "primary", "length": len(ad), "hid_service": True, "flags": [6], "appearance": [960], "local_name": ["HID"]})
        self.assertNotIn("secret", repr(self.monitor.summary()))

    def test_extended_commands_and_whitelisted_status(self):
        params = bytes((3,)) + struct.pack("<H", 0x0013) + (0x100).to_bytes(3, "little") * 2 + bytes((7, 1, 0)) + b"hidden" + bytes((2, 0, 0, 1, 0, 0, 0))
        self.feed_command(0x2036, params)
        self.feed_command(0x2039, bytes((1, 1, 3, 2, 0, 4)))
        complete = bytes((1,)) + struct.pack("<H", 0x2036) + bytes((0,))
        self.monitor._receive(record(trace.HCI_MON_EVENT_PKT, 1, bytes((0x0E, len(complete))) + complete))
        self.assertEqual(self.events[0][1]["pdu"], "legacy")
        self.assertEqual(self.events[0][1]["handle"], 3)
        self.assertEqual(self.events[1][1]["controller_api"], "extended")
        self.assertNotIn("pdu", self.events[1][1])
        self.assertEqual(self.events[1][1]["sets"], [{"handle": 3, "duration_ms": 20, "max_events": 4}])
        self.assertEqual(self.events[2], ("advertising_status", {"opcode": "0x2036", "status": 0, "event": "complete"}))
        self.assertNotIn("hidden", repr(self.monitor.summary()))

    def test_unknown_acl_ltk_and_malformed_data_never_emit_or_leak(self):
        secret = b"LTK-DO-NOT-LEAK"
        self.monitor._receive(record(4, 1, secret))
        self.feed_command(0x2019, secret)  # LE Start Encryption, deliberately not allowed
        self.monitor._receive(record(trace.HCI_MON_EVENT_PKT, 1, bytes((0x3E, 3, 1, 2, 3))))
        self.monitor._receive(record(trace.HCI_MON_COMMAND_PKT, 1, b"\x08\x20\xff"))
        self.monitor._receive(b"\x02\x00")
        self.assertEqual(self.events, [])
        self.assertNotIn("LTK", repr(self.monitor.summary()))

    def test_random_advertising_address_is_only_a_configuration_flag(self):
        self.feed_command(0x2035, bytes((7,)) + b"secret")
        self.assertEqual(self.events, [("advertising_command", {"opcode": "0x2035", "handle": 7, "configured_random_address": True})])
        self.assertNotIn("secret", repr(self.monitor.summary()))

    async def test_index_filter_status_and_closed_handles(self):
        self.feed_command(0x200A, b"\x01", index=2)
        status = bytes((0x0C, 1)) + struct.pack("<H", 0x200A)
        self.monitor._receive(record(trace.HCI_MON_EVENT_PKT, 1, bytes((0x0F, len(status))) + status))
        self.assertEqual(self.events, [("advertising_status", {"opcode": "0x200a", "status": 12, "event": "status"})])
        self.socket.recv_value = b""
        self.monitor._read_available()
        self.assertTrue(self.socket.closed)
        await self.monitor.close()
        self.assertTrue(self.monitor.summary()["closed"])
