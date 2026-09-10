from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest

from dbus_fast import MessageType

from bluetooth_auth_hid import client
from bluetooth_auth_hid.bluez import API_INTERFACE, API_PATH, BUS_NAME


DBUS = "org.freedesktop.DBus"
OWNER = ":1.42"


class FakeMessageBus:
    def __init__(self, *, value=True, uid=1000, ask_reply=None, stall=False, owner_sender=DBUS):
        self.value = value
        self.uid = uid
        self.ask_reply = ask_reply
        self.stall = stall
        self.owner_sender = owner_sender
        self.calls = []
        self.connected = False
        self.disconnected = False
        self.waited = False

    async def connect(self):
        self.connected = True
        return self

    async def call(self, message):
        self.calls.append(message)
        if message.member == "GetNameOwner":
            return SimpleNamespace(sender=self.owner_sender, message_type=MessageType.METHOD_RETURN,
                                   signature="s", body=[OWNER])
        if message.member == "GetConnectionUnixUser":
            return SimpleNamespace(sender=DBUS, message_type=MessageType.METHOD_RETURN,
                                   signature="u", body=[self.uid])
        if message.member == "AskOrConnect":
            if self.stall:
                await asyncio.sleep(60)
            if self.ask_reply is not None:
                return self.ask_reply
            return SimpleNamespace(sender=OWNER, message_type=MessageType.METHOD_RETURN,
                                   signature="b", body=[self.value])
        raise AssertionError(f"意外调用：{message.member}")

    def disconnect(self):
        self.disconnected = True

    async def wait_for_disconnect(self):
        self.waited = True


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_round_returns_true_or_false_with_one_api_call(self):
        for expected in (True, False):
            with self.subTest(expected=expected):
                bus = FakeMessageBus(value=expected)
                result = await client.ask_or_connect(
                    timeout=1, server_uid=1000, bus_factory=lambda: bus
                )
                self.assertIs(result, expected)
                self.assertEqual(
                    [(call.destination, call.path, call.interface, call.member)
                     for call in bus.calls],
                    [
                        (DBUS, "/org/freedesktop/DBus", DBUS, "GetNameOwner"),
                        (DBUS, "/org/freedesktop/DBus", DBUS, "GetConnectionUnixUser"),
                        (OWNER, API_PATH, API_INTERFACE, "AskOrConnect"),
                    ],
                )
                self.assertTrue(bus.disconnected)
                self.assertTrue(bus.waited)

    async def test_owner_uid_check_rejects_fake_service_before_api_call(self):
        bus = FakeMessageBus(uid=2000)
        with self.assertRaisesRegex(RuntimeError, "用户不匹配"):
            await client.ask_or_connect(timeout=1, server_uid=1000, bus_factory=lambda: bus)
        self.assertEqual([call.member for call in bus.calls], [
            "GetNameOwner", "GetConnectionUnixUser",
        ])
        self.assertTrue(bus.disconnected)
        self.assertTrue(bus.waited)

    async def test_rejects_forged_dbus_owner_reply_before_uid_lookup(self):
        bus = FakeMessageBus(owner_sender=":1.fake")
        with self.assertRaisesRegex(RuntimeError, "D-Bus 调用失败"):
            await client.ask_or_connect(timeout=1, server_uid=1000, bus_factory=lambda: bus)
        self.assertEqual([call.member for call in bus.calls], ["GetNameOwner"])
        self.assertTrue(bus.disconnected)

    async def test_timeout_has_no_retry_and_closes_bus(self):
        bus = FakeMessageBus(stall=True)
        with self.assertRaises(TimeoutError):
            await client.ask_or_connect(timeout=0.01, server_uid=1000, bus_factory=lambda: bus)
        self.assertEqual([call.member for call in bus.calls].count("AskOrConnect"), 1)
        self.assertTrue(bus.disconnected)
        self.assertTrue(bus.waited)

    async def test_rejects_invalid_api_return_signature_or_value_type(self):
        invalid_replies = (
            SimpleNamespace(sender=OWNER, message_type=MessageType.METHOD_RETURN,
                            signature="", body=[True]),
            SimpleNamespace(sender=OWNER, message_type=MessageType.METHOD_RETURN,
                            signature="b", body=[1]),
            SimpleNamespace(sender=OWNER, message_type=MessageType.METHOD_RETURN,
                            signature="b", body=[True, False]),
        )
        for reply in invalid_replies:
            with self.subTest(reply=reply):
                bus = FakeMessageBus(ask_reply=reply)
                with self.assertRaisesRegex(RuntimeError, "返回格式无效"):
                    await client.ask_or_connect(timeout=1, server_uid=1000, bus_factory=lambda: bus)
                self.assertEqual([call.member for call in bus.calls].count("AskOrConnect"), 1)
                self.assertTrue(bus.disconnected)
                self.assertTrue(bus.waited)


if __name__ == "__main__":
    unittest.main()
