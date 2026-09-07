"""Monitor one configured Bluetooth device without connecting or pairing."""

import argparse
import asyncio
import ctypes
import os
import re
import socket
import struct
import time
from pathlib import Path

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

ROUND_SECONDS = 5
ADDRESS_FILE_ENV = "BLUETOOTH_AUTH_ADDRESS_FILE"
ADDRESS_PATTERN = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")


def resolve_address_file(argument, environ=None):
    """Prefer an explicit path, then the single documented environment variable."""
    if argument:
        return argument
    return (os.environ if environ is None else environ).get(ADDRESS_FILE_ENV)


def load_address(address_file):
    """Read and validate configuration before opening HCI or D-Bus resources."""
    if not address_file:
        raise ValueError(f"缺少目标地址文件；使用 --address-file 或 {ADDRESS_FILE_ENV}")
    try:
        value = Path(address_file).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise ValueError("无法读取目标蓝牙地址文件") from None
    if not ADDRESS_PATTERN.fullmatch(value):
        raise ValueError("目标蓝牙地址文件内容无效")
    return value.upper()


def redact_addresses(value):
    return ADDRESS_PATTERN.sub("<蓝牙地址>", value)


def open_reports():
    caps = re.search(r"^CapEff:\s*([0-9a-fA-F]+)", Path("/proc/self/status").read_text(), re.M)
    if not caps or not int(caps[1], 16) & (1 << 12):  # CAP_NET_ADMIN
        raise PermissionError("权限不足：管理通道不会发送设备发现报告")
    # 本机 Python 没编入蓝牙地址支持，使用 Linux sockaddr_hci 直接绑定。
    reports = socket.socket(31, socket.SOCK_RAW, 1)  # AF_BLUETOOTH, BTPROTO_HCI
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        address = ctypes.create_string_buffer(struct.pack("=HHH", 31, 0xFFFF, 3))
        if libc.bind(reports.fileno(), address, 6) < 0:
            raise OSError(ctypes.get_errno(), "无法绑定蓝牙报告通道")
        reports.setblocking(False)
        return reports
    except BaseException:
        reports.close()
        raise


def scan_rssi(packet, index, address):
    """只接受 Linux BR/EDR inquiry 报告，排除名称更新附带的缓存 RSSI。"""
    if len(packet) < 20:
        return None
    event, controller, length = struct.unpack_from("<HHH", packet)
    if event != 0x0012 or controller != index or len(packet) != length + 6:
        return None
    if packet[12] != 0 or packet[6:12] != bytes.fromhex(address.replace(":", ""))[::-1]:
        return None
    if struct.unpack_from("<I", packet, 14)[0] & 0x10:  # 名称查询失败不是新测量
        return None
    data = packet[20:]
    if struct.unpack_from("<H", packet, 18)[0] != len(data):
        return None
    # Linux 为 inquiry 报告附加 Class of Device；名称缓存更新没有此字段。
    has_class = False
    offset = 0
    while offset < len(data) and data[offset]:
        size = data[offset]
        if offset + size + 1 > len(data):
            return None
        has_class |= size == 4 and data[offset + 1] == 0x0D
        offset += size + 1
    if not has_class:
        return None
    value = struct.unpack_from("b", packet, 13)[0]
    return value if value != 127 else None


async def link_rssi(address):
    # hcitool rssi 只查询已有连接，不会建立连接；经典蓝牙返回相对值，0 有效。
    process = await asyncio.create_subprocess_exec(
        "hcitool", "rssi", address,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 2)
        match = re.search(rb"RSSI return value:\s*(-?\d+)", stdout)
        if process.returncode == 0 and match:
            value = int(match[1])
            return f"RSSI {value}（链路原始值）" if value != 127 else "链路 RSSI 不可用"
        error = redact_addresses((stderr or stdout).decode(errors="replace").strip())
        return "收不到（本秒无新扫描报告，未连接）" if error == "Not connected." else f"链路读取失败：{error}"
    except TimeoutError:
        return "链路读取超时"
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def main(address):
    # CONTROL 通道只接收报告；扫描仍由 BlueZ 管理，退出只释放自己的会话。
    reports = open_reports()
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except BaseException:
        reports.close()
        raise
    latest = None
    accepting = False
    adapter_lost = False
    round_done = asyncio.Event()
    started = False
    loop = asyncio.get_running_loop()

    async def call(path, interface, member, signature="", body=None):
        reply = await asyncio.wait_for(bus.call(Message(
            destination="org.bluez", path=path, interface=interface,
            member=member, signature=signature, body=body or [],
        )), timeout=5)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{reply.error_name}: {reply.body}")
        return reply.body

    try:
        objects = (await call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        adapters = [path for path, interfaces in objects.items()
                    if "org.bluez.Adapter1" in interfaces
                    and interfaces["org.bluez.Adapter1"]["Powered"].value]
        if len(adapters) != 1:
            raise RuntimeError("需要恰好一个已开启的蓝牙适配器")
        adapter = adapters[0]
        index = int(adapter.rsplit("hci", 1)[1])

        def on_report():
            nonlocal latest, adapter_lost
            try:
                packet = reports.recv(65541)
            except BlockingIOError:
                return
            if len(packet) < 6:
                return
            event, controller, length = struct.unpack_from("<HHH", packet)
            if controller != index or len(packet) != length + 6:
                return
            if event == 0x0005:  # MGMT Index Removed
                adapter_lost = True
                latest = None
                round_done.set()
            if not accepting:
                return
            if event == 0x0013 and length == 2 and not packet[7]:
                round_done.set()
            value = scan_rssi(packet, index, address)
            if value is not None:
                latest = value
                round_done.set()

        loop.add_reader(reports.fileno(), on_report)
        await call(adapter, "org.bluez.Adapter1", "SetDiscoveryFilter", "a{sv}", [{
            "Transport": Variant("s", "bredr"),
            "RSSI": Variant("n", -127),
            "AutoConnect": Variant("b", False),
        }])
        print(f"监测目标设备（{adapter.rsplit('/', 1)[-1]}），每轮最多 {ROUND_SECONDS} 秒，Ctrl+C 退出", flush=True)
        scan_round = 0
        while True:
            discovering = (await call(adapter, "org.freedesktop.DBus.Properties", "Get", "ss",
                                      ["org.bluez.Adapter1", "Discovering"]))[0].value
            if discovering:
                raise RuntimeError("其他程序仍在扫描，无法开启独立的新一轮；请先关闭其他扫描程序")
            if adapter_lost:
                raise RuntimeError("蓝牙适配器已移除")
            # 丢弃上一轮排队的报告；暂停期间的结果不能给下一轮续期。
            while True:
                try:
                    reports.recv(65541)
                except BlockingIOError:
                    break
            latest = None
            round_done.clear()
            scan_round += 1
            accepting = True
            started = True
            await call(adapter, "org.bluez.Adapter1", "StartDiscovery")
            print(time.strftime("%H:%M:%S"), f"第 {scan_round} 轮开始", flush=True)
            try:
                async with asyncio.timeout(ROUND_SECONDS):
                    while not round_done.is_set():
                        try:
                            await asyncio.wait_for(round_done.wait(), 1)
                        except TimeoutError:
                            connected_reading = await link_rssi(address)
                            if not round_done.is_set():
                                print(time.strftime("%H:%M:%S"), f"第 {scan_round} 轮", connected_reading, flush=True)
                    if adapter_lost:
                        raise RuntimeError("蓝牙适配器已移除")
                    reading = f"RSSI {latest} dBm（本轮新查询结果）" if latest is not None else "本轮结束，没有目标新读数"
                    print(time.strftime("%H:%M:%S"), f"第 {scan_round} 轮", reading, flush=True)
            except TimeoutError:
                print(time.strftime("%H:%M:%S"), f"第 {scan_round} 轮超时，没有目标新扫描读数", flush=True)
            finally:
                accepting = False
                latest = None
                started = False
                await call(adapter, "org.bluez.Adapter1", "StopDiscovery")
            print(time.strftime("%H:%M:%S"), f"第 {scan_round} 轮会话已释放", flush=True)
            await asyncio.sleep(0.2)
    finally:
        accepting = False
        try:
            if started:
                try:
                    await call(adapter, "org.bluez.Adapter1", "StopDiscovery")
                except Exception as error:
                    print(f"停止扫描未成功：{error}；正在关闭本脚本的连接", flush=True)
        finally:
            loop.remove_reader(reports.fileno())
            reports.close()
            bus.disconnect()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--address-file",
        help=f"目标地址文件；未提供时读取 ${ADDRESS_FILE_ENV}",
    )
    return result


async def run(args):
    # This validation intentionally precedes main(), which is the first point
    # that opens either the HCI report channel or the system D-Bus connection.
    address = load_address(resolve_address_file(args.address_file))
    await main(address)


if __name__ == "__main__":
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except PermissionError:
        raise SystemExit(
            "需要权限读取控制器报告；请用 sudo 运行本脚本，并通过 --address-file 传入地址文件。"
        )
    except (ValueError, RuntimeError, OSError, TimeoutError) as error:
        raise SystemExit(f"错误：{redact_addresses(str(error))}")
    except ValueError as error:
        raise SystemExit(f"错误：{redact_addresses(str(error))}")
