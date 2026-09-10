"""常驻持有 HID 服务，只处理 ask-or-connect 请求。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import re
import signal
import stat
from pathlib import Path

from dbus_fast.service import ServiceInterface, dbus_method

from .bluez import API_INTERFACE, API_PATH, BackendError, BlueZBackend


def load_address(path: Path) -> str:
    """地址只从 root 或服务用户持有的私有普通文件读取。"""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid not in {0, os.geteuid()}
            or info.st_size > 128
        ):
            raise ValueError("地址文件权限或类型无效")
        address = os.read(descriptor, 129).decode("ascii").strip().upper()
        if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
            raise ValueError("地址格式无效")
        return address
    finally:
        os.close(descriptor)


class Authentication(ServiceInterface):
    """唯一接口：实时查询，未连接时只等待一轮连接结果。"""

    def __init__(self, backend, connect_timeout: float, query_timeout: float = 2):
        super().__init__(API_INTERFACE)
        self.backend = backend
        self.connect_timeout, self.query_timeout = connect_timeout, query_timeout
        self._request = None
        self._closed = False

    @dbus_method()
    async def AskOrConnect(self) -> "b":
        return await self.ask_or_connect()

    async def ask_or_connect(self) -> bool:
        if self._closed:
            return False
        # 并发调用共享正在执行的一轮；完成后下一次请求重新查询。
        if self._request is None or self._request.done():
            self._request = asyncio.create_task(self._once())
        return await asyncio.shield(self._request)

    async def _once(self) -> bool:
        try:
            async with asyncio.timeout(self.query_timeout):
                if await self.backend.connected():
                    return True
            disconnected = self.backend.disconnect_count
            # HID 广播由 daemon 持续提供；这里只开启一次限时连接等待。
            async with asyncio.timeout(self.connect_timeout):
                while True:
                    connected = await self.backend.connected()
                    if self.backend.disconnect_count != disconnected:
                        return False
                    if connected:
                        return True
                    # 查询同一轮连接的进展，不重建广播，不重试连接。
                    await self.backend.wait_change(0.2)
        except Exception:
            # 失败和超时都结束本次请求；不回显可能带地址的异常正文。
            return False

    async def close(self):
        self._closed = True
        if self._request is not None:
            self._request.cancel()
            await asyncio.gather(self._request, return_exceptions=True)


async def run(address: str, adapter: str, connect_timeout: float) -> int:
    backend = BlueZBackend(address, adapter)
    api = Authentication(backend, connect_timeout)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, task.cancel)
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, _context: print("HID daemon 内部错误", flush=True)
    )
    try:
        await backend.open()
        backend.bus.export(API_PATH, api)
        print("HID daemon 已启动", flush=True)
        await backend.failed.wait()
        print("蓝牙后端失效，HID daemon 退出", flush=True)
        return 2
    except asyncio.CancelledError:
        return 0
    finally:
        try:
            await api.close()
        finally:
            await backend.close()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        loop.set_exception_handler(previous_handler)
        print("HID daemon 已停止", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address-file",
        help="只包含目标手机身份地址的私有文件；省略时读取 systemd 的 phone 凭据",
    )
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument(
        "--connect-timeout", type=float, default=5, help="一次连接等待的秒数，不重试"
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"hci[0-9]+", args.adapter) or int(args.adapter[3:]) >= 0xFFFF:
        parser.error("适配器必须是有效的 hci 编号")
    if not math.isfinite(args.connect_timeout) or not 0 < args.connect_timeout <= 60:
        parser.error("连接超时须大于零且不超过 60 秒")
    try:
        path = (
            Path(args.address_file)
            if args.address_file
            else Path(os.environ["CREDENTIALS_DIRECTORY"]) / "phone"
        )
        address = load_address(path)
    except (OSError, ValueError, KeyError):
        print(
            "无法读取手机地址：需要私有普通文件，或 systemd 的 phone 凭据", flush=True
        )
        return 78
    # BlueZ 原始日志可能带设备地址；错误由本程序以固定文本报告。
    logger = logging.getLogger("dbus_fast")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    try:
        return asyncio.run(run(address, args.adapter, args.connect_timeout))
    except BackendError as error:
        print(f"HID daemon 失败：{error}", flush=True)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        print("HID daemon 启动或运行失败", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
