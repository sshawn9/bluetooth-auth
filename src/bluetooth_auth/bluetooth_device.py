from __future__ import annotations

import asyncio

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface


class BluetoothDevice:
    # Well-known D-Bus service name owned by the BlueZ daemon on the system bus.
    BLUEZ_SERVICE_NAME = "org.bluez"

    # hci0 is Linux's first local Bluetooth controller. BlueZ exposes that
    # controller as an adapter object at this D-Bus object path.
    HCI0_OBJECT_PATH = "/org/bluez/hci0"

    # Adapter1 is the BlueZ interface implemented by local Bluetooth
    # controllers. It exposes controller-level state, such as Powered.
    BLUEZ_ADAPTER1_INTERFACE = "org.bluez.Adapter1"

    # Device1 is the BlueZ interface implemented by remote Bluetooth devices
    # below an adapter path, for example /org/bluez/hci0/dev_XX_XX_XX_XX_XX_XX.
    BLUEZ_DEVICE1_INTERFACE = "org.bluez.Device1"

    # Standard D-Bus interface used to read properties from Adapter1 and Device1.
    DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"

    def __init__(
        self,
        address: str,
    ) -> None:
        self.address = address
        self.bluez_device_path = (
            f"{self.HCI0_OBJECT_PATH}/dev_{address.upper().replace(':', '_')}"
        )
        self.bluez_bus: MessageBus | None = None
        self.bluez_device_properties_iface: ProxyInterface | None = None
        self.bluez_device_iface: ProxyInterface | None = None
        self.bluez_hci0_properties_iface: ProxyInterface | None = None

    def close(self) -> None:
        if self.bluez_bus is not None:
            try:
                self.bluez_bus.disconnect()
            except Exception:
                pass

        self.bluez_bus = None
        self.bluez_device_properties_iface = None
        self.bluez_device_iface = None
        self.bluez_hci0_properties_iface = None

    def is_initialized(self) -> bool:
        return (
            self.bluez_bus is not None
            and self.bluez_device_properties_iface is not None
            and self.bluez_device_iface is not None
            and self.bluez_hci0_properties_iface is not None
        )

    async def initialize(self) -> None:
        self.close()
        bluez_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        device_introspection = await bluez_bus.introspect(
            self.BLUEZ_SERVICE_NAME,
            self.bluez_device_path,
        )
        bluez_device_obj = bluez_bus.get_proxy_object(
            self.BLUEZ_SERVICE_NAME,
            self.bluez_device_path,
            device_introspection,
        )
        bluez_device_properties_iface = bluez_device_obj.get_interface(
            self.DBUS_PROPERTIES_INTERFACE
        )
        bluez_device_iface = bluez_device_obj.get_interface(
            self.BLUEZ_DEVICE1_INTERFACE
        )

        hci0_introspection = await bluez_bus.introspect(
            BluetoothDevice.BLUEZ_SERVICE_NAME,
            BluetoothDevice.HCI0_OBJECT_PATH,
        )
        bluez_hci0_obj = bluez_bus.get_proxy_object(
            BluetoothDevice.BLUEZ_SERVICE_NAME,
            BluetoothDevice.HCI0_OBJECT_PATH,
            hci0_introspection,
        )
        bluez_hci0_properties_iface = bluez_hci0_obj.get_interface(
            BluetoothDevice.DBUS_PROPERTIES_INTERFACE
        )

        self.bluez_bus = bluez_bus
        self.bluez_device_properties_iface = bluez_device_properties_iface
        self.bluez_device_iface = bluez_device_iface
        self.bluez_hci0_properties_iface = bluez_hci0_properties_iface

    async def is_connected(self) -> bool:
        value = await self.bluez_device_properties_iface.call_get(
            self.BLUEZ_DEVICE1_INTERFACE,
            "Connected",
        )
        return getattr(value, "value", value)

    async def is_powered(self) -> bool:
        value = await self.bluez_hci0_properties_iface.call_get(
            self.BLUEZ_ADAPTER1_INTERFACE,
            "Powered",
        )
        return getattr(value, "value", value)

    async def is_trusted_and_paired(self) -> bool:
        props = await self.bluez_device_properties_iface.call_get_all(
            self.BLUEZ_DEVICE1_INTERFACE,
        )
        is_trusted_value = props.get("Trusted")
        is_paired_value = props.get("Paired")
        is_trusted = bool(getattr(is_trusted_value, "value", is_trusted_value))
        is_paired = bool(getattr(is_paired_value, "value", is_paired_value))
        return is_trusted and is_paired
