from __future__ import annotations

import asyncio

from dbus_fast import BusType
from dbus_fast.aio import MessageBus
from dbus_fast.aio.proxy_object import ProxyInterface


BLUEZ = "org.bluez"
DEVICE_IFACE = "org.bluez.Device1"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


class BluetoothDevice:
    def __init__(
        self,
        address: str,
    ) -> None:
        self.address = address
        self.path = f"/org/bluez/hci0/dev_{address.upper().replace(':', '_')}"
        self.bus: MessageBus | None = None
        self.properties_iface: ProxyInterface | None = None
        self.device: ProxyInterface | None = None

    def close(self) -> None:
        if self.bus is not None:
            try:
                self.bus.disconnect()
            except Exception:
                pass

        self.bus = None
        self.properties_iface = None
        self.device = None

    async def ensure_bluez_proxy(self) -> None:
        if (
            self.bus is not None
            and self.properties_iface is not None
            and self.device is not None
        ):
            return
        while True:
            try:
                self.close()
                bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                introspection = await bus.introspect(BLUEZ, self.path)
                obj = bus.get_proxy_object(BLUEZ, self.path, introspection)
                properties_iface = obj.get_interface(PROPERTIES_IFACE)
                device = obj.get_interface(DEVICE_IFACE)

                self.bus = bus
                self.properties_iface = properties_iface
                self.device = device
                return
            except asyncio.CancelledError:
                self.close()
                raise
            except Exception:
                self.close()
                await asyncio.sleep(10)

    async def _get_property(self, name: str) -> object:
        try:
            await self.ensure_bluez_proxy()
            assert self.properties_iface is not None
            value = await self.properties_iface.call_get(DEVICE_IFACE, name)
            return getattr(value, "value", value)
        except BaseException:
            self.close()
            raise

    async def query_connected(self) -> bool:
        return bool(await self._get_property("Connected"))

    async def connect(self) -> None:
        try:
            if await self.query_connected():
                return

            is_trusted = bool(await self._get_property("Trusted"))
            is_paired = bool(await self._get_property("Paired"))
            if not is_trusted or not is_paired:
                return

            await self.ensure_bluez_proxy()
            assert self.device is not None
            await self.device.call_connect()
        except BaseException:
            self.close()
            raise

    async def ensure_connected(self) -> None:
        while True:
            try:
                await self.connect()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.close()
                raise
            except Exception:
                self.close()
                await asyncio.sleep(10)
