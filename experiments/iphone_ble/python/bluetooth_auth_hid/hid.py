"""可导出的 BlueZ HOGP D-Bus 对象。

本模块只构造和导出对象；调用方负责连接系统总线及向 BlueZ 注册。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from dbus_fast import Variant
from dbus_fast.constants import PropertyAccess
from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, dbus_method, dbus_property


OBJECT_MANAGER = "org.freedesktop.DBus.ObjectManager"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHARACTERISTIC = "org.bluez.GattCharacteristic1"
GATT_DESCRIPTOR = "org.bluez.GattDescriptor1"
ADVERTISEMENT = "org.bluez.LEAdvertisement1"

HID_UUID = "1812"
BATTERY_UUID = "180f"
DIS_UUID = "180a"
HID_APPEARANCE = 0x03C0
ADVERTISING_INTERVAL_MS = 20
REPORT_TYPE_INPUT = 1

# Consumer Control，报告 ID 为 1，输入报告固定为全零。
CONSUMER_CONTROL_REPORT_MAP = bytes.fromhex(
    "050C0901A1018501150025017501950809CD09B509B609B709E909EA09E209408102C0"
)


def _bluez_error(name: str, text: str) -> DBusError:
    return DBusError(f"org.bluez.Error.{name}", text)


def _option(options: dict[str, Variant], key: str, signature: str) -> Any:
    value = options.get(key)
    if not isinstance(value, Variant) or value.signature != signature:
        return None
    return value.value


def _offset(options: dict[str, Variant]) -> int:
    value = _option(options, "offset", "q")
    if "offset" in options and value is None:
        raise _bluez_error("InvalidOffset", "offset 必须是 uint16")
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _bluez_error("InvalidOffset", "offset 必须是非负整数")
    return value


class _ObjectManager(ServiceInterface):
    def __init__(
        self, managed_objects: Callable[[], dict[str, dict[str, dict[str, Variant]]]]
    ):
        super().__init__(OBJECT_MANAGER)
        self._managed_objects = managed_objects

    @dbus_method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        return self._managed_objects()


class _GattObject(ServiceInterface):
    def __init__(self, name: str, path: str, allow_device: Callable[[str], bool]):
        super().__init__(name)
        self.path = path
        self._allow_device = allow_device

    def _require_access(self, options: dict[str, Variant]) -> None:
        device = _option(options, "device", "o")
        if not isinstance(device, str) or not self._allow_device(device):
            raise _bluez_error("NotAuthorized", "此设备无权访问 GATT")
        link = _option(options, "link", "s")
        if not isinstance(link, str) or link.lower() != "le":
            raise _bluez_error("NotAuthorized", "GATT 只允许 LE 连接")


class _Service(_GattObject):
    def __init__(self, path: str, uuid: str, allow_device: Callable[[str], bool]):
        super().__init__(GATT_SERVICE, path, allow_device)
        self.uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "ao":
        return []


class _Characteristic(_GattObject):
    def __init__(
        self,
        path: str,
        service: _Service,
        uuid: str,
        value: bytes,
        flags: list[str],
        allow_device: Callable[[str], bool],
        *,
        writable: bool = False,
    ):
        super().__init__(GATT_CHARACTERISTIC, path, allow_device)
        self.service, self.uuid, self.value = service, uuid, value
        self.flags, self.writable, self.notifying = flags, writable, False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":
        return self.service.path

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":
        return self.notifying

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return self.flags

    def read_value(self, options: dict[str, Variant]) -> bytes:
        self._require_access(options)
        offset = _offset(options)
        if offset > len(self.value):
            raise _bluez_error("InvalidOffset", "offset 超出值长度")
        return self.value[offset:]

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return self.read_value(options)

    def write_value(self, value: bytes, options: dict[str, Variant]) -> None:
        if not self.writable:
            raise _bluez_error("NotPermitted", "特征不可写")
        self._require_access(options)
        if _offset(options) != 0:
            raise _bluez_error("InvalidOffset", "写入必须从 offset 0 开始")
        if value not in (b"\x00", b"\x01"):
            raise _bluez_error("InvalidValueLength", "HID 控制点只接受一个字节")

    @dbus_method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        self.write_value(value, options)

    @dbus_method()
    def StartNotify(self):
        if "notify" not in self.flags:
            raise _bluez_error("NotSupported", "不支持通知")
        self.notifying = True
        self.emit_properties_changed({"Notifying": True})

    @dbus_method()
    def StopNotify(self):
        if "notify" not in self.flags:
            raise _bluez_error("NotSupported", "不支持通知")
        self.notifying = False
        self.emit_properties_changed({"Notifying": False})


class _Descriptor(_GattObject):
    def __init__(
        self,
        path: str,
        characteristic: _Characteristic,
        value: bytes,
        allow_device: Callable[[str], bool],
    ):
        super().__init__(GATT_DESCRIPTOR, path, allow_device)
        self.characteristic, self.value = characteristic, value

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":
        return "2908"

    @dbus_property(access=PropertyAccess.READ)
    def Characteristic(self) -> "o":
        return self.characteristic.path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":
        return ["read", "encrypt-read"]

    def read_value(self, options: dict[str, Variant]) -> bytes:
        self._require_access(options)
        offset = _offset(options)
        if offset > len(self.value):
            raise _bluez_error("InvalidOffset", "offset 超出值长度")
        return self.value[offset:]

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay":
        return self.read_value(options)


class HidApplication:
    """一组稳定的、可由 BlueZ 注册的 HOGP 对象。"""

    def __init__(
        self,
        bus: Any,
        root_path: str,
        allow_device: Callable[[str], bool],
        *,
        include_dis: bool = True,
        include_battery: bool = True,
    ):
        if not isinstance(root_path, str) or not root_path.startswith("/"):
            raise ValueError("root_path 必须是绝对 D-Bus 对象路径")
        if type(include_dis) is not bool or type(include_battery) is not bool:
            raise TypeError("include_dis 和 include_battery 必须为 bool")
        self.bus = bus
        self.root_path = root_path.rstrip("/") or "/"
        self._exported = False
        self._exported_items: list[tuple[str, ServiceInterface]] = []

        root = self.root_path
        self.hid = _Service(f"{root}/service0", HID_UUID, allow_device)
        self.battery = _Service(f"{root}/service1", BATTERY_UUID, allow_device)
        self.dis = _Service(f"{root}/service2", DIS_UUID, allow_device)
        encrypted_read = ["read", "encrypt-read"]
        encrypted_write = ["write-without-response", "encrypt-write"]
        self.hid_info = _Characteristic(
            f"{root}/service0/char0",
            self.hid,
            "2a4a",
            b"\x11\x01\x00\x02",
            encrypted_read,
            allow_device,
        )
        self.report_map = _Characteristic(
            f"{root}/service0/char1",
            self.hid,
            "2a4b",
            CONSUMER_CONTROL_REPORT_MAP,
            encrypted_read,
            allow_device,
        )
        self.control_point = _Characteristic(
            f"{root}/service0/char2",
            self.hid,
            "2a4c",
            b"\x00",
            encrypted_write,
            allow_device,
            writable=True,
        )
        self.report = _Characteristic(
            f"{root}/service0/char3",
            self.hid,
            "2a4d",
            b"\x00",
            [*encrypted_read, "notify", "encrypt-notify"],
            allow_device,
        )
        self.report_reference = _Descriptor(
            f"{root}/service0/char3/desc0",
            self.report,
            bytes((1, REPORT_TYPE_INPUT)),
            allow_device,
        )
        self.battery_level = _Characteristic(
            f"{root}/service1/char0",
            self.battery,
            "2a19",
            b"\x64",
            ["read", "encrypt-read", "notify", "encrypt-notify"],
            allow_device,
        )
        self.manufacturer = _Characteristic(
            f"{root}/service2/char0",
            self.dis,
            "2a29",
            b"Bluetooth Auth",
            ["read"],
            allow_device,
        )
        self.model = _Characteristic(
            f"{root}/service2/char1",
            self.dis,
            "2a24",
            b"Passive Consumer Control",
            ["read"],
            allow_device,
        )
        self.pnp_id = _Characteristic(
            f"{root}/service2/char2",
            self.dis,
            "2a50",
            b"\x02\x00\x00\x01\x00\x01\x00",
            ["read"],
            allow_device,
        )
        self.objects: list[ServiceInterface] = [
            self.hid,
            self.hid_info,
            self.report_map,
            self.control_point,
            self.report,
            self.report_reference,
        ]
        if include_battery:
            self.objects.extend((self.battery, self.battery_level))
        if include_dis:
            self.objects.extend((self.dis, self.manufacturer, self.model, self.pnp_id))
        self.manager = _ObjectManager(self.managed_objects)

    @staticmethod
    def _properties(interface: ServiceInterface) -> dict[str, Variant]:
        return {
            prop.name: Variant(prop.signature, prop.prop_getter(interface))
            for prop in ServiceInterface._get_properties(interface)
        }

    def managed_objects(self) -> dict[str, dict[str, dict[str, Variant]]]:
        return {
            interface.path: {interface.name: self._properties(interface)}  # type: ignore[attr-defined]
            for interface in self.objects
        }

    def export(self) -> None:
        if self._exported:
            return
        try:
            self.bus.export(self.root_path, self.manager)
            self._exported_items.append((self.root_path, self.manager))
            for interface in self.objects:
                self.bus.export(interface.path, interface)  # type: ignore[attr-defined]
                self._exported_items.append((interface.path, interface))  # type: ignore[attr-defined]
        except BaseException:
            for path, interface in reversed(self._exported_items):
                self.bus.unexport(path, interface)
            self._exported_items.clear()
            raise
        self._exported = True

    def unexport(self) -> None:
        if not self._exported:
            return
        for path, interface in reversed(self._exported_items):
            self.bus.unexport(path, interface)
        self._exported_items.clear()
        self._exported = False


class Advertisement(ServiceInterface):
    """固定 HID 参数的可导出广播对象。"""

    def __init__(self, alias: str, on_release: Callable[[], None]):
        super().__init__(ADVERTISEMENT)
        if not alias or len(alias.encode("utf-8")) > 248:
            raise ValueError("alias 必须非空且最多 248 个 UTF-8 字节")
        self.alias = alias
        self._on_release = on_release
        self._bus: Any | None = None
        self._path: str | None = None

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as":
        return [HID_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def Appearance(self) -> "q":
        return HID_APPEARANCE

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self.alias

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def MinInterval(self) -> "u":
        return ADVERTISING_INTERVAL_MS

    @dbus_property(access=PropertyAccess.READ)
    def MaxInterval(self) -> "u":
        return ADVERTISING_INTERVAL_MS

    @dbus_method()
    def Release(self):
        self._on_release()

    def export(self, bus: Any, path: str) -> None:
        if self._bus is not None:
            raise RuntimeError("advertisement 已导出")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("path 必须是绝对 D-Bus 对象路径")
        bus.export(path, self)
        self._bus, self._path = bus, path

    def unexport(self) -> None:
        if self._bus is not None and self._path is not None:
            self._bus.unexport(self._path, self)
        self._bus, self._path = None, None


__all__ = ["Advertisement", "HidApplication"]
