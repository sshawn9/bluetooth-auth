"""持有 HID 服务；每次认证请求重新核对目标手机的连接。"""

from __future__ import annotations

import asyncio

from dbus_fast import BusType, Message, MessageFlag, MessageType
from dbus_fast.aio import MessageBus
from dbus_fast.constants import NameFlag, RequestNameReply

from .hid import Advertisement, HidApplication
from .link import BT_CONNECTED, HCI_LE_LINK, LinkError, LinkReader

BLUEZ = "org.bluez"
DBUS = "org.freedesktop.DBus"
PROPERTIES = DBUS + ".Properties"
OBJECT_MANAGER = DBUS + ".ObjectManager"
ADAPTER = BLUEZ + ".Adapter1"
DEVICE = BLUEZ + ".Device1"
GATT_MANAGER = BLUEZ + ".GattManager1"
AD_MANAGER = BLUEZ + ".LEAdvertisingManager1"
BUS_NAME = "org.bluetooth_auth.Hid"
API_PATH = "/org/bluetooth_auth/control"
API_INTERFACE = BUS_NAME
ROOT = "/org/bluetooth_auth/hid"
APP_PATH = ROOT + "/app"
AD_PATH = ROOT + "/advertisement"


class BackendError(RuntimeError):
    """固定错误信息，不携带设备地址或原始 D-Bus 错误正文。"""


class BlueZBackend:
    def __init__(
        self,
        address,
        adapter="hci0",
        call_timeout=2,
        *,
        link_reader=None,
        bus_factory=None,
    ):
        self.address = address
        self.adapter_index = int(adapter[3:])
        self.adapter_path = "/org/bluez/" + adapter
        self.call_timeout = call_timeout
        self.reader = link_reader if link_reader is not None else LinkReader()
        self.bus_factory = bus_factory or (lambda: MessageBus(bus_type=BusType.SYSTEM))
        self.bus = self.owner = self.target_path = None
        self.app = self.advertisement = self._watcher = None
        self.failed = asyncio.Event()
        self.changed = asyncio.Event()
        self.disconnect_count = self.revision = 0
        self._closing = self._handler_added = False
        # 仅约束 GATT 访问；认证请求仍重新读取 BlueZ 和内核。
        self.phone = {}

    def _fail(self):
        if not self._closing:
            self.failed.set()
            self.changed.set()

    def _check(self):
        if self.bus is None or self.failed.is_set() or self._closing:
            raise BackendError("蓝牙后端不可用")

    async def _call(
        self, path, interface, member, signature="", body=None, *, bus_daemon=False
    ):
        self._check()
        destination = DBUS if bus_daemon else self.owner
        try:
            async with asyncio.timeout(self.call_timeout):
                reply = await self.bus.call(
                    Message(
                        destination=destination,
                        path=path,
                        interface=interface,
                        member=member,
                        signature=signature,
                        body=body or [],
                        flags=MessageFlag.NO_AUTOSTART,
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BackendError("BlueZ 调用失败或超时") from None
        self._check()
        if (
            reply is None
            or reply.sender != destination
            or reply.message_type != MessageType.METHOD_RETURN
        ):
            raise BackendError("BlueZ 拒绝了操作")
        return reply.body

    async def _dbus(self, member, signature="", body=None):
        return await self._call(
            "/org/freedesktop/DBus", DBUS, member, signature, body, bus_daemon=True
        )

    def _valid_phone(self, phone):
        return (
            str(phone.get("Address", "")).upper() == self.address
            and phone.get("Adapter") == self.adapter_path
            and all(phone.get(key) is True for key in ("Paired", "Bonded", "Trusted"))
            and phone.get("Blocked") is not True
        )

    def _allow_device(self, path):
        return (
            not self._closing
            and not self.failed.is_set()
            and path == self.target_path
            and self._valid_phone(self.phone)
        )

    def _message(self, message):
        if message.message_type == MessageType.METHOD_CALL and (
            message.path == ROOT or (message.path or "").startswith(ROOT + "/")
        ):
            if message.sender != self.owner or self.failed.is_set() or self._closing:
                return Message.new_error(
                    message, DBUS + ".Error.AccessDenied", "Only BlueZ is allowed"
                )
            return None
        if message.message_type != MessageType.SIGNAL:
            return None
        if (
            message.sender == DBUS
            and message.interface == DBUS
            and message.member == "NameOwnerChanged"
            and len(message.body) == 3
            and message.body[0] == BLUEZ
            and message.body[2] != self.owner
        ):
            self._fail()
        if message.sender != self.owner:
            return None
        try:
            if (
                message.interface == PROPERTIES
                and message.member == "PropertiesChanged"
            ):
                interface, changes, invalid = message.body
                if message.path == self.target_path and interface == DEVICE:
                    was_connected = self.phone.get("Connected") is True
                    self.phone.update(
                        {key: value.value for key, value in changes.items()}
                    )
                    for key in invalid:
                        self.phone.pop(key, None)
                    if was_connected and self.phone.get("Connected") is False:
                        self.disconnect_count += 1
                    self.revision += 1
                    self.changed.set()
                if message.path == self.adapter_path and interface == ADAPTER:
                    self.revision += 1
                    if "Powered" in invalid or (
                        "Powered" in changes and changes["Powered"].value is not True
                    ):
                        self._fail()
            elif (
                message.interface == OBJECT_MANAGER
                and message.member == "InterfacesRemoved"
            ):
                path, interfaces = message.body
                if path == self.target_path and DEVICE in interfaces:
                    self.phone.clear()
                    self.disconnect_count += 1
                    self.revision += 1
                    self.changed.set()
                if path == self.adapter_path:
                    self._fail()
            elif (
                message.path == self.target_path
                and message.interface == DEVICE
                and message.member == "Disconnected"
            ):
                self.disconnect_count += 1
                self.revision += 1
                self.changed.set()
        except (TypeError, ValueError, AttributeError):
            self._fail()
        return None

    async def _watch_disconnect(self, bus):
        try:
            await bus.wait_for_disconnect()
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        self._fail()

    async def open(self):
        try:
            try:
                self.bus = self.bus_factory()
                async with asyncio.timeout(self.call_timeout):
                    await self.bus.connect()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise BackendError("无法连接系统 D-Bus") from None
            named = await self._dbus(
                "RequestName", "su", [BUS_NAME, int(NameFlag.DO_NOT_QUEUE)]
            )
            if named != [RequestNameReply.PRIMARY_OWNER.value]:
                raise BackendError("daemon 服务名已被占用，或没有注册权限")
            self.owner = (await self._dbus("GetNameOwner", "s", [BLUEZ]))[0]
            self.bus.add_message_handler(self._message)
            self._handler_added = True
            for rule in (
                "type='signal',sender='org.bluez',path_namespace='/org/bluez'",
                "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'",
            ):
                await self._dbus("AddMatch", "s", [rule])
            if (await self._dbus("GetNameOwner", "s", [BLUEZ]))[0] != self.owner:
                raise BackendError("BlueZ 在启动过程中退出")
            raw = (await self._call("/", OBJECT_MANAGER, "GetManagedObjects"))[0]
            objects = {
                path: {
                    name: {key: value.value for key, value in props.items()}
                    for name, props in interfaces.items()
                }
                for path, interfaces in raw.items()
            }
            managers = objects.get(self.adapter_path, {})
            adapter = managers.get(ADAPTER, {})
            if (
                adapter.get("Powered") is not True
                or GATT_MANAGER not in managers
                or AD_MANAGER not in managers
            ):
                raise BackendError("适配器未开启或不支持所需服务")
            targets = [
                (path, interfaces[DEVICE])
                for path, interfaces in objects.items()
                if DEVICE in interfaces and self._valid_phone(interfaces[DEVICE])
            ]
            if len(targets) != 1:
                raise BackendError("没有找到唯一的已配对、已保存且受信任的目标手机")
            self.target_path, self.phone = targets[0]
            uuids = {
                value.lower().split("-", 1)[0].lstrip("0")
                for value in adapter.get("UUIDs", [])
            }
            if "1812" in uuids:
                raise BackendError("适配器已经提供 HID 服务")
            self.app = HidApplication(
                self.bus,
                APP_PATH,
                self._allow_device,
                include_dis="180a" not in uuids,
                include_battery="180f" not in uuids,
            )
            self.advertisement = Advertisement(adapter.get("Alias", ""), self._fail)
            self.app.export()
            self.advertisement.export(self.bus, AD_PATH)
            await self._call(
                self.adapter_path,
                GATT_MANAGER,
                "RegisterApplication",
                "oa{sv}",
                [APP_PATH, {}],
            )
            await self._call(
                self.adapter_path,
                AD_MANAGER,
                "RegisterAdvertisement",
                "oa{sv}",
                [AD_PATH, {}],
            )
            self._watcher = asyncio.create_task(self._watch_disconnect(self.bus))
            return self
        except BaseException:
            await self.close()
            raise

    async def connected(self) -> bool:
        revision = self.revision
        adapter = (
            await self._call(self.adapter_path, PROPERTIES, "GetAll", "s", [ADAPTER])
        )[0]
        phone = (
            await self._call(self.target_path, PROPERTIES, "GetAll", "s", [DEVICE])
        )[0]
        phone = {key: value.value for key, value in phone.items()}
        if (
            adapter.get("Powered") is None
            or adapter["Powered"].value is not True
            or not self._valid_phone(phone)
        ):
            raise BackendError("适配器或手机身份状态无效")
        try:
            links = self.reader.read(self.adapter_index)
        except LinkError:
            raise BackendError("无法查询内核蓝牙链路") from None
        self._check()
        if revision != self.revision:
            raise BackendError("查询期间连接状态发生变化")
        self.phone = phone
        matches = [
            link
            for link in links
            if link.address == self.address
            and link.link_type == HCI_LE_LINK
            and link.state == BT_CONNECTED
        ]
        if len(matches) > 1:
            raise BackendError("无法确认唯一的目标链路")
        # RPA 无法精确关联时返回未连接，不按附近设备数量猜测身份。
        return bool(matches) and phone.get("Connected") is True and matches[0].encrypted

    async def wait_change(self, timeout):
        self._check()
        try:
            await asyncio.wait_for(self.changed.wait(), timeout)
        except TimeoutError:
            pass
        self.changed.clear()
        self._check()

    async def close(self):
        if self._closing:
            return
        self._closing = True
        self.changed.set()
        bus, self.bus = self.bus, None
        if bus is None:
            return
        # 总线连接关闭后，BlueZ 自动注销本进程对象；原配对和其他连接不动。
        bus.disconnect()
        try:
            async with asyncio.timeout(self.call_timeout):
                if self._watcher is not None:
                    await asyncio.shield(self._watcher)
                else:
                    await bus.wait_for_disconnect()
        except Exception:
            if self._watcher is not None:
                self._watcher.cancel()
                await asyncio.gather(self._watcher, return_exceptions=True)
        if self.advertisement is not None:
            self.advertisement.unexport()
        if self.app is not None:
            self.app.unexport()
        if self._handler_added:
            bus.remove_message_handler(self._message)
