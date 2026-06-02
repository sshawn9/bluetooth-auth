from __future__ import annotations

import asyncio
import sys

from .bluetooth_device import BluetoothDevice


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 1:
        return 2

    bluetooth_address = args[0].strip()
    if not bluetooth_address:
        return 2

    device = BluetoothDevice(bluetooth_address)
    try:
        asyncio.run(device.ensure_connected())
    except (OSError, ValueError) as error:
        return 2
    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
