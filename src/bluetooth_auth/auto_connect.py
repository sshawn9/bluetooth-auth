from __future__ import annotations

import asyncio
import json
import sys
from importlib import resources
from pathlib import Path

from dbus_fast.errors import DBusError, InterfaceNotFoundError

from .bluetooth_device import BluetoothDevice


async def main() -> int:
    try:
        config_path = (
            Path(sys.argv[1].strip())
            if len(sys.argv) > 1
            else resources.files("bluetooth_auth").joinpath("config.json")
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        bluetooth_address = (
            Path(config["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
        )
        auto_connect_config = config["autoConnect"]
        check_interval_seconds = max(
            int(auto_connect_config["checkIntervalSeconds"]), 5
        )
        device_unavailable_grace_seconds = max(
            int(auto_connect_config["deviceUnvailableGraceSeconds"]), 10
        )
        exception_grace_seconds = max(
            int(auto_connect_config["exceptionGraceSeconds"]), 10
        )
        if (
            not bluetooth_address
            or check_interval_seconds <= 0
            or device_unavailable_grace_seconds <= 0
            or exception_grace_seconds <= 0
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return 2

    device = BluetoothDevice(bluetooth_address)
    while True:
        try:
            if not device.is_initialized():
                await device.initialize()

            if await device.is_connected():
                # Device is connected, wait before checking again.
                await asyncio.sleep(check_interval_seconds)
                continue

            # Device is not connected, attempt to connect.
            if (
                not await device.is_powered()
                or not await device.is_trusted_and_paired()
            ):
                # Bluetooth adapter is not powered or device is not trusted and paired, wait before retrying.
                await asyncio.sleep(device_unavailable_grace_seconds)
                continue

            await device.bluez_device_iface.call_connect()
        except (DBusError, InterfaceNotFoundError, OSError):
            device.close()
            await asyncio.sleep(exception_grace_seconds)
        except (asyncio.CancelledError, KeyboardInterrupt):
            device.close()
            return 130
        except (TypeError, ValueError):
            device.close()
            return 2
        except Exception:
            device.close()
            return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
