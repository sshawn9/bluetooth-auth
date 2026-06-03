from __future__ import annotations

import asyncio
import json
import os
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
        trusted_user = config["user"].strip()
        bluetooth_address = (
            Path(config["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
        )
        timeout_seconds = min(float(config["sudoAuth"]["timeoutSeconds"]), 5.0)
        if not trusted_user or not bluetooth_address or timeout_seconds <= 0:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        return 2

    device = BluetoothDevice(bluetooth_address)
    try:
        pam_users = {
            os.environ.get("PAM_USER", ""),
            os.environ.get("PAM_RUSER", ""),
            os.environ.get("SUDO_USER", ""),
        }
        if trusted_user not in pam_users:
            return 1

        async with asyncio.timeout(timeout_seconds):
            if not device.is_initialized():
                await device.initialize()

            return 0 if await device.is_connected() else 1

    except (DBusError, InterfaceNotFoundError):
        return 1
    except asyncio.CancelledError:
        return 1
    except TimeoutError:
        return 1
    except OSError:
        return 1
    except ValueError:
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        return 1
    finally:
        device.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
