"""直接注册本机 HID 服务。"""

from dbus_fast import BusType
from dbus_fast.aio import MessageBus

from .hid import _Characteristic, _Descriptor, _Service


async def register_hid() -> MessageBus:
    """在 hci0 注册 HID，返回持有这些服务的 D-Bus 连接。"""
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    root = "/org/bluetooth_auth/hid_device/app"
    read = ["read", "encrypt-read"]
    report_map = bytes.fromhex(
        "050C0901A10185011500250175019508"
        "09CD09B509B609B709E909EA09E209408102C0"
    )

    hid = _Service(root + "/service0", "1812", bool)
    battery = _Service(root + "/service1", "180f", bool)
    device_info = _Service(root + "/service2", "180a", bool)
    report = _Characteristic(
        hid.path + "/char3", hid, "2a4d", b"\x00", [*read, "notify"], bool
    )
    objects = [
        hid,
        _Characteristic(hid.path + "/char0", hid, "2a4a", b"\x11\x01\x00\x02", read, bool),
        _Characteristic(hid.path + "/char1", hid, "2a4b", report_map, read, bool),
        _Characteristic(hid.path + "/char2", hid, "2a4c", b"\x00",
                        ["write-without-response", "encrypt-write"], bool, writable=True),
        report,
        _Descriptor(report.path + "/descriptor0", report, b"\x01\x01", bool),
        battery,
        _Characteristic(battery.path + "/char0", battery, "2a19", b"\x64", [*read, "notify"], bool),
        device_info,
        _Characteristic(device_info.path + "/char0", device_info, "2a29", b"Bluetooth Auth", ["read"], bool),
        _Characteristic(device_info.path + "/char1", device_info, "2a24", b"Passive Consumer Control", ["read"], bool),
        _Characteristic(device_info.path + "/char2", device_info, "2a50", b"\x02\x00\x00\x01\x00\x01\x00", ["read"], bool),
    ]
    for interface in objects:
        bus.export(interface.path, interface)

    adapter = "/org/bluez/hci0"
    introspection = await bus.introspect("org.bluez", adapter)
    proxy = bus.get_proxy_object("org.bluez", adapter, introspection)
    manager = proxy.get_interface("org.bluez.GattManager1")
    await manager.call_register_application(root, {})
    return bus
