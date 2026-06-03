from __future__ import annotations

import asyncio
import json
import sys
from importlib import resources
from pathlib import Path

from dbus_fast.errors import DBusError, InterfaceNotFoundError

from .bluetooth_device import BluetoothDevice


async def async_main() -> int:
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
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(
            "bluetooth-auth-auto-connect: failed to load config or address file: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    print(
        "bluetooth-auth-auto-connect: starting "
        f"config={config_path} "
        f"check_interval_seconds={check_interval_seconds} "
        f"device_unavailable_grace_seconds={device_unavailable_grace_seconds} "
        f"exception_grace_seconds={exception_grace_seconds}",
        flush=True,
    )

    device = BluetoothDevice(bluetooth_address)
    while True:
        try:
            if not device.is_initialized():
                print(
                    "bluetooth-auth-auto-connect: initializing BlueZ D-Bus proxy",
                    flush=True,
                )
                await device.initialize()
                print(
                    "bluetooth-auth-auto-connect: BlueZ D-Bus proxy initialized",
                    flush=True,
                )

            if await device.is_connected():
                await asyncio.sleep(check_interval_seconds)
                continue

            # Device is not connected, attempt to connect.
            if not await device.is_powered():
                print(
                    "bluetooth-auth-auto-connect: Bluetooth controller is not powered; "
                    f"retrying in {device_unavailable_grace_seconds}s",
                    flush=True,
                )
                await asyncio.sleep(device_unavailable_grace_seconds)
                continue

            if not await device.is_trusted_and_paired():
                print(
                    "bluetooth-auth-auto-connect: device is not paired or not trusted; "
                    f"retrying in {device_unavailable_grace_seconds}s",
                    flush=True,
                )
                await asyncio.sleep(device_unavailable_grace_seconds)
                continue

            print(
                "bluetooth-auth-auto-connect: device is disconnected; "
                "requesting BlueZ connection",
                flush=True,
            )
            await device.bluez_device_iface.call_connect()
        except (DBusError, InterfaceNotFoundError, OSError) as error:
            device.close()
            print(
                "bluetooth-auth-auto-connect: BlueZ or D-Bus error; "
                f"retrying in {exception_grace_seconds}s: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            try:
                await asyncio.sleep(exception_grace_seconds)
            except (asyncio.CancelledError, KeyboardInterrupt):
                print(
                    "bluetooth-auth-auto-connect: interrupted during retry sleep",
                    flush=True,
                )
                return 130
        except (asyncio.CancelledError, KeyboardInterrupt):
            device.close()
            print("bluetooth-auth-auto-connect: interrupted", flush=True)
            return 130
        except (TypeError, ValueError) as error:
            device.close()
            print(
                "bluetooth-auth-auto-connect: invalid runtime state: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            return 2
        except Exception as error:
            device.close()
            print(
                "bluetooth-auth-auto-connect: unexpected error: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            return 2


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
