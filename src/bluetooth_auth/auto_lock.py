from __future__ import annotations

import asyncio
import subprocess
import sys

from .bluetooth_device import BluetoothDevice
from .session import Session


async def run(
    user: str,
    bluetooth_address: str,
    check_interval_seconds: int = 30,
) -> None:
    device = BluetoothDevice(bluetooth_address)
    session = Session(user)

    try:
        while True:
            await asyncio.sleep(check_interval_seconds)
            if await device.query_connected() or session.is_locked():
                continue
            subprocess.run(
                ("systemctl", "start", "noctalia-lock.service"),
                check=False,
            )
    finally:
        device.close()


def main() -> int:
    args = sys.argv[1:]
    if len(args) < 2:
        return 2

    user = args[0].strip()
    bluetooth_address = args[1].strip()
    if not user or not bluetooth_address:
        return 2

    check_interval_seconds = 30
    if len(args) >= 3:
        try:
            check_interval_seconds = max(check_interval_seconds, int(args[2]))
        except ValueError:
            pass

    try:
        asyncio.run(run(user, bluetooth_address, check_interval_seconds))
    except (OSError, ValueError) as error:
        print(f"bluetooth-auth-auto-lock: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
