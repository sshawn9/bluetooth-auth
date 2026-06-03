from __future__ import annotations

import asyncio
import json
import os
import sys
import syslog
from importlib import resources
from pathlib import Path
from dbus_fast.errors import DBusError, InterfaceNotFoundError


from .bluetooth_device import BluetoothDevice


async def async_main() -> int:
    syslog.openlog(
        "bluetooth-auth-sudo-auth",
        syslog.LOG_PID,
        syslog.LOG_AUTHPRIV,
    )

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
    except (OSError, ValueError, KeyError, TypeError) as error:
        syslog.syslog(
            syslog.LOG_ERR,
            "failed to load config or address file: "
            f"{type(error).__name__}: {error}",
        )
        return 2

    syslog.syslog(
        syslog.LOG_INFO,
        "starting "
        f"config={config_path} "
        f"trusted_user={trusted_user} "
        f"timeout_seconds={timeout_seconds}",
    )

    device = BluetoothDevice(bluetooth_address)
    try:
        pam_users = {
            os.environ.get("PAM_USER", ""),
            os.environ.get("PAM_RUSER", ""),
            os.environ.get("SUDO_USER", ""),
        }
        if trusted_user not in pam_users:
            syslog.syslog(
                syslog.LOG_INFO,
                "PAM request is not for the trusted user; skipping Bluetooth auth",
            )
            return 1

        async with asyncio.timeout(timeout_seconds):
            if not device.is_initialized():
                syslog.syslog(
                    syslog.LOG_INFO,
                    "initializing BlueZ D-Bus proxy",
                )
                await device.initialize()
                syslog.syslog(
                    syslog.LOG_INFO,
                    "BlueZ D-Bus proxy initialized",
                )

            if await device.is_connected():
                syslog.syslog(
                    syslog.LOG_INFO,
                    "trusted device is connected; accepting sudo authentication",
                )
                return 0

            syslog.syslog(
                syslog.LOG_INFO,
                "trusted device is not connected; falling back to the next PAM rule",
            )
            return 1

    except (DBusError, InterfaceNotFoundError) as error:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "BlueZ or D-Bus check failed; falling back to the next PAM rule: "
            f"{type(error).__name__}: {error}",
        )
        return 1
    except asyncio.CancelledError:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "Bluetooth check was cancelled; falling back to the next PAM rule",
        )
        return 1
    except TimeoutError as error:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "Bluetooth check timed out; falling back to the next PAM rule: "
            f"{type(error).__name__}: {error}",
        )
        return 1
    except OSError as error:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "Bluetooth check unavailable; falling back to the next PAM rule: "
            f"{type(error).__name__}: {error}",
        )
        return 1
    except ValueError as error:
        syslog.syslog(
            syslog.LOG_ERR,
            f"invalid runtime state: {type(error).__name__}: {error}",
        )
        return 2
    except KeyboardInterrupt:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "interrupted",
        )
        return 130
    except Exception as error:
        syslog.syslog(
            syslog.LOG_ERR,
            "unexpected error; falling back to the next PAM rule: "
            f"{type(error).__name__}: {error}",
        )
        return 1
    finally:
        device.close()
        syslog.closelog()


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
