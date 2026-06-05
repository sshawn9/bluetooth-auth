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


MODE_SETTINGS = {
    "sudo": {
        "ident": "bluetooth-auth-sudo-auth",
        "settingsKey": "sudoAuth",
        "userMatches": lambda trusted_user: (
            trusted_user
            in {
                os.environ.get("PAM_USER", "").strip(),
                os.environ.get("PAM_RUSER", "").strip(),
            }
        ),
    },
    "polkit": {
        "ident": "bluetooth-auth-polkit-auth",
        "settingsKey": "polkitAuth",
        "userMatches": lambda _trusted_user: True,
    },
    "locker": {
        "ident": "bluetooth-auth-locker-auth",
        "settingsKey": "lockerAuth",
        "userMatches": lambda trusted_user: (
            os.environ.get("PAM_USER", "").strip() == trusted_user
        ),
    },
    "greetd": {
        "ident": "bluetooth-auth-greetd-auth",
        "settingsKey": "greetdAuth",
        "userMatches": lambda trusted_user: (
            os.environ.get("PAM_USER", "").strip() == trusted_user
        ),
    },
}


async def async_main() -> int:
    mode = sys.argv[2].strip() if len(sys.argv) > 2 else ""
    syslog.openlog(
        MODE_SETTINGS.get(mode, {}).get("ident", "bluetooth-auth-oneshot-auth"),
        syslog.LOG_PID,
        syslog.LOG_AUTHPRIV,
    )
    syslog.syslog(
        syslog.LOG_INFO,
        "PAM context "
        f"PAM_USER={os.environ.get('PAM_USER', '').strip()!r} "
        f"PAM_RUSER={os.environ.get('PAM_RUSER', '').strip()!r}",
    )

    try:
        settings_path = (
            Path(sys.argv[1].strip())
            if len(sys.argv) > 1
            else resources.files("bluetooth_auth").joinpath("settings.json")
        )
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        trusted_user = settings["user"].strip()
        bluetooth_address = (
            Path(settings["bluetoothAddressFile"]).read_text(encoding="utf-8").strip()
        )
        mode_settings = MODE_SETTINGS[mode]
        if not mode_settings["userMatches"](trusted_user):
            syslog.syslog(
                syslog.LOG_INFO,
                "PAM request is not for the trusted user; skipping auth",
            )
            syslog.closelog()
            return 1

        timeout_seconds = min(
            float(settings[mode_settings["settingsKey"]]["timeoutSeconds"]),
            5.0,
        )
        if not trusted_user or not bluetooth_address or timeout_seconds <= 0:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError) as error:
        syslog.syslog(
            syslog.LOG_ERR,
            f"failed to load settings or auth context: {type(error).__name__}: {error}",
        )
        syslog.closelog()
        return 2

    syslog.syslog(
        syslog.LOG_INFO,
        f"starting mode={mode} settings={settings_path} "
        f"timeout_seconds={timeout_seconds}",
    )

    device = BluetoothDevice(bluetooth_address)
    try:
        async with asyncio.timeout(timeout_seconds):
            if not device.is_initialized():
                syslog.syslog(
                    syslog.LOG_INFO,
                    "initializing BlueZ D-Bus proxy",
                )
                await device.initialize()

            if await device.is_connected():
                syslog.syslog(
                    syslog.LOG_INFO,
                    "trusted device is connected; accepting authentication",
                )
                return 0

            syslog.syslog(
                syslog.LOG_INFO,
                "trusted device is not connected; falling back to the next auth rule",
            )
            return 1
    except (DBusError, InterfaceNotFoundError) as error:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "BlueZ or D-Bus check failed; falling back to the next auth rule: "
            f"{type(error).__name__}: {error}",
        )
        return 1
    except asyncio.CancelledError:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "Bluetooth check was cancelled; falling back to the next auth rule",
        )
        return 1
    except TimeoutError as error:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "Bluetooth check timed out; falling back to the next auth rule: "
            f"{type(error).__name__}: {error}",
        )
        return 1
    except OSError as error:
        syslog.syslog(
            syslog.LOG_NOTICE,
            "Bluetooth check unavailable; falling back to the next auth rule: "
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
            "unexpected error; falling back to the next auth rule: "
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
