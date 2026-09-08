#!/usr/bin/env python3
"""真实 D-Bus 退出回归：只连接本测试创建的临时 Unix 总线，不访问蓝牙。

直接运行本文件；--reproduce-old 只用于复现旧退出顺序。
Bluetooth 对象查询/注册使用固定夹具；D-Bus、HID 对象、子进程和 EOF 均为真实实现。
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus
import hid_release_test as release


PHONE = "AA:BB:CC:DD:EE:FF"


def local_socket_guard(endpoint):
    original = socket.socket

    class LocalOnlySocket(original):
        def __init__(self, family=socket.AF_INET, *args, **kwargs):
            if family != socket.AF_UNIX:
                raise RuntimeError("D-Bus regression forbids Bluetooth and network sockets")
            super().__init__(family, *args, **kwargs)

        def connect(self, address):
            if address != endpoint:
                raise RuntimeError("only the temporary test bus is allowed")
            return super().connect(address)

        def connect_ex(self, address):
            if address != endpoint:
                raise RuntimeError("only the temporary test bus is allowed")
            return super().connect_ex(address)

    socket.socket = LocalOnlySocket
    return original


def run_child(endpoint, phone_file):
    local_socket_guard(endpoint)
    os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = "unix:path=" + endpoint

    class FixtureBackend(release.Backend):
        async def objects(self):
            return {
                "/org/bluez/hci0": {release.ADAPTER: {"Alias": "Test Computer", "UUIDs": []}},
                "/target": {release.DEVICE: {"Address": PHONE, "Adapter": "/org/bluez/hci0",
                    "Paired": True, "Bonded": True, "Trusted": False, "Blocked": False}},
            }

        async def register(self, *_):
            # 刷新真实总线上的对象导出消息；不调用任何 BlueZ 注册接口。
            await self.dbus("GetId")

    release.Backend = FixtureBackend
    sys.argv = [release.__file__, "--provider", "--phone-file", phone_file, "--adapter", "hci0"]
    return release.main()


class PrivateBusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hid-release-dbus-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.endpoint = str(self.root / "bus")
        original = local_socket_guard(self.endpoint)
        self.addCleanup(setattr, socket, "socket", original)
        config = self.root / "bus.conf"
        config.write_text(
            '<busconfig><type>session</type><listen>unix:path=' + self.endpoint + '</listen>'
            '<auth>EXTERNAL</auth><policy context="default"><allow own="*"/>'
            '<allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>'
        )
        self.daemon = await asyncio.create_subprocess_exec(
            "dbus-daemon", "--nofork", "--config-file=" + str(config), "--print-address=1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        self.addAsyncCleanup(self.stop_process, self.daemon)
        address = (await asyncio.wait_for(self.daemon.stdout.readline(), 3)).decode().strip()
        if not address.startswith("unix:path=" + self.endpoint):
            error = (await self.daemon.stderr.read()).decode().replace(str(self.root), "<test-directory>")
            self.fail("private D-Bus startup failed: " + error)
        self.bus = await asyncio.wait_for(MessageBus(bus_address=address).connect(), 3)
        self.addAsyncCleanup(self.close_bus, self.bus)
        # 仅在私有总线上提供 BlueZ 的身份，真实系统 BlueZ 不参与测试。
        await self.bus.request_name("org.bluez")

    async def stop_process(self, process):
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close_bus(self, bus):
        bus.disconnect()
        await asyncio.wait_for(bus.wait_for_disconnect(), 2)

    async def call(self, member, signature="", body=None, *, destination="org.freedesktop.DBus",
                   path="/org/freedesktop/DBus", interface="org.freedesktop.DBus"):
        reply = await asyncio.wait_for(self.bus.call(Message(
            destination=destination, path=path, interface=interface, member=member,
            signature=signature, body=body or [],
        )), 3)
        self.assertEqual(reply.message_type, MessageType.METHOD_RETURN, reply.error_name)
        return reply.body

    async def test_provider_exits_cleanly_after_real_gatt_read_and_pipe_eof(self):
        phone_file = self.root / "phone-address"
        phone_file.write_text(PHONE + "\n")
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                child = await asyncio.create_subprocess_exec(
                    sys.executable, "-B", str(Path(__file__).resolve()), "--provider-child",
                    self.endpoint, str(phone_file), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                self.addAsyncCleanup(self.stop_process, child)
                events = []
                async with asyncio.timeout(5):
                    while True:
                        line = await child.stdout.readline()
                        self.assertTrue(line, "provider exited before registration")
                        event = json.loads(line)
                        events.append(event)
                        if event["event"] == "registered":
                            owner = event["owner"]
                            break
                self.assertEqual(await self.call("NameHasOwner", "s", [owner]), [True])
                objects = (await self.call("GetManagedObjects", destination=owner,
                    path=release.APP_PATH, interface="org.freedesktop.DBus.ObjectManager"))[0]
                self.assertTrue(any("org.bluez.GattService1" in item for item in objects.values()))
                await self.call("ReadValue", "a{sv}", [{"device": Variant("o", "/target"),
                    "link": Variant("s", "LE")}], destination=owner,
                    path=release.APP_PATH + "/service0/char1", interface="org.bluez.GattCharacteristic1")
                child.stdin.close()
                tail, error_output = await asyncio.wait_for(child.communicate(), 5)
                events.extend(json.loads(line) for line in tail.splitlines())
                self.assertEqual(child.returncode, 0, events)
                self.assertEqual(error_output, b"")
                self.assertIn("hid_read", [event["event"] for event in events])
                closed = next(event for event in events if event["event"] == "provider_closed")
                self.assertIs(closed["dbus_closed"], True)
                self.assertEqual(await self.call("NameHasOwner", "s", [owner]), [False])

    async def reproduce_old_cleanup(self):
        bus = await MessageBus(bus_address="unix:path=" + self.endpoint).connect()
        self.addAsyncCleanup(self.close_bus, bus)
        app = release.HidApplication(bus, release.APP_PATH, lambda *_: None, lambda _: True)
        ad = release.Advertising("Test Computer", lambda *_: None)
        app.export()
        ad.export(bus, release.AD_PATH)
        # 先清空写队列，走与正常就绪后退出相同的即时发送路径。
        await asyncio.wait_for(bus.call(Message(destination="org.freedesktop.DBus",
            path="/org/freedesktop/DBus", interface="org.freedesktop.DBus", member="GetId")), 3)
        try:
            bus.disconnect()
            ad.unexport()
            app.unexport()
        except Exception as error:
            print(json.dumps({"old_cleanup_error": type(error).__name__, "errno": getattr(error, "errno", None)}))
            self.assertIsInstance(error, OSError)
            self.assertEqual(error.errno, errno.EBADF)
        else:
            self.fail("old cleanup did not reproduce the expected failure")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--provider-child":
        raise SystemExit(run_child(sys.argv[2], sys.argv[3]))
    if "--reproduce-old" in sys.argv:
        unittest.main(argv=[sys.argv[0], "PrivateBusTests.reproduce_old_cleanup"])
    else:
        unittest.main()
