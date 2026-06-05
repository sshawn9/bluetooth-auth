from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from importlib import resources
from pathlib import Path
from dbus_fast.errors import DBusError, InterfaceNotFoundError

from .bluetooth_device import BluetoothDevice
from .session import Session


async def async_main() -> int:
    try:
        settings_path = (
            Path(sys.argv[1].strip())
            if len(sys.argv) > 1
            else resources.files("bluetooth_auth").joinpath("settings.json")
        )
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        user = settings["user"].strip()
        bluetooth_address = (
            Path(settings["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
        )
        auto_lock_settings = settings["autoLock"]
        check_interval_seconds = max(
            int(auto_lock_settings["checkIntervalSeconds"]),
            5,
        )
        sleep_after_lock_seconds = max(
            int(auto_lock_settings["sleepAfterLockSeconds"]), 30
        )
        exception_grace_seconds = max(
            int(auto_lock_settings["exceptionGraceSeconds"]), 10
        )
        if (
            not user
            or not bluetooth_address
            or check_interval_seconds <= 0
            or sleep_after_lock_seconds < 0
            or exception_grace_seconds <= 0
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(
            "bluetooth-auth-auto-lock: failed to load settings or address file: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    print(
        "bluetooth-auth-auto-lock: starting "
        f"settings={settings_path} "
        f"user={user} "
        f"check_interval_seconds={check_interval_seconds} "
        f"sleep_after_lock_seconds={sleep_after_lock_seconds} "
        f"exception_grace_seconds={exception_grace_seconds}",
        flush=True,
    )

    async def async_sleep(interval: int) -> bool:
        try:
            await asyncio.sleep(interval)
            return True
        except (asyncio.CancelledError, KeyboardInterrupt):
            return False

    device = BluetoothDevice(bluetooth_address)
    session = Session(user)

    while True:
        try:
            if not device.is_initialized():
                print(
                    "bluetooth-auth-auto-lock: initializing BlueZ D-Bus proxy",
                    flush=True,
                )
                await device.initialize()
                print(
                    "bluetooth-auth-auto-lock: BlueZ D-Bus proxy initialized",
                    flush=True,
                )
            connected = await device.is_connected()
        except (DBusError, InterfaceNotFoundError, OSError) as error:
            device.close()
            print(
                "bluetooth-auth-auto-lock: BlueZ or D-Bus error; "
                f"retrying in {exception_grace_seconds}s: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            if not await async_sleep(exception_grace_seconds):
                print(
                    "bluetooth-auth-auto-lock: interrupted during retry sleep",
                    flush=True,
                )
                return 130
            continue
        except (asyncio.CancelledError, KeyboardInterrupt):
            device.close()
            print("bluetooth-auth-auto-lock: interrupted", flush=True)
            return 130
        except Exception as error:
            device.close()
            print(
                "bluetooth-auth-auto-lock: unexpected BlueZ check error: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            return 2

        try:
            is_locked = session.is_locked()
        except KeyboardInterrupt:
            device.close()
            print("bluetooth-auth-auto-lock: interrupted", flush=True)
            return 130
        except Exception as error:
            device.close()
            print(
                "bluetooth-auth-auto-lock: failed to read session lock state: "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            return 2

        if connected:
            if not await async_sleep(check_interval_seconds):
                device.close()
                print(
                    "bluetooth-auth-auto-lock: interrupted during normal sleep",
                    flush=True,
                )
                return 130
            continue

        if is_locked:
            if not await async_sleep(check_interval_seconds):
                device.close()
                print(
                    "bluetooth-auth-auto-lock: interrupted during normal sleep",
                    flush=True,
                )
                return 130
            continue

        while True:
            try:
                print(
                    "bluetooth-auth-auto-lock: device is disconnected and session is unlocked; "
                    "starting noctalia-lock.service",
                    flush=True,
                )
                subprocess.run(
                    ("systemctl", "start", "noctalia-lock.service"),
                    check=True,
                )
                if not session.is_locked():
                    print(
                        "bluetooth-auth-auto-lock: session is still unlocked after "
                        "starting noctalia-lock.service; retrying in 0.2s",
                        flush=True,
                    )
                    if not await async_sleep(0.2):
                        device.close()
                        print(
                            "bluetooth-auth-auto-lock: interrupted during retry sleep",
                            flush=True,
                        )
                        return 130
                    continue
                print(
                    "bluetooth-auth-auto-lock: noctalia-lock.service start completed "
                    "and session is locked; "
                    f"sleeping {sleep_after_lock_seconds}s",
                    flush=True,
                )
                if not await async_sleep(sleep_after_lock_seconds):
                    device.close()
                    print(
                        "bluetooth-auth-auto-lock: interrupted after starting lock service",
                        flush=True,
                    )
                    return 130
                break
            except subprocess.CalledProcessError as error:
                device.close()
                print(
                    "bluetooth-auth-auto-lock: noctalia-lock.service failed: "
                    f"returncode={error.returncode}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            except OSError as error:
                device.close()
                print(
                    "bluetooth-auth-auto-lock: failed to execute systemctl: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2
            except KeyboardInterrupt:
                device.close()
                print("bluetooth-auth-auto-lock: interrupted", flush=True)
                return 130
            except Exception as error:
                device.close()
                print(
                    "bluetooth-auth-auto-lock: unexpected lock command error: "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 2


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
