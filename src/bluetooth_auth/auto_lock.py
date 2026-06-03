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


async def main() -> int:
    try:
        config_path = (
            Path(sys.argv[1].strip())
            if len(sys.argv) > 1
            else resources.files("bluetooth_auth").joinpath("config.json")
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        user = config["user"].strip()
        bluetooth_address = (
            Path(config["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
        )
        auto_lock_config = config["autoLock"]
        check_interval_seconds = max(
            int(auto_lock_config["checkIntervalSeconds"]),
            5,
        )
        sleep_after_lock_seconds = max(
            int(auto_lock_config["sleepAfterLockSeconds"]), 0
        )
        exception_grace_seconds = max(
            int(auto_lock_config["exceptionGraceSeconds"]), 10
        )
        if (
            not user
            or not bluetooth_address
            or check_interval_seconds <= 0
            or sleep_after_lock_seconds < 0
            or exception_grace_seconds <= 0
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return 2

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
                await device.initialize()
            connected = await device.is_connected()
        except (DBusError, InterfaceNotFoundError, OSError):
            device.close()
            if not await async_sleep(exception_grace_seconds):
                return 130
            continue
        except (asyncio.CancelledError, KeyboardInterrupt):
            device.close()
            return 130
        except Exception:
            device.close()
            return 2

        try:
            is_locked = session.is_locked()
        except KeyboardInterrupt:
            device.close()
            return 130
        except Exception:
            device.close()
            return 2

        if connected or is_locked:
            if not await async_sleep(check_interval_seconds):
                device.close()
                return 130
            continue

        try:
            subprocess.run(
                ("systemctl", "start", "noctalia-lock.service"),
                check=True,
            )
            if not await async_sleep(sleep_after_lock_seconds):
                device.close()
                return 130
        except subprocess.CalledProcessError:
            device.close()
            return 1
        except KeyboardInterrupt:
            device.close()
            return 130
        except Exception:
            device.close()
            return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
