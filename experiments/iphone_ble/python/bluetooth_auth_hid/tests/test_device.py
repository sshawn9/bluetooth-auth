"""device 的全部测试使用假系统总线和假 HCI ioctl。"""

from __future__ import annotations

import asyncio
import contextlib
import io
import struct
import unittest
from unittest.mock import Mock, patch

from dbus_fast import Message, Variant
from dbus_fast.constants import MessageType

from bluetooth_auth_hid import device


ADDRESS = "AA:BB:CC:DD:EE:FF"
OWNER = ":1.42"
ADAPTER = "/org/bluez/hci0"
TARGET = ADAPTER + "/dev_" + ADDRESS.replace(":", "_")


def managed_objects():
    return {
        ADAPTER: {
            "org.bluez.Adapter1": {
                "Powered": Variant("b", True), "UUIDs": Variant("as", []), "Alias": Variant("s", "host"),
            },
            "org.bluez.GattManager1": {}, "org.bluez.LEAdvertisingManager1": {},
        },
        TARGET: {"org.bluez.Device1": {
            "Address": Variant("s", ADDRESS), "Adapter": Variant("o", ADAPTER), "Blocked": Variant("b", False),
        }},
    }


class FakeBus:
    def __init__(self, *, fail_member=None, release_on_advertisement=False, wait_error=None, unexport_error=None):
        self.fail_member, self.connected = fail_member, False
        self.release_on_advertisement = release_on_advertisement
        self.wait_error, self.connect_gate = wait_error, None
        self.advertisement_gate = None
        self.unregister_gate = None
        self.advertising = False
        self.unexport_error = unexport_error
        self.lock_check, self.cleanup_lock_states = None, []
        self.calls, self.exports = [], []
        self.closed, self.disconnected = False, asyncio.Event()
        self.disconnect_count = self.wait_count = 0
        self.connect_started = asyncio.Event()

    async def connect(self):
        self.connect_started.set()
        if self.connect_gate is not None: await self.connect_gate.wait()
        self.connected = True
        return self

    async def call(self, message):
        self.calls.append(message)
        if message.member == "RegisterAdvertisement":
            if self.advertisement_gate is not None: await self.advertisement_gate.wait()
            if message.member != self.fail_member: self.advertising = True
        if message.member == "RegisterAdvertisement" and self.release_on_advertisement:
            self.advertising = False
            next(item for path, item in self.exports if path == device.ADVERTISEMENT).Release()
        if message.member == self.fail_member:
            return Message(message_type=MessageType.ERROR, sender=message.destination,
                           error_name="org.bluez.Error.Failed", reply_serial=1)
        if message.member == "UnregisterAdvertisement":
            if self.lock_check is not None: self.cleanup_lock_states.append(self.lock_check())
            if self.unregister_gate is not None: await self.unregister_gate.wait()
            if not self.advertising:
                return Message(message_type=MessageType.ERROR, sender=message.destination,
                               error_name="org.bluez.Error.DoesNotExist", reply_serial=1)
            self.advertising = False
        if message.member == "GetNameOwner":
            return Message(message_type=MessageType.METHOD_RETURN, sender="org.freedesktop.DBus", body=[OWNER], reply_serial=1)
        if message.member == "GetManagedObjects":
            return Message(message_type=MessageType.METHOD_RETURN, sender=OWNER, body=[managed_objects()], reply_serial=1)
        return Message(message_type=MessageType.METHOD_RETURN, sender=OWNER, body=[], reply_serial=1)

    def add_message_handler(self, _handler): pass
    def export(self, path, item): self.exports.append((path, item))
    def unexport(self, path):
        if path == device.ADVERTISEMENT and self.lock_check is not None:
            self.cleanup_lock_states.append(self.lock_check())
        if self.unexport_error is not None: raise self.unexport_error
        self.exports = [item for item in self.exports if item[0] != path]
    def disconnect(self):
        if self.lock_check is not None: self.cleanup_lock_states.append(self.lock_check())
        self.closed, self.connected = True, False
        self.disconnect_count += 1
        self.disconnected.set()
    async def wait_for_disconnect(self):
        self.wait_count += 1
        if self.wait_error is not None: raise self.wait_error
        await self.disconnected.wait()


class FakeSocket:
    def __init__(self): self.closed = False
    def fileno(self): return 42
    def close(self): self.closed = True
    def __enter__(self): return self
    def __exit__(self, *_): self.close()


class FakeLock:
    def __init__(self, *, enter_error=None, exit_error=None, gate=None):
        self.enter_error, self.exit_error, self.gate = enter_error, exit_error, gate
        self.entered = asyncio.Event()
        self.acquire_count = self.release_count = 0
        self.acquired = False

    async def acquire(self):
        self.acquire_count += 1
        self.entered.set()
        if self.gate is not None: await self.gate.wait()
        if self.enter_error is not None: raise self.enter_error
        self.acquired = True
        return True

    def release(self):
        self.release_count += 1
        if self.exit_error is not None: raise self.exit_error
        self.acquired = False

    def locked(self): return self.acquired

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *_):
        self.release()


def write_links(buffer, links, *, index=0):
    header, entry = struct.Struct("=HH"), struct.Struct("=H6sBBHI")
    header.pack_into(buffer, 0, index, len(links))
    for number, (address, encrypted, handle, *kind) in enumerate(links):
        link_type, state = kind or (0x80, 1)
        entry.pack_into(buffer, header.size + number * entry.size, handle,
                        bytes.fromhex(address.replace(":", ""))[::-1], link_type, 0, state,
                        0x0004 if encrypted else 0)


class DeviceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus, self.socket = FakeBus(), FakeSocket()
        self.patches = [patch.object(device, "MessageBus", side_effect=lambda **_kwargs: self.bus)]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in reversed(self.patches): item.stop()

    def context(self): return device.BluetoothContext(ADDRESS)

    async def test_register_is_idempotent_concurrent_and_unregister_closes_once(self):
        ctx = device.BluetoothContext(ADDRESS.lower())
        self.assertEqual(await asyncio.gather(*(device.register_hid(ctx) for _ in range(3))), [True] * 3)
        self.assertTrue(ctx._registered)
        self.assertEqual([item.member for item in self.bus.calls if item.member.startswith("Register")],
                         ["RegisterApplication"])
        await asyncio.gather(device.unregister_hid(ctx), device.unregister_hid(ctx))
        self.assertTrue(self.bus.closed)
        self.assertEqual((self.bus.disconnect_count, self.bus.wait_count), (1, 1))
        self.assertFalse(ctx._registered)
        self.assertIsNone(ctx._bus)
        self.assertFalse({"Connect", "Pair", "Set", "Disconnect"} & {item.member for item in self.bus.calls})

    async def test_partial_failure_closes_and_allows_later_registration(self):
        ctx = self.context()
        lock = FakeLock()
        ctx._lock = lock
        self.bus.fail_member = "RegisterApplication"
        self.bus.lock_check = ctx._lock.locked
        output = io.StringIO()
        with contextlib.redirect_stderr(output): self.assertFalse(await device.register_hid(ctx))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(self.bus.closed)
        self.assertEqual(self.bus.cleanup_lock_states, [True])
        self.assertEqual((lock.acquire_count, lock.release_count, lock.locked()), (1, 1, False))
        self.assertIsNone(ctx._bus)
        self.bus = FakeBus()
        self.assertTrue(await device.register_hid(ctx))
        self.assertTrue(ctx._registered)

    async def test_release_during_connect_does_not_unregister_hid(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.release_on_advertisement = True
        with patch.object(device, "_query", new=Mock(return_value=False)), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await device.connect(ctx, 0.01))
        self.assertTrue(ctx._registered)
        self.assertIsNotNone(ctx._bus)
        self.assertFalse(self.bus.advertising)
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])

    async def test_query_requires_exact_one_connected_encrypted_le(self):
        cases = [([], False), ([(ADDRESS, True, 1)], True),
                 ([("FF:FF:FF:FF:FF:FF", True, 1)], False),
                 ([(ADDRESS, True, 1), (ADDRESS, True, 2)], None),
                 ([(ADDRESS, False, 1)], False),
                 ([(ADDRESS, True, 1, 1, 1)], False),
                 ([(ADDRESS, True, 1, 0x80, 2)], False)]
        for links, expected in cases:
            with self.subTest(links=links):
                def ioctl(_fd, command, buffer, mutate):
                    self.assertEqual(command, 0x800448D4); self.assertTrue(mutate)
                    write_links(buffer, links)
                with patch.object(device.socket, "socket", return_value=self.socket), \
                     patch.object(device.fcntl, "ioctl", side_effect=ioctl):
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertIs(await device.query(self.context()), False if expected is None else expected)
                self.assertTrue(self.socket.closed)

    async def test_query_hci_error_prints_once_and_returns_failure(self):
        ctx, output = self.context(), io.StringIO()
        error = PermissionError(13, "没有权限读取蓝牙连接")
        with patch.object(device.socket, "socket", side_effect=error) as factory, \
             contextlib.redirect_stderr(output):
            self.assertFalse(await device.query(ctx))
        self.assertEqual(output.getvalue(), f"错误：query：{error}\n")
        factory.assert_called_once()

    async def test_connect_without_hid_only_accepts_an_existing_link(self):
        ctx = self.context()
        with patch.object(device, "_query", new=Mock(return_value=True)) as query:
            self.assertTrue(await device.connect(ctx, timeout=0.01))
            query.assert_called_once_with(ctx)
        with patch.object(device, "_query", new=Mock(return_value=False)):
            with contextlib.redirect_stderr(io.StringIO()): self.assertFalse(await device.connect(ctx, timeout=0.01))
        error, output = OSError(5, "HCI 读取失败"), io.StringIO()
        with patch.object(device, "_query", side_effect=error) as query, contextlib.redirect_stderr(output):
            self.assertFalse(await device.connect(ctx, timeout=0.01))
        query.assert_called_once_with(ctx)
        self.assertEqual(output.getvalue(), f"错误：connect：{error}\n")
        self.assertFalse(ctx._registered)  # 未注册不隐式建立 HID 或广播。
        self.assertFalse(self.bus.calls)

    async def test_existing_hid_owner_lookup_failure_keeps_its_bus(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        original = ctx._bus
        self.bus.fail_member = "GetNameOwner"
        with contextlib.redirect_stderr(io.StringIO()): self.assertFalse(await device.register_hid(ctx))
        self.assertIs(ctx._bus, original)
        self.assertTrue(ctx._registered)
        self.assertFalse(self.bus.closed)

    async def test_lock_entry_failure_returns_false_once_without_touching_resources(self):
        for operation in (device.register_hid, device.unregister_hid, device.connect):
            with self.subTest(operation=operation.__name__):
                ctx, output = self.context(), io.StringIO()
                ctx._bus, ctx._registered, ctx._lock = self.bus, True, FakeLock(enter_error=OSError(5, "lock"))
                with contextlib.redirect_stderr(output):
                    self.assertFalse(await operation(ctx))
                self.assertEqual(len(output.getvalue().splitlines()), 1)
                self.assertIs(ctx._bus, self.bus)
                self.assertTrue(ctx._registered)
                self.assertFalse(self.bus.closed)

    async def test_lock_exit_failure_returns_false_once(self):
        ctx, output = self.context(), io.StringIO()
        self.assertTrue(await device.register_hid(ctx))
        ctx._lock = FakeLock(exit_error=OSError(5, "lock"))
        with patch.object(device, "_query", new=Mock(return_value=True)), contextlib.redirect_stderr(output):
            self.assertFalse(await device.connect(ctx))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertTrue(ctx._registered)

    async def test_register_and_unregister_lock_exit_failures_return_false_once(self):
        self.assertTrue(await device.register_hid(self.context()))
        ctx, output = self.context(), io.StringIO()
        ctx._bus, ctx._owner, ctx._registered = self.bus, OWNER, True
        ctx._lock = FakeLock(exit_error=OSError(5, "lock"))
        with contextlib.redirect_stderr(output): self.assertFalse(await device.register_hid(ctx))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIs(ctx._bus, self.bus)

        self.bus, ctx, output = FakeBus(), self.context(), io.StringIO()
        self.assertTrue(await device.register_hid(ctx))
        ctx._lock = FakeLock(exit_error=OSError(5, "lock"))
        with contextlib.redirect_stderr(output): self.assertFalse(await device.unregister_hid(ctx))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIsNone(ctx._bus)

    async def test_cancellation_waiting_for_lock_preserves_other_call_resources(self):
        for operation in (device.register_hid, device.connect, device.unregister_hid):
            with self.subTest(operation=operation.__name__):
                self.bus = FakeBus()
                ctx = self.context()
                self.assertTrue(await device.register_hid(ctx))
                original = ctx._bus
                await ctx._lock.acquire()
                task = asyncio.create_task(operation(ctx))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
                self.assertTrue(ctx._lock.locked())
                self.assertIs(ctx._bus, original)
                self.assertTrue(ctx._registered)
                self.assertFalse(self.bus.closed)
                ctx._lock.release()

    async def test_invalid_connect_timeout_does_not_advertise(self):
        ctx = self.context()
        with patch.object(device, "_query", new=Mock(return_value=False)), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await device.connect(ctx, 0))
        self.assertFalse(any(call.member == "RegisterAdvertisement" for call in self.bus.calls))

    async def test_connect_preserves_existing_hid_for_success_timeout_and_concurrency(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        original_bus = ctx._bus
        self.bus.lock_check = ctx._lock.locked
        with patch.object(device, "_query", new=Mock(side_effect=[False, True])):
            self.assertTrue(await device.connect(ctx, 0.01))
        members = [call.member for call in self.bus.calls]
        self.assertEqual(members.count("RegisterAdvertisement"), 1)
        self.assertEqual(members.count("UnregisterAdvertisement"), 1)
        self.assertEqual(self.bus.cleanup_lock_states, [True, True])
        self.assertFalse(self.bus.advertising)
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])
        with patch.object(device, "_query", new=Mock(return_value=False)), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await device.connect(ctx, 0.01))
        members = [call.member for call in self.bus.calls]
        self.assertEqual(members.count("RegisterAdvertisement"), 2)
        self.assertEqual(members.count("UnregisterAdvertisement"), 2)
        self.assertFalse(self.bus.advertising)
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])
        before = list(self.bus.calls)
        with patch.object(device, "_query", new=Mock(return_value=True)):
            self.assertEqual(await asyncio.gather(*(device.connect(ctx, .01) for _ in range(3))), [True] * 3)
        self.assertEqual(self.bus.calls, before)
        self.assertTrue(ctx._registered)
        self.assertIs(ctx._bus, original_bus)

    async def test_successful_register_releases_its_acquired_lock(self):
        ctx, lock = self.context(), FakeLock()
        ctx._lock = lock
        self.assertTrue(await device.register_hid(ctx))
        self.assertEqual((lock.acquire_count, lock.release_count, lock.locked()), (1, 1, False))

    async def test_failed_close_keeps_handle_for_a_later_unregister(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.wait_error = OSError(9, "句柄无效")
        output = io.StringIO()
        with contextlib.redirect_stderr(output): self.assertFalse(await device.unregister_hid(ctx))
        self.assertIs(ctx._bus, self.bus)
        self.assertEqual(output.getvalue(), f"错误：unregister_hid：{self.bus.wait_error}\n")
        self.bus.wait_error = None
        self.assertTrue(await device.unregister_hid(ctx))

    async def test_cancelled_registration_still_closes_its_bus(self):
        ctx = self.context()
        self.bus.connect_gate = asyncio.Event()
        task = asyncio.create_task(device.register_hid(ctx))
        await self.bus.connect_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(self.bus.closed)
        self.assertIsNone(ctx._bus)

    async def test_cancelled_registration_survives_a_separate_cleanup_failure(self):
        ctx = self.context()
        self.bus.connect_gate, self.bus.wait_error = asyncio.Event(), OSError(9, "句柄无效")
        task = asyncio.create_task(device.register_hid(ctx))
        await self.bus.connect_started.wait()
        task.cancel()
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaises(asyncio.CancelledError) as raised: await task
        self.assertEqual(raised.exception.__notes__, [f"注销 HID 失败：{self.bus.wait_error}"])
        self.assertEqual(output.getvalue(), "")

    async def test_cancelled_advertisement_registration_unexports_but_keeps_gatt(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        lock = FakeLock()
        ctx._lock = lock
        self.bus.advertisement_gate = asyncio.Event()
        with patch.object(device, "_query", new=Mock(return_value=False)):
            task = asyncio.create_task(device.connect(ctx, 1))
            while not any(call.member == "RegisterAdvertisement" for call in self.bus.calls):
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(ctx._registered)
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])
        self.assertEqual((lock.acquire_count, lock.release_count, lock.locked()), (1, 1, False))

        self.bus = FakeBus(fail_member="UnregisterAdvertisement")
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.advertisement_gate = asyncio.Event()
        with patch.object(device, "_query", new=Mock(return_value=False)):
            task = asyncio.create_task(device.connect(ctx, 1))
            while not any(call.member == "RegisterAdvertisement" for call in self.bus.calls):
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as raised: await task
        self.assertTrue(any("停止广播失败" in note for note in raised.exception.__notes__))
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])

    async def test_cancellation_during_advertisement_stop_unexports_and_preserves_gatt(self):
        for result in ("success", "timeout"):
            with self.subTest(result=result):
                self.bus = FakeBus()
                ctx = self.context()
                self.assertTrue(await device.register_hid(ctx))
                self.bus.unregister_gate = asyncio.Event()
                values = [False, True] if result == "success" else [False]
                with patch.object(device, "_query", new=Mock(side_effect=values)):
                    task = asyncio.create_task(device.connect(ctx, .01))
                    while not any(call.member == "UnregisterAdvertisement" for call in self.bus.calls):
                        await asyncio.sleep(0)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task
                self.assertTrue(ctx._registered)
                self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])

    async def test_advertisement_register_failure_logs_once_and_cleanup_ignores_missing(self):
        ctx, output = self.context(), io.StringIO()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.fail_member = "RegisterAdvertisement"
        with patch.object(device, "_query", new=Mock(return_value=False)), contextlib.redirect_stderr(output):
            self.assertFalse(await device.connect(ctx, .01))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("RegisterAdvertisement", output.getvalue())
        self.assertFalse(self.bus.advertising)
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])

    async def test_advertisement_cleanup_failure_returns_false_once_and_keeps_gatt(self):
        ctx, output = self.context(), io.StringIO()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.fail_member = "UnregisterAdvertisement"
        with patch.object(device, "_query", new=Mock(side_effect=[False, True])), contextlib.redirect_stderr(output):
            self.assertFalse(await device.connect(ctx, .01))
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertIn("UnregisterAdvertisement", output.getvalue())
        self.assertTrue(ctx._registered)
        self.assertNotIn(device.ADVERTISEMENT, [path for path, _ in self.bus.exports])

    async def test_advertisement_unexport_error_returns_false_without_leaking(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.unexport_error = OSError(9, "fixture")
        with patch.object(device, "_query", new=Mock(side_effect=[False, True])), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await device.connect(ctx, .01))
        self.assertTrue(ctx._registered)

    async def test_cancelled_unexport_releases_lock_and_propagates_cancellation(self):
        ctx = self.context()
        self.assertTrue(await device.register_hid(ctx))
        self.bus.unexport_error = asyncio.CancelledError()
        with patch.object(device, "_query", new=Mock(side_effect=[False, True])):
            with self.assertRaises(asyncio.CancelledError): await device.connect(ctx, .01)
        self.assertFalse(ctx._lock.locked())
        self.assertTrue(ctx._registered)

    async def test_registration_and_cleanup_failures_are_reported_together_at_boundary(self):
        ctx, output = self.context(), io.StringIO()
        self.bus.fail_member, self.bus.wait_error = "RegisterApplication", OSError(9, "句柄无效")
        with contextlib.redirect_stderr(output): self.assertFalse(await device.register_hid(ctx))
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("BlueZ RegisterApplication 失败：org.bluez.Error.Failed", lines[0])
        self.assertIn(f"注销 HID 失败：{self.bus.wait_error}", lines[0])

    def test_exported_gatt_and_advertisement_introspect(self):
        ctx = self.context()
        self.assertNotIn(ADDRESS, repr(ctx))
        objects = device._gatt_objects(ctx, set())
        manager = device._ObjectManager(objects)
        self.assertTrue(manager.introspect().methods)
        for item in [*objects.values(), device._Advertisement("host")]:
            interface = item.introspect()
            self.assertTrue(interface.properties or interface.methods)
            reads = []
            device.ServiceInterface._get_all_property_values(
                item, lambda _item, values, _data, error: reads.append((values, error)))
            self.assertEqual(len(reads), 1)
            values, error = reads[0]
            self.assertIsNone(error)
            self.assertEqual(set(values), {prop.name for prop in interface.properties})


if __name__ == "__main__":
    unittest.main()
