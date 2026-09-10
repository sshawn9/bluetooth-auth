"""独立的 HID 注册、注销、连接和查询函数，不组装认证流程。"""

from __future__ import annotations

import asyncio
import fcntl
import socket
import struct
import sys
from dataclasses import dataclass, field
from functools import partial

from dbus_fast import BusType, Message, MessageFlag, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import PropertyAccess
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property

BLUEZ = "org.bluez"
DBUS = "org.freedesktop.DBus"
ROOT = "/org/bluetooth_auth/hid_device"
APP = ROOT + "/app"
ADVERTISEMENT = ROOT + "/advertisement"
REPORT_MAP = bytes.fromhex(
    "050C0901A10185011500250175019508"
    "09CD09B509B609B709E909EA09E209408102C0"
)


@dataclass
class BluetoothContext:
    """调用方提供有效地址和适配器；同一事件循环内使用，持有 HID 时保持不变。"""

    address: str = field(repr=False)
    adapter: str = "hci0"
    _bus: MessageBus | None = field(default=None, init=False, repr=False)
    _owner: str | None = field(default=None, init=False, repr=False)
    _device_path: str | None = field(default=None, init=False, repr=False)
    _alias: str = field(default="", init=False, repr=False)
    _registered: bool = field(default=False, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)


async def register_hid(ctx: BluetoothContext) -> bool:
    """只注册一份 HID 服务，不启动广播；重复/并发调用不重复注册。"""
    acquired = created = False
    failure = None
    try:
        acquired = await ctx._lock.acquire()
        if ctx._bus is not None and not (
            ctx._bus.connected and ctx._registered and ctx._owner == await _call(
                ctx, "/org/freedesktop/DBus", DBUS, "GetNameOwner", "s", [BLUEZ], destination=DBUS
            )
        ):
            await _close(ctx)
        if ctx._bus is None:
            ctx._bus = MessageBus(bus_type=BusType.SYSTEM)
            created = True
            async with asyncio.timeout(5):
                await ctx._bus.connect()
            ctx._owner = await _call(ctx, "/org/freedesktop/DBus", DBUS,
                                    "GetNameOwner", "s", [BLUEZ], destination=DBUS)
            raw = await _call(ctx, "/", DBUS + ".ObjectManager", "GetManagedObjects")
            objects = {path: {name: {key: value.value for key, value in props.items()}
                             for name, props in interfaces.items()}
                       for path, interfaces in raw.items()}
            adapter_path = "/org/bluez/" + ctx.adapter
            interfaces = objects.get(adapter_path, {})
            adapter = interfaces.get(BLUEZ + ".Adapter1", {})
            if adapter.get("Powered") is not True or any(
                BLUEZ + name not in interfaces for name in (".GattManager1", ".LEAdvertisingManager1")
            ):
                raise RuntimeError("适配器未开启或缺少 HID/广播注册接口")
            phones = [path for path, items in objects.items()
                      if (phone := items.get(BLUEZ + ".Device1"))
                      and phone.get("Address", "").upper() == ctx.address.upper()
                      and phone.get("Adapter") == adapter_path and not phone.get("Blocked")]
            if len(phones) != 1:
                raise RuntimeError("没有找到唯一、未被阻止的目标手机记录")
            ctx._device_path = phones[0]
            if not isinstance(adapter.get("UUIDs"), list):
                raise RuntimeError("无法读取适配器服务列表")
            uuids = {value.lower().split("-", 1)[0].lstrip("0") for value in adapter["UUIDs"]}
            if "1812" in uuids:
                raise RuntimeError("适配器已有其他 HID 服务")

            exports = _gatt_objects(ctx, uuids)
            ctx._bus.add_message_handler(partial(_message, ctx))
            ctx._bus.export(APP, _ObjectManager(exports))
            for path, interface in exports.items():
                ctx._bus.export(path, interface)
            ctx._alias = adapter["Alias"]
            await _call(ctx, adapter_path, BLUEZ + ".GattManager1",
                        "RegisterApplication", "oa{sv}", [APP, {}])
            ctx._registered = True
    except BaseException as error:
        failure = error
    if created and failure is not None:  # 只清理本次注册创建的总线。
        try:
            await _close(ctx)
        except BaseException as error:
            if isinstance(error, Exception):
                failure.add_note(f"注销 HID 失败：{error}")
            else:
                failure = error
    try:
        if acquired:
            ctx._lock.release()
    except Exception as error:
        if failure is None:
            failure = error
        else:
            failure.add_note(f"释放锁失败：{error}")
    if failure is None:
        return True
    if not isinstance(failure, Exception):
        raise failure
    print(f"错误：register_hid：{str(failure) or type(failure).__name__}",
          *getattr(failure, "__notes__", ()), sep="；", file=sys.stderr, flush=True)
    return False


async def unregister_hid(ctx: BluetoothContext) -> bool:
    """只释放本 ctx 的 HID/广播；重复调用无副作用，不断开蓝牙连接。"""
    try:
        async with ctx._lock:
            await _close(ctx)
        return True
    except Exception as error:
        print(f"错误：unregister_hid：{str(error) or type(error).__name__}", file=sys.stderr, flush=True)
        return False


async def query(ctx: BluetoothContext) -> bool:
    """实时读取目标加密 LE；未连接或读取失败均 False，读取失败打印原因。"""
    try:
        return _query(ctx)
    except Exception as error:
        print(f"错误：query：{str(error) or type(error).__name__}", file=sys.stderr, flush=True)
        return False


async def connect(ctx: BluetoothContext, timeout: float = 20) -> bool:
    """已连立即返回；否则基于已注册的 HID 广播一次，结束时停止广播，不重试。"""
    acquired = False
    advertisement, failure = None, None
    try:
        acquired = await ctx._lock.acquire()
        if not 0 < timeout <= 120:
            raise ValueError("连接等待须大于零且不超过 120 秒")
        if not _query(ctx):
            if not ctx._registered or ctx._bus is None or not ctx._bus.connected:
                raise RuntimeError("请先注册 HID 服务")
            advertisement = _Advertisement(ctx._alias)
            ctx._bus.export(ADVERTISEMENT, advertisement)
            async with asyncio.timeout(timeout):
                await _call(ctx, "/org/bluez/" + ctx.adapter, BLUEZ + ".LEAdvertisingManager1",
                            "RegisterAdvertisement", "oa{sv}", [ADVERTISEMENT, {}])
                while not advertisement.released:
                    if _query(ctx):
                        break
                    await asyncio.sleep(0.2)
                else:
                    raise RuntimeError("连接过程中广播被释放")
    except BaseException as error:
        failure = error
    if advertisement is not None:
        try:
            if not advertisement.released:
                await _call(ctx, "/org/bluez/" + ctx.adapter, BLUEZ + ".LEAdvertisingManager1",
                            "UnregisterAdvertisement", "o", [ADVERTISEMENT])
        except BaseException as error:
            if isinstance(error, DBusError) and error.type == BLUEZ + ".Error.DoesNotExist":
                pass
            elif failure is not None and isinstance(error, Exception):
                failure.add_note(f"停止广播失败：{error}")
            else:
                failure = error
        try:
            if ctx._bus.connected:
                ctx._bus.unexport(ADVERTISEMENT)
        except BaseException as error:
            if failure is not None and isinstance(error, Exception):
                failure.add_note(f"移除广播对象失败：{error}")
            else:
                failure = error
    try:
        if acquired:
            ctx._lock.release()
    except Exception as error:
        if failure is None:
            failure = error
        else:
            failure.add_note(f"释放锁失败：{error}")
    if failure is not None:
        if not isinstance(failure, Exception):
            raise failure
        print(f"错误：connect：{str(failure) or type(failure).__name__}",
              *getattr(failure, "__notes__", ()), sep="；", file=sys.stderr, flush=True)
        return False
    return True


def _query(ctx):
    # Linux HCI ioctl ABI；兼容没有 socket.AF_BLUETOOTH 的 Python。
    header, entry, capacity = struct.Struct("=HH"), struct.Struct("=H6sBBHI"), 512
    index = int(ctx.adapter[3:])
    buffer = bytearray(header.size + capacity * entry.size)
    header.pack_into(buffer, 0, index, capacity)
    with socket.socket(31, socket.SOCK_RAW | getattr(socket, "SOCK_CLOEXEC", 0), 1) as sock:
        fcntl.ioctl(sock.fileno(), 0x800448D4, buffer, True)  # HCIGETCONNLIST
    returned_index, count = header.unpack_from(buffer)
    if returned_index != index or count >= capacity or header.size + count * entry.size > len(buffer):
        raise RuntimeError("内核蓝牙连接快照不完整")
    address = bytes.fromhex(ctx.address.replace(":", ""))[::-1]
    matches = [entry.unpack_from(buffer, offset)
               for offset in range(header.size, header.size + count * entry.size, entry.size)]
    matches = [link for link in matches
               if link[1] == address and link[2] == 0x80 and link[4] == 1]
    if len(matches) > 1:
        raise RuntimeError("无法确认唯一的目标 LE 连接")
    return bool(matches) and bool(matches[0][5] & 0x0004)


async def _call(ctx, path, interface, member, signature="", body=None, *, destination=None):
    destination = destination or ctx._owner
    async with asyncio.timeout(5):
        reply = await ctx._bus.call(Message(
            destination=destination, path=path, interface=interface, member=member,
            signature=signature, body=body or [], flags=MessageFlag.NO_AUTOSTART,
        ))
    if reply is not None and reply.sender == destination:
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, f"BlueZ {member} 失败：{reply.error_name}")
        if reply.message_type == MessageType.METHOD_RETURN:
            return reply.body[0] if reply.body else None
    raise RuntimeError(f"BlueZ {member} 未获得有效回复")


async def _close(ctx):
    if ctx._bus is None:
        return
    ctx._registered = False
    ctx._bus.disconnect()
    async with asyncio.timeout(5):
        await ctx._bus.wait_for_disconnect()
    # 保留失败时的句柄供再次注销；不在已关闭的总线上 unexport。
    ctx._bus = ctx._owner = ctx._device_path = None


def _message(ctx, message):
    if (message.message_type == MessageType.METHOD_CALL
            and (message.path == ROOT or (message.path or "").startswith(ROOT + "/"))
            and message.sender != ctx._owner):
        return Message.new_error(message, DBUS + ".Error.AccessDenied", "Only BlueZ is allowed")


def _read(ctx, value, options):
    if (options.get("device", Variant("o", "/")).value != ctx._device_path
            or options.get("link", Variant("s", "")).value.lower() != "le"):
        raise DBusError(BLUEZ + ".Error.NotAuthorized", "Only the target LE device is allowed")
    offset = options.get("offset", Variant("q", 0)).value
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset <= len(value):
        raise DBusError(BLUEZ + ".Error.InvalidOffset", "Invalid offset")
    return value[offset:]


def _gatt_objects(ctx, existing_uuids):
    read = ["read", "encrypt-read"]
    specs = [
        ("1812", [
            ("2a4a", b"\x11\x01\x00\x02", read),
            ("2a4b", REPORT_MAP, read),
            ("2a4c", b"\x00", ["write-without-response", "encrypt-write"]),
            ("2a4d", b"\x00", [*read, "notify"]),
        ]),
        ("180f", [("2a19", b"\x64", [*read, "notify"])]),
        ("180a", [
            ("2a29", b"Bluetooth Auth", ["read"]),
            ("2a24", b"Passive Consumer Control", ["read"]),
            ("2a50", b"\x02\x00\x00\x01\x00\x01\x00", ["read"]),
        ]),
    ]
    objects = {}
    for number, (uuid, characteristics) in enumerate(specs):
        if uuid != "1812" and uuid in existing_uuids:
            continue
        service = f"{APP}/service{number}"
        objects[service] = _Service(uuid)
        for index, (char_uuid, value, flags) in enumerate(characteristics):
            path = f"{service}/char{index}"
            objects[path] = _Characteristic(ctx, service, char_uuid, value, flags)
            if char_uuid == "2a4d":
                objects[path + "/descriptor0"] = _Descriptor(ctx, path)
    return objects


# 以下类仅实现 dbus-fast 所需的协议属性与回调，业务操作均在上方函数中。
def _property(name, signature, getter):
    """统一声明只读 D-Bus 属性。"""
    getter.__name__ = name
    getter.__annotations__["return"] = signature
    return dbus_property(name=name, access=PropertyAccess.READ)(getter)


class _ObjectManager(ServiceInterface):
    def __init__(self, objects):
        super().__init__(DBUS + ".ObjectManager")
        self.objects = objects

    @dbus_method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        return {path: {item.name: {prop.name: Variant(prop.signature, prop.prop_getter(item))
                                  for prop in ServiceInterface._get_properties(item)}}
                for path, item in self.objects.items()}


class _Service(ServiceInterface):
    def __init__(self, uuid):
        super().__init__(BLUEZ + ".GattService1")
        self.uuid = uuid

    UUID = _property("UUID", "s", lambda self: self.uuid)
    Primary = _property("Primary", "b", lambda self: True)
    Includes = _property("Includes", "ao", lambda self: [])


class _Characteristic(ServiceInterface):
    def __init__(self, ctx, service, uuid, value, flags):
        super().__init__(BLUEZ + ".GattCharacteristic1")
        self.ctx, self.service, self.uuid, self.value = ctx, service, uuid, value
        self.flags, self.notifying = flags, False

    UUID = _property("UUID", "s", lambda self: self.uuid)
    Service = _property("Service", "o", lambda self: self.service)
    Flags = _property("Flags", "as", lambda self: self.flags)
    Notifying = _property("Notifying", "b", lambda self: self.notifying)

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        if "read" not in self.flags:
            raise DBusError(BLUEZ + ".Error.NotPermitted", "Not readable")
        return _read(self.ctx, self.value, options)

    @dbus_method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        _read(self.ctx, b"", options)  # 核对目标及零偏移。
        if self.uuid != "2a4c":
            raise DBusError(BLUEZ + ".Error.NotPermitted", "Not writable")
        if value not in (b"\x00", b"\x01"):
            raise DBusError(BLUEZ + ".Error.InvalidValueLength", "Invalid control point")

    @dbus_method()
    def StartNotify(self):
        if "notify" not in self.flags:
            raise DBusError(BLUEZ + ".Error.NotSupported", "Notifications unsupported")
        if not self.notifying:
            self.notifying = True
            self.emit_properties_changed({"Notifying": True})

    @dbus_method()
    def StopNotify(self):
        if self.notifying:
            self.notifying = False
            self.emit_properties_changed({"Notifying": False})


class _Descriptor(ServiceInterface):
    def __init__(self, ctx, characteristic):
        super().__init__(BLUEZ + ".GattDescriptor1")
        self.ctx, self.characteristic = ctx, characteristic

    UUID = _property("UUID", "s", lambda self: "2908")
    Characteristic = _property("Characteristic", "o", lambda self: self.characteristic)
    Flags = _property("Flags", "as", lambda self: ["read", "encrypt-read"])

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return _read(self.ctx, b"\x01\x01", options)


class _Advertisement(ServiceInterface):
    def __init__(self, alias):
        super().__init__(BLUEZ + ".LEAdvertisement1")
        self.alias, self.released = alias, False

    Type = _property("Type", "s", lambda self: "peripheral")
    ServiceUUIDs = _property("ServiceUUIDs", "as", lambda self: ["1812"])
    Appearance = _property("Appearance", "q", lambda self: 0x03C0)
    LocalName = _property("LocalName", "s", lambda self: self.alias)
    Discoverable = _property("Discoverable", "b", lambda self: True)
    MinInterval = _property("MinInterval", "u", lambda self: 20)
    MaxInterval = _property("MaxInterval", "u", lambda self: 20)

    @dbus_method()
    def Release(self):
        self.released = True
