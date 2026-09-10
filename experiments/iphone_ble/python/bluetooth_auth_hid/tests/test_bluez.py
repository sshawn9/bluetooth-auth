from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dbus_fast import Message, MessageType

from bluetooth_auth_hid import bluez
from bluetooth_auth_hid.link import LinkInfo


ADDRESS = "AA:BB:CC:DD:EE:FF"
OWNER = ":1.42"
TARGET = "/org/bluez/hci0/dev_" + ADDRESS.replace(":", "_")


def variant(value):
    return SimpleNamespace(value=value)


def objects(*, connected=True, paired=True, bonded=True, trusted=True):
    return {
        "/org/bluez/hci0": {
            bluez.ADAPTER: {"Powered": variant(True), "UUIDs": variant([]), "Alias": variant("host")},
            bluez.GATT_MANAGER: {},
            bluez.AD_MANAGER: {},
        },
        TARGET: {
            bluez.DEVICE: {
                "Adapter": variant("/org/bluez/hci0"), "Address": variant(ADDRESS),
                "Paired": variant(paired), "Bonded": variant(bonded),
                "Trusted": variant(trusted), "Blocked": variant(False),
                "Connected": variant(connected),
            }
        },
    }


class FakeBus:
    def __init__(
        self,
        managed_objects,
        *,
        register_failure=False,
        register_timeout=False,
        request_name_reply=1,
        connect_failure=False,
        owner_failure=False,
    ):
        self.objects = managed_objects
        self.register_failure = register_failure
        self.register_timeout = register_timeout
        self.request_name_reply = request_name_reply
        self.connect_failure = connect_failure
        self.owner_failure = owner_failure
        self.calls = []
        self.handlers = []
        self.disconnected = asyncio.Event()
        self.closed = False
        self.target_get_all_started = asyncio.Event()
        self.target_get_all_gate: asyncio.Event | None = None

    async def connect(self):
        if self.connect_failure:
            raise OSError("unavailable")
        return self

    async def call(self, message):
        self.calls.append(message)
        if self.register_timeout and message.member.startswith("Register"):
            await asyncio.sleep(60)
        if self.register_failure and message.member.startswith("Register"):
            return SimpleNamespace(sender=message.destination, message_type=MessageType.ERROR, body=[])
        if message.member == "GetNameOwner":
            if self.owner_failure:
                return SimpleNamespace(sender=bluez.DBUS, message_type=MessageType.ERROR, body=[])
            return SimpleNamespace(sender=bluez.DBUS, message_type=MessageType.METHOD_RETURN, body=[OWNER])
        if message.member == "RequestName":
            return SimpleNamespace(
                sender=bluez.DBUS,
                message_type=MessageType.METHOD_RETURN,
                body=[self.request_name_reply],
            )
        if message.member == "GetManagedObjects":
            return SimpleNamespace(sender=OWNER, message_type=MessageType.METHOD_RETURN, body=[self.objects])
        if message.member == "GetAll":
            interface = message.body[0]
            if message.path == TARGET:
                self.target_get_all_started.set()
                if self.target_get_all_gate is not None:
                    await self.target_get_all_gate.wait()
            return SimpleNamespace(
                sender=OWNER,
                message_type=MessageType.METHOD_RETURN,
                body=[self.objects[message.path][interface]],
            )
        return SimpleNamespace(sender=message.destination, message_type=MessageType.METHOD_RETURN, body=[])

    def add_message_handler(self, handler):
        self.handlers.append(handler)

    def remove_message_handler(self, handler):
        self.handlers.remove(handler)

    def disconnect(self):
        self.closed = True
        self.disconnected.set()

    async def wait_for_disconnect(self):
        await self.disconnected.wait()


class FakeReader:
    def __init__(self, links=()):
        self.links = list(links)
        self.calls = []

    def read(self, adapter_index):
        self.calls.append(adapter_index)
        return list(self.links)


class FakeApp:
    def __init__(self, *_args, **_kwargs):
        self.exported = False

    def export(self):
        self.exported = True

    def unexport(self):
        self.exported = False


class FakeAdvertisement(FakeApp):
    def __init__(self, _name, released):
        super().__init__()
        self.released = released

    def export(self, *_args):
        self.exported = True


class BlueZBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patches = [
            patch.object(bluez, "HidApplication", FakeApp),
            patch.object(bluez, "Advertisement", FakeAdvertisement),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    async def open_backend(self, bus, reader):
        backend = bluez.BlueZBackend(ADDRESS, call_timeout=0.02, link_reader=reader, bus_factory=lambda: bus)
        await backend.open()
        self.addAsyncCleanup(backend.close)
        return backend

    async def test_probe_queries_fresh_snapshot_and_requires_exact_le_encrypted_link(self):
        bus = FakeBus(objects())
        reader = FakeReader([LinkInfo(ADDRESS, 1, bluez.HCI_LE_LINK, bluez.BT_CONNECTED, True)])
        backend = await self.open_backend(bus, reader)

        self.assertTrue(await backend.connected())
        bus.objects = objects(connected=False)
        self.assertFalse(await backend.connected())
        self.assertEqual(reader.calls, [0, 0])

        bus.objects = objects()
        reader.links = [LinkInfo(ADDRESS, 1, 1, bluez.BT_CONNECTED, True)]
        self.assertFalse(await backend.connected())
        reader.links = [LinkInfo(ADDRESS, 1, bluez.HCI_LE_LINK, bluez.BT_CONNECTED, False)]
        self.assertFalse(await backend.connected())
        reader.links = [LinkInfo("FF:FF:FF:FF:FF:FF", 1, bluez.HCI_LE_LINK, bluez.BT_CONNECTED, True)]
        self.assertFalse(await backend.connected())

    async def test_probe_requires_paired_bonded_and_trusted_target(self):
        bus = FakeBus(objects())
        reader = FakeReader([LinkInfo(ADDRESS, 1, bluez.HCI_LE_LINK, bluez.BT_CONNECTED, True)])
        backend = await self.open_backend(bus, reader)

        for field in ("paired", "bonded", "trusted"):
            with self.subTest(field=field):
                args = {field: False}
                bus.objects = objects(**args)
                with self.assertRaises(bluez.BackendError):
                    await backend.connected()

    async def test_disconnect_and_owner_change_invalidate_old_result(self):
        bus = FakeBus(objects())
        reader = FakeReader([LinkInfo(ADDRESS, 1, bluez.HCI_LE_LINK, bluez.BT_CONNECTED, True)])
        backend = await self.open_backend(bus, reader)
        self.assertTrue(await backend.connected())

        disconnected = Message(
            message_type=MessageType.SIGNAL, sender=OWNER, path=TARGET,
            interface=bluez.DEVICE, member="Disconnected",
        )
        backend._message(disconnected)
        self.assertEqual(backend.disconnect_count, 1)
        bus.objects = objects(connected=False)
        self.assertFalse(await backend.connected())

        owner_changed = Message(
            message_type=MessageType.SIGNAL, sender=bluez.DBUS,
            path="/org/freedesktop/DBus", interface=bluez.DBUS,
            member="NameOwnerChanged", signature="sss", body=[bluez.BLUEZ, OWNER, ":1.77"],
        )
        backend._message(owner_changed)
        with self.assertRaises(bluez.BackendError):
            await backend.connected()

    async def test_rejects_forged_gatt_sender_using_real_message_sender(self):
        bus = FakeBus(objects())
        backend = await self.open_backend(bus, FakeReader())
        forged = Message(
            message_type=MessageType.METHOD_CALL, serial=1, sender=":1.99", path=bluez.ROOT,
            interface="org.freedesktop.DBus.Peer", member="Ping",
        )
        self.assertEqual(forged.sender, ":1.99")
        reply = backend._message(forged)
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message_type, MessageType.ERROR)

        valid = Message(
            message_type=MessageType.METHOD_CALL, serial=2, sender=OWNER, path=bluez.ROOT,
            interface="org.freedesktop.DBus.Peer", member="Ping",
        )
        self.assertIsNone(backend._message(valid))

    async def test_open_holds_registration_without_connection_commands(self):
        bus = FakeBus(objects())
        backend = await self.open_backend(bus, FakeReader())
        registered = [message.member for message in bus.calls if message.member.startswith("Register")]
        self.assertEqual(registered, ["RegisterApplication", "RegisterAdvertisement"])
        self.assertEqual(
            [message.member for message in bus.calls if message.member.startswith("Register")],
            registered,
        )

    async def test_registration_failure_and_timeout_close_bus_without_connection_commands(self):
        for mode in ("failure", "timeout"):
            with self.subTest(mode=mode):
                bus = FakeBus(objects(), register_failure=mode == "failure", register_timeout=mode == "timeout")
                backend = bluez.BlueZBackend(ADDRESS, call_timeout=0.02, link_reader=FakeReader(), bus_factory=lambda: bus)
                with self.assertRaises((bluez.BackendError, TimeoutError)):
                    await backend.open()
                self.assertTrue(bus.closed)
                self.assertFalse({"Connect", "Pair", "Set", "Disconnect"} & {m.member for m in bus.calls})

    async def test_connected_rejects_disconnect_or_trust_change_during_get_all(self):
        for kind in ("disconnect", "trust"):
            with self.subTest(kind=kind):
                bus = FakeBus(objects())
                reader = FakeReader([LinkInfo(ADDRESS, 1, bluez.HCI_LE_LINK, bluez.BT_CONNECTED, True)])
                backend = await self.open_backend(bus, reader)
                bus.target_get_all_gate = asyncio.Event()
                task = asyncio.create_task(backend.connected())
                await bus.target_get_all_started.wait()
                if kind == "disconnect":
                    signal = Message(
                        message_type=MessageType.SIGNAL,
                        sender=OWNER,
                        path=TARGET,
                        interface=bluez.DEVICE,
                        member="Disconnected",
                    )
                else:
                    signal = Message(
                        message_type=MessageType.SIGNAL,
                        sender=OWNER,
                        path=TARGET,
                        interface=bluez.PROPERTIES,
                        member="PropertiesChanged",
                        signature="sa{sv}as",
                        body=[bluez.DEVICE, {"Trusted": variant(False)}, []],
                    )
                backend._message(signal)
                bus.target_get_all_gate.set()
                with self.assertRaises(bluez.BackendError):
                    await task

    async def test_occupied_name_never_registers_hid(self):
        bus = FakeBus(objects(), request_name_reply=3)
        backend = bluez.BlueZBackend(ADDRESS, call_timeout=0.02, bus_factory=lambda: bus)
        with self.assertRaises(bluez.BackendError):
            await backend.open()
        self.assertTrue(bus.closed)
        self.assertFalse(any(call.member.startswith("Register") for call in bus.calls))

    async def test_connect_or_owner_failure_closes_without_handler_remove_error(self):
        for bus in (FakeBus(objects(), connect_failure=True), FakeBus(objects(), owner_failure=True)):
            with self.subTest(owner_failure=bus.owner_failure):
                backend = bluez.BlueZBackend(ADDRESS, call_timeout=0.02, bus_factory=lambda: bus)
                with self.assertRaises((bluez.BackendError, OSError)):
                    await backend.open()
                self.assertTrue(bus.closed)


if __name__ == "__main__":
    unittest.main()
