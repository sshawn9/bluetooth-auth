import asyncio
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbus_fast.errors import DBusError
from coexist_pairing import PairingAgent


TARGET = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"


class PairingAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.numbers = []

        async def allow(device): return device == TARGET
        async def approve(number):
            self.numbers.append(number)
            return True
        self.agent = PairingAgent(allow, approve, lambda event, details: self.events.append((event, details)))

    async def test_accepts_one_target_numeric_comparison(self):
        await self.agent.request_confirmation(TARGET, 123456)
        self.assertEqual(self.numbers, [123456])
        self.assertTrue(self.agent.accepted)
        self.assertEqual(self.agent.confirmed_device, TARGET)
        with self.assertRaises(DBusError) as duplicate:
            await self.agent.request_confirmation(TARGET, 654321)
        self.assertEqual(duplicate.exception.type, "org.bluez.Error.Rejected")

    async def test_refuses_other_pairing_flows_and_other_device(self):
        async def refuse(_number): return False
        self.agent = PairingAgent(self.agent._allow_device, refuse, self.agent._emit)
        for device in ("/org/bluez/hci0/dev_OTHER", TARGET):
            with self.assertRaises(DBusError) as rejected:
                await self.agent.request_confirmation(device, 1)
            self.assertEqual(rejected.exception.type, "org.bluez.Error.Rejected")
        with self.assertRaises(DBusError) as just_works:
            self.agent.RequestAuthorization.__dict__["__DBUS_METHOD"].fn(self.agent, TARGET)
        self.assertEqual(just_works.exception.type, "org.bluez.Error.Rejected")
        with self.assertRaises(DBusError) as passkey:
            self.agent.DisplayPasskey.__dict__["__DBUS_METHOD"].fn(self.agent, TARGET, 1, 1)
        self.assertEqual(passkey.exception.type, "org.bluez.Error.Rejected")

    async def test_cancel_and_concurrent_confirmation_are_rejected(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def wait_for_user(_number):
            entered.set()
            await release.wait()
            return True
        self.agent = PairingAgent(self.agent._allow_device, wait_for_user, self.agent._emit)
        first = asyncio.create_task(self.agent.request_confirmation(TARGET, 123456))
        await entered.wait()
        with self.assertRaises(DBusError) as busy:
            await self.agent.request_confirmation(TARGET, 123457)
        self.assertEqual(busy.exception.type, "org.bluez.Error.Rejected")
        self.agent.Cancel.__dict__["__DBUS_METHOD"].fn(self.agent)
        with self.assertRaises(DBusError) as cancelled:
            await first
        self.assertEqual(cancelled.exception.type, "org.bluez.Error.Canceled")
        self.assertIsNone(self.agent._pending_task)
        self.agent = PairingAgent(self.agent._allow_device, wait_for_user, self.agent._emit)
        second = asyncio.create_task(self.agent.request_confirmation(TARGET, 123458))
        await asyncio.sleep(0)
        self.agent.Release.__dict__["__DBUS_METHOD"].fn(self.agent)
        with self.assertRaises(DBusError) as released:
            await second
        self.assertEqual(released.exception.type, "org.bluez.Error.Canceled")

    async def test_authorize_service_allows_only_target_hid(self):
        await self.agent.authorize_service(TARGET, "00001812-0000-1000-8000-00805f9b34fb")
        with self.assertRaises(DBusError) as wrong_service:
            await self.agent.authorize_service(TARGET, "180a")
        self.assertEqual(wrong_service.exception.type, "org.bluez.Error.Rejected")
        with self.assertRaises(DBusError) as other_device:
            await self.agent.authorize_service("/org/bluez/hci0/dev_OTHER", "1812")
        self.assertEqual(other_device.exception.type, "org.bluez.Error.Rejected")
