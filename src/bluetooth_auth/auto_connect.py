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
        settings_path = (
            Path(sys.argv[1].strip())
            if len(sys.argv) > 1
            else resources.files("bluetooth_auth").joinpath("settings.json")
        )
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        bluetooth_address = (
            Path(settings["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
        )
        auto_connect_settings = settings["autoConnect"]
        check_interval_seconds = max(
            int(auto_connect_settings["checkIntervalSeconds"]), 5
        )
        device_unavailable_grace_seconds = max(
            int(auto_connect_settings["deviceUnvailableGraceSeconds"]), 10
        )
        exception_grace_seconds = max(
            int(auto_connect_settings["exceptionGraceSeconds"]), 10
        )
        reconnect_times = max(int(auto_connect_settings["reconnectTimes"]), 1)
        reconnect_interval_seconds = max(
            int(auto_connect_settings["reconnectIntervalSeconds"]), 2
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
            "bluetooth-auth-auto-connect: failed to load settings or address file: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    print(
        "bluetooth-auth-auto-connect: starting "
        f"settings={settings_path} "
        f"check_interval_seconds={check_interval_seconds} "
        f"device_unavailable_grace_seconds={device_unavailable_grace_seconds} "
        f"exception_grace_seconds={exception_grace_seconds} "
        f"reconnect_times={reconnect_times} "
        f"reconnect_interval_seconds={reconnect_interval_seconds}",
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
            continue
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

        retries = 0
        while retries < reconnect_times:
            retries += 1
            try:
                print(
                    "bluetooth-auth-auto-connect: device is disconnected; "
                    f"requesting BlueZ connection attempt={retries}/{reconnect_times}",
                    flush=True,
                )
                await device.bluez_device_iface.call_connect()
                break
            except DBusError as error:
                print(
                    "bluetooth-auth-auto-connect: failed to request BlueZ connection; "
                    f"retrying in {reconnect_interval_seconds}s: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    await asyncio.sleep(reconnect_interval_seconds)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    print(
                        "bluetooth-auth-auto-connect: interrupted during connection retry sleep",
                        flush=True,
                    )
                    return 130
                continue
            except OSError as error:
                device.close()
                print(
                    "bluetooth-auth-auto-connect: failed to request BlueZ connection; "
                    f"retrying state check in {exception_grace_seconds}s: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    await asyncio.sleep(exception_grace_seconds)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    print(
                        "bluetooth-auth-auto-connect: interrupted during connection retry sleep",
                        flush=True,
                    )
                    return 130
                break
            except (asyncio.CancelledError, KeyboardInterrupt):
                device.close()
                print("bluetooth-auth-auto-connect: interrupted", flush=True)
                return 130
            except Exception as error:
                device.close()
                print(
                    "bluetooth-auth-auto-connect: unexpected connection error: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
