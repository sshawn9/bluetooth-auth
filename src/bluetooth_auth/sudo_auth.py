from __future__ import annotations

import asyncio
import os
import sys

from .bluetooth_device import BluetoothDevice


async def run(bluetooth_address: str, trusted_user: str) -> int:
    pam_users = {
        os.environ.get("PAM_USER", ""),
        os.environ.get("PAM_RUSER", ""),
        os.environ.get("SUDO_USER", ""),
    }
    if trusted_user not in pam_users:
        return 1

    device = BluetoothDevice(bluetooth_address)
    try:
        connected = await asyncio.wait_for(
            device.query_connected(),
            timeout=3,
        )
        return 0 if connected else 1
    except TimeoutError:
        return 1
    finally:
        device.close()


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 2:
        return 2

    trusted_user = args[0].strip()
    bluetooth_address = args[1].strip()
    if not trusted_user or not bluetooth_address:
        return 2

    try:
        return asyncio.run(run(bluetooth_address, trusted_user))
    except (OSError, ValueError) as error:
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
