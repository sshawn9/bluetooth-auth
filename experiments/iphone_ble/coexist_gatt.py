"""BlueZ D-Bus HOGP objects for a non-exclusive local peripheral experiment.

This module only describes and exports D-Bus objects.  It never opens an HCI
socket, changes an adapter property, connects to a bus, or registers the
objects with BlueZ.  The caller owns those operations and their cleanup.
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
REPORT_TYPE_INPUT = 1
HID_APPEARANCE = 0x03C0
ADVERTISING_INTERVAL_MS = 20

# Consumer Control, report ID 1, eight one-bit controls.  This peripheral
# exposes the corresponding input report as zero and deliberately never emits
# a report Value change or notification.
CONSUMER_CONTROL_REPORT_MAP = bytes.fromhex(
    "050C0901A10185011500250175019508"
    "09CD09B509B609B709E909EA09E20940"
    "8102C0"
)


def _error(name: str, text: str) -> DBusError:
    return DBusError(f"org.bluez.Error.{name}", text)


def _offset(options: dict[str, Variant]) -> int:
    value = options.get("offset")
    offset = value.value if isinstance(value, Variant) else value
    if offset is None:
        return 0
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise _error("InvalidOffset", "offset must be a non-negative integer")
    return offset


class _ObjectManager(ServiceInterface):
    def __init__(self, objects: Callable[[], dict[str, dict[str, dict[str, Variant]]]]):
        super().__init__(OBJECT_MANAGER)
        self._objects = objects

    @dbus_method()
    def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
        return self._objects()


class _GattObject(ServiceInterface):
    def __init__(self, name: str, path: str, on_event: Callable[[str, dict], None], allow_device: Callable[[str], bool]):
        super().__init__(name)
        self.path = path
        self._on_event = on_event
        self._allow_device = allow_device

    def _access(self, options: dict[str, Variant], attribute: str) -> dict[str, Any]:
        raw_device = options.get("device")
        device = raw_device.value if isinstance(raw_device, Variant) else raw_device
        if not isinstance(device, str) or not self._allow_device(device):
            raise _error("NotAuthorized", "GATT access is not allowed for this device")
        raw_link = options.get("link")
        link = raw_link.value if isinstance(raw_link, Variant) else raw_link
        if not isinstance(link, str) or link.lower() != "le":
            raise _error("NotAuthorized", "GATT access is only allowed over LE")
        return {"device": device, "link": link, "attribute": attribute}


class _Service(_GattObject):
    def __init__(self, path: str, uuid: str, on_event: Callable[[str, dict], None], allow_device: Callable[[str], bool]):
        super().__init__(GATT_SERVICE, path, on_event, allow_device)
        self.uuid = uuid

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s": return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b": return True

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "ao": return []


class _Characteristic(_GattObject):
    def __init__(self, path: str, service: _Service, uuid: str, value: bytes, flags: list[str], attribute: str, on_event: Callable[[str, dict], None], allow_device: Callable[[str], bool], writable: bool = False):
        super().__init__(GATT_CHARACTERISTIC, path, on_event, allow_device)
        self.service, self.uuid, self.value = service, uuid, value
        self.flags, self.attribute, self.writable, self.notifying = flags, attribute, writable, False

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s": return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o": return self.service.path

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b": return self.notifying

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as": return self.flags

    def read_value(self, options: dict[str, Variant]) -> bytes:
        event = self._access(options, self.attribute)
        offset = _offset(options)
        if offset > len(self.value):
            raise _error("InvalidOffset", "offset exceeds value length")
        if self.attribute in {"report_map", "report"}:
            self._on_event("target_gatt_access", event)
        return self.value[offset:]

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay": return self.read_value(options)

    def write_value(self, value: bytes, options: dict[str, Variant]) -> None:
        if not self.writable:
            raise _error("NotPermitted", "characteristic is not writable")
        self._access(options, self.attribute)
        if _offset(options) != 0:
            raise _error("InvalidOffset", "writes must start at offset zero")
        # HID Control Point accepts Suspend (0) and Exit Suspend (1), but has
        # no input side effect in this passive experiment.
        if self.attribute == "hid_control_point" and value not in (b"\x00", b"\x01"):
            raise _error("InvalidValueLength", "HID control point accepts one byte")

    @dbus_method()
    def WriteValue(self, value: "ay", options: "a{sv}"):
        self.write_value(value, options)

    @dbus_method()
    def StartNotify(self):
        if "notify" not in self.flags:
            raise _error("NotSupported", "notifications are unsupported")
        self.notifying = True
        self.emit_properties_changed({"Notifying": True})
        self._on_event("gatt_subscription", {"attribute": self.attribute, "scope": "global"})

    @dbus_method()
    def StopNotify(self):
        if "notify" not in self.flags:
            raise _error("NotSupported", "notifications are unsupported")
        self.notifying = False
        self.emit_properties_changed({"Notifying": False})


class _Descriptor(_GattObject):
    def __init__(self, path: str, characteristic: _Characteristic, uuid: str, value: bytes, flags: list[str], on_event: Callable[[str, dict], None], allow_device: Callable[[str], bool]):
        super().__init__(GATT_DESCRIPTOR, path, on_event, allow_device)
        self.characteristic, self.uuid, self.value, self.flags = characteristic, uuid, value, flags

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s": return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Characteristic(self) -> "o": return self.characteristic.path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as": return self.flags

    def read_value(self, options: dict[str, Variant]) -> bytes:
        self._access(options, "report_reference")
        offset = _offset(options)
        if offset > len(self.value):
            raise _error("InvalidOffset", "offset exceeds value length")
        return self.value[offset:]

    @dbus_method()
    def ReadValue(self, options: "a{sv}") -> "ay": return self.read_value(options)


class HidApplication:
    """Exportable HOGP, Battery, and Device Information object hierarchy."""
    def __init__(
        self,
        bus: Any,
        root_path: str,
        on_event: Callable[[str, dict], None],
        allow_device: Callable[[str], bool],
        *,
        include_dis: bool = True,
        include_battery: bool = True,
    ):
        if not root_path.startswith("/"):
            raise ValueError("root_path must be an absolute D-Bus object path")
        if type(include_dis) is not bool or type(include_battery) is not bool:
            raise TypeError("include_dis and include_battery must be bool")
        self.bus, self.root_path = bus, root_path.rstrip("/") or "/"
        self._on_event, self._allow_device, self._exported = on_event, allow_device, False
        self._exported_items: list[tuple[str, ServiceInterface]] = []
        root = self.root_path
        self.hid = _Service(f"{root}/service0", HID_UUID, on_event, allow_device)
        self.battery = _Service(f"{root}/service1", BATTERY_UUID, on_event, allow_device)
        self.dis = _Service(f"{root}/service2", DIS_UUID, on_event, allow_device)
        encrypted_read = ["read", "encrypt-read"]
        encrypted_write = ["write-without-response", "encrypt-write"]
        self.hid_info = _Characteristic(f"{root}/service0/char0", self.hid, "2a4a", b"\x11\x01\x00\x02", encrypted_read, "hid_information", on_event, allow_device)
        self.report_map = _Characteristic(f"{root}/service0/char1", self.hid, "2a4b", CONSUMER_CONTROL_REPORT_MAP, encrypted_read, "report_map", on_event, allow_device)
        self.control_point = _Characteristic(f"{root}/service0/char2", self.hid, "2a4c", b"\x00", encrypted_write, "hid_control_point", on_event, allow_device, writable=True)
        self.report = _Characteristic(f"{root}/service0/char3", self.hid, "2a4d", b"\x00", [*encrypted_read, "notify"], "report", on_event, allow_device)
        self.report_reference = _Descriptor(f"{root}/service0/char3/desc0", self.report, "2908", bytes((1, REPORT_TYPE_INPUT)), encrypted_read, on_event, allow_device)
        self.battery_level = _Characteristic(f"{root}/service1/char0", self.battery, "2a19", b"\x64", [*encrypted_read, "notify"], "battery_level", on_event, allow_device)
        self.manufacturer = _Characteristic(f"{root}/service2/char0", self.dis, "2a29", b"Bluetooth Auth", ["read"], "manufacturer", on_event, allow_device)
        self.model = _Characteristic(f"{root}/service2/char1", self.dis, "2a24", b"Passive Consumer Control", ["read"], "model", on_event, allow_device)
        self.pnp_id = _Characteristic(f"{root}/service2/char2", self.dis, "2a50", b"\x02\x00\x00\x01\x00\x01\x00", ["read"], "pnp_id", on_event, allow_device)
        self.objects: list[ServiceInterface] = [
            self.hid, self.hid_info, self.report_map, self.control_point,
            self.report, self.report_reference,
        ]
        if include_battery:
            self.objects.extend((self.battery, self.battery_level))
        if include_dis:
            self.objects.extend((self.dis, self.manufacturer, self.model, self.pnp_id))
        self.manager = _ObjectManager(self.managed_objects)

    def managed_objects(self) -> dict[str, dict[str, dict[str, Variant]]]:
        result: dict[str, dict[str, dict[str, Variant]]] = {}
        for interface in self.objects:
            result[interface.path] = {interface.name: self._properties(interface)}  # type: ignore[attr-defined]
        return result

    @staticmethod
    def _properties(interface: ServiceInterface) -> dict[str, Variant]:
        return {prop.name: Variant(prop.signature, prop.prop_getter(interface)) for prop in ServiceInterface._get_properties(interface)}

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
        self._on_event("gatt_exported", {"root_path": self.root_path})

    def unexport(self) -> None:
        if not self._exported:
            return
        for path, interface in reversed(self._exported_items):
            self.bus.unexport(path, interface)
        self._exported_items.clear()
        self._exported = False
        self._on_event("gatt_unexported", {"root_path": self.root_path})


class Advertising(ServiceInterface):
    """Exportable legacy peripheral advertisement; registration is caller-owned."""
    def __init__(self, alias: str, on_event: Callable[[str, dict], None]):
        super().__init__(ADVERTISEMENT)
        if not alias or len(alias.encode("utf-8")) > 248:
            raise ValueError("alias must be non-empty and no more than 248 UTF-8 bytes")
        self.alias, self._on_event, self._bus, self._path = alias, on_event, None, None

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s": return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def ServiceUUIDs(self) -> "as": return [HID_UUID]

    @dbus_property(access=PropertyAccess.READ)
    def Appearance(self) -> "q": return HID_APPEARANCE

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s": return self.alias

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b": return True

    @dbus_property(access=PropertyAccess.READ)
    def MinInterval(self) -> "u": return ADVERTISING_INTERVAL_MS

    @dbus_property(access=PropertyAccess.READ)
    def MaxInterval(self) -> "u": return ADVERTISING_INTERVAL_MS

    @dbus_method()
    def Release(self):
        self._on_event("advertisement_released", {"path": self._path or ""})

    def export(self, bus: Any, path: str) -> None:
        if self._bus is not None:
            raise RuntimeError("advertisement is already exported")
        bus.export(path, self)
        self._bus, self._path = bus, path
        self._on_event("advertisement_exported", {"path": path})

    def unexport(self) -> None:
        if self._bus is not None and self._path is not None:
            self._bus.unexport(self._path, self)
            self._on_event("advertisement_unexported", {"path": self._path})
        self._bus, self._path = None, None
