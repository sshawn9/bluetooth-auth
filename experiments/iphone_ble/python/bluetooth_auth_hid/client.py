"""最小客户端：调用一次 ask-or-connect，然后返回结果和退出码。"""

from __future__ import annotations

import argparse
import asyncio
import math

from dbus_fast import BusType, Message, MessageFlag, MessageType
from dbus_fast.aio import MessageBus

from .bluez import API_INTERFACE, API_PATH, BUS_NAME


async def ask_or_connect(
    *, timeout: float = 10, server_uid: int = 0, bus_factory=None
) -> bool:
    bus = None
    try:
        async with asyncio.timeout(timeout):
            bus = bus_factory() if bus_factory else MessageBus(bus_type=BusType.SYSTEM)
            await bus.connect()

            async def call(
                destination, path, interface, member, signature="", body=None
            ):
                reply = await bus.call(
                    Message(
                        destination=destination,
                        path=path,
                        interface=interface,
                        member=member,
                        signature=signature,
                        body=body or [],
                        flags=MessageFlag.NO_AUTOSTART,
                    )
                )
                if (
                    reply.sender != destination
                    or reply.message_type != MessageType.METHOD_RETURN
                ):
                    raise RuntimeError("D-Bus 调用失败")
                return reply

            dbus = "org.freedesktop.DBus"
            owner = (
                await call(
                    dbus, "/org/freedesktop/DBus", dbus, "GetNameOwner", "s", [BUS_NAME]
                )
            ).body[0]
            uid = (
                await call(
                    dbus,
                    "/org/freedesktop/DBus",
                    dbus,
                    "GetConnectionUnixUser",
                    "s",
                    [owner],
                )
            ).body[0]
            if type(uid) is not int or uid != server_uid:
                raise RuntimeError("daemon 用户不匹配")
            reply = await call(owner, API_PATH, API_INTERFACE, "AskOrConnect")
            if (
                reply.signature != "b"
                or len(reply.body) != 1
                or type(reply.body[0]) is not bool
            ):
                raise RuntimeError("daemon 返回格式无效")
            return reply.body[0]
    finally:
        if bus is not None:
            bus.disconnect()
            try:
                async with asyncio.timeout(1):
                    await bus.wait_for_disconnect()
            except Exception:
                pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation", nargs="?", choices=["ask-or-connect"], default="ask-or-connect"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10,
        help="整次客户端调用的超时秒数，须覆盖 daemon 的查询和连接时限",
    )
    parser.add_argument(
        "--server-uid", type=int, default=0, help="预期的 daemon 用户 UID，默认 root"
    )
    args = parser.parse_args(argv)
    if (
        not math.isfinite(args.timeout)
        or not 0 < args.timeout <= 125
        or args.server_uid < 0
    ):
        parser.error("超时须在 0 到 125 秒之间，UID 必须非负")
    try:
        connected = asyncio.run(
            ask_or_connect(timeout=args.timeout, server_uid=args.server_uid)
        )
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("失败：daemon 不可用、调用超时或返回无效", flush=True)
        return 2
    print("成功" if connected else "失败", flush=True)
    return 0 if connected else 1


if __name__ == "__main__":
    raise SystemExit(main())
