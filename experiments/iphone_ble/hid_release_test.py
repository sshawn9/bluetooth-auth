#!/usr/bin/env python3
"""短测：HID 提供进程退出后，原加密 LE 连接能否继续保持。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
from pathlib import Path
import re
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "python"))

from bluetooth_auth_hid.link import BT_CONNECTED, HCI_LE_LINK, LinkReader
from bluez_hid_lab import ADAPTER, AD_MANAGER, DEVICE, GATT_MANAGER, PHONE_PROPS, PROPS, Backend, address
from coexist_gatt import ADVERTISING_INTERVAL_MS, HID_APPEARANCE, HID_UUID, Advertising, HidApplication
from coexist_link import open as open_link


APP_PATH = "/org/bluetooth_auth/coexist/release_test/app"
AD_PATH = "/org/bluetooth_auth/coexist/release_test/advertisement"


class LinkLost(RuntimeError):
    pass


class Inconclusive(RuntimeError):
    """只用于本文件中的固定诊断文本，不携带底层异常。"""


def show(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def report_error(event, phase, error):
    """保留阶段、类型、错误号和代码位置，不输出含地址/路径的异常正文。"""
    error_name = getattr(error, "error_name", "")
    if not isinstance(error_name, str) or not re.fullmatch(
            r"org\.(?:bluez|freedesktop\.DBus)\.Error\.[A-Za-z]+", error_name):
        error_name = None
    origin = None
    trace = error.__traceback__
    while trace is not None:
        origin = {"function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno}
        trace = trace.tb_next
    show(event, phase=phase, error_type=type(error).__name__, dbus_error=error_name,
         errno=error.errno if isinstance(error, OSError) else None, origin=origin)


def uuids(adapter):
    return {value.lower().split("-", 1)[0].lstrip("0") for value in adapter.get("UUIDs", [])}


def target(objects, adapter_path, phone):
    matches = [(path, interfaces[DEVICE]) for path, interfaces in objects.items()
               if DEVICE in interfaces and interfaces[DEVICE].get("Adapter") == adapter_path
               and str(interfaces[DEVICE].get("Address", "")).upper() == phone]
    if len(matches) != 1:
        raise Inconclusive("无法找到与指定地址和适配器匹配的唯一手机记录")
    path, info = matches[0]
    if info.get("Blocked"):
        raise Inconclusive("目标手机被 BlueZ 阻止（Blocked=true）")
    # 这是已选地址的加密 LE 链路保持实验，不执行认证授权。
    # Paired/Bonded 是汇总属性，Trusted 是系统信任策略，都不能代替实际链路检查。
    return path, info


def connected_links(reader, index, phone):
    return [link for link in reader.read(index) if link.address == phone
            and link.link_type == HCI_LE_LINK and link.state == BT_CONNECTED]


def hid_ready(state):
    return (state.get("gatt_registered") is True and state.get("advertisement_registered") is True
            and state.get("hid_uuid_present") is True and state.get("advertising_instances") == 1)


def connection_ready(state):
    # 本轮测 LE 链路连续性；手机使用 HID 缓存时，不要求它重新读取 Report Map。
    return (hid_ready(state) and state.get("phone_le_connected") is True
            and state.get("hci_le_links") == 1 and state.get("hci_encrypted") is True)


def timeout_reason(phase, state):
    if phase == "register":
        return "未能在时限内确认 HID 服务和广播注册完成；尚未开始连接计时"
    if phase == "connect":
        if not hid_ready(state):
            return "等待期间 HID 服务或广播不再就绪"
        if not state.get("phone_le_connected"):
            return "HID 服务和广播已注册，但等待期内未观察到目标手机 LE 连接"
        if state.get("hci_le_links") != 1:
            return "已观察到目标 LE，但内核连接未能与目标身份唯一对应"
        if not state.get("hci_encrypted"):
            return "目标 LE 已连接，但等待期内未确认链路加密"
    return "阶段超时；尚不能回答退出后连接保持的问题"


def check_continuity(expected_handle, links, phone, disconnects):
    if any(item["address"] == phone and item["address_type"] in (1, 2) for item in disconnects):
        raise LinkLost("观察到目标 LE 断线事件；后续重连不算原连接保留")
    if len(links) != 1 or links[0].handle != expected_handle:
        raise LinkLost("原 LE 连接已消失或连接句柄已改变")
    if not links[0].encrypted:
        raise LinkLost("原 LE 连接的加密状态已失效")


async def provider(args, phone):
    """子进程只提供临时 GATT/广告；EOF 后只关闭自己的 D-Bus。"""
    backend = Backend()
    app = advertisement = None
    phase = "open"
    status = 0
    try:
        await backend.open()
        objects = await backend.objects()
        adapter_path = "/org/bluez/" + args.adapter
        phone_path, _ = target(objects, adapter_path, phone)
        adapter = objects[adapter_path][ADAPTER]

        def received(event, values):
            if event == "target_gatt_access":
                show("hid_read")  # 回调已经过目标路径、LE 和加密读取约束。

        app = HidApplication(backend.bus, APP_PATH, received, lambda path: path == phone_path,
                             include_dis="180a" not in uuids(adapter),
                             include_battery="180f" not in uuids(adapter))
        advertisement = Advertising(adapter["Alias"], lambda *_: None)
        app.export()
        phase = "register_gatt"
        await backend.register(adapter_path, GATT_MANAGER, APP_PATH)
        show("gatt_registered")
        advertisement.export(backend.bus, AD_PATH)
        phase = "register_advertisement"
        await backend.register(adapter_path, AD_MANAGER, AD_PATH)
        # unique name 仅经管道交给观察进程，不进入对外实验日志。
        show("registered", owner=backend.bus.unique_name)
        phase = "wait_for_eof"
        await asyncio.to_thread(sys.stdin.buffer.read, 1)
    except Exception as error:
        report_error("provider_error", phase, error)
        status = 2
    finally:
        try:
            bus = backend.bus
            await backend.close()
            if bus is not None:
                # disconnect() 只发起 shutdown；等待库完成 fd 和对象清理。
                # 不再调用 unexport，避免在已 shutdown 的连接上发送通知。
                await asyncio.wait_for(bus.wait_for_disconnect(), 3)
            show("provider_closed", dbus_closed=True)
        except Exception as error:
            report_error("provider_error", "close_dbus", error)
            status = 2
    return status


async def stop_provider(child):
    if child is None:
        return
    if child.stdin is not None:
        child.stdin.close()
    try:
        await asyncio.wait_for(child.wait(), 5)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        await child.wait()
        raise Inconclusive(f"HID 子进程停止超时，已终止；退出码 {child.returncode}，本轮不能判通过")


async def experiment(args, phone):
    observer, mgmt = Backend(), None
    child = output_task = None
    connection_setup = "manual" if getattr(args, "manual_connect", False) else "automatic"
    index = int(args.adapter[3:])
    adapter_path = "/org/bluez/" + args.adapter
    reader = LinkReader()
    signals = {"gatt_registered": False, "registered": False, "hid_read": False, "owner": None,
               "error": False, "closed": False}
    state = {}
    phase, observe_started = "prepare", None
    try:
        await observer.open()
        mgmt = await open_link(index)  # 未传 baseline，辅助模块的断开功能保持禁用。
        event_start = len(mgmt.disconnect_events)
        objects = await observer.objects()
        adapter = objects.get(adapter_path, {}).get(ADAPTER, {})
        show("initial_resources", hid_uuid_present="1812" in uuids(adapter),
             advertising_instances=objects.get(adapter_path, {}).get(AD_MANAGER, {}).get("ActiveInstances"))
        phone_path, phone_info = target(objects, adapter_path, phone)
        saved_phone = {key: phone_info.get(key) for key in PHONE_PROPS}
        state["target"] = saved_phone
        show("target_state", **saved_phone)
        if adapter.get("Powered") is not True or GATT_MANAGER not in objects.get(adapter_path, {}):
            raise Inconclusive("适配器未开启或没有 GATT 管理接口")
        if AD_MANAGER not in objects[adapter_path]:
            raise Inconclusive("适配器没有 LE 广播管理接口")
        if "1812" in uuids(adapter) or objects[adapter_path][AD_MANAGER].get("ActiveInstances", 0) != 0:
            raise Inconclusive("请先退出其他 HID 程序及广播实验，再运行本短测")
        original_links = await mgmt.connections()
        existing_target = {(peer, kind) for peer, kind in original_links if peer == phone and kind in (1, 2)}
        initial_links = connected_links(reader, index, phone)
        initial_handle = initial_links[0].handle if len(initial_links) == 1 else None
        # 目标 LE 的连续性由本轮单独判定；它退出后断线应判 failed，
        # 不能混入其他原连接保护集合，被误报为外部干扰。
        original_links -= existing_target
        if existing_target or initial_links:
            connection_setup = "existing"
            show("existing_connection", connection_setup=connection_setup,
                 hci_le_links=len(initial_links),
                 encrypted=len(initial_links) == 1 and initial_links[0].encrypted,
                 message="复用已有目标 LE 连接，核对加密后进行注销测试；无需先断开。")
        saved_adapter = {key: adapter.get(key) for key in PROPS}

        async def snapshot():
            objects = await observer.objects()
            current_adapter = objects.get(adapter_path, {}).get(ADAPTER, {})
            current_phone = objects.get(phone_path, {}).get(DEVICE, {})
            if {key: current_adapter.get(key) for key in PROPS} != saved_adapter:
                raise Inconclusive("适配器设置发生变化，本轮观测受干扰")
            current_flags = {key: current_phone.get(key) for key in PHONE_PROPS}
            state["target"] = current_flags
            if current_flags != saved_phone:
                show("target_state_changed", changes={key: {"before": saved_phone[key], "now": current_flags[key]}
                     for key in PHONE_PROPS if current_flags[key] != saved_phone[key]})
                raise Inconclusive("手机配对或信任状态发生变化，本轮观测受干扰")
            if str(current_phone.get("Address", "")).upper() != phone:
                raise Inconclusive("手机身份发生变化，本轮观测受干扰")
            current = await mgmt.connections()
            lost = {(item["address"], item["address_type"]) for item in mgmt.disconnect_events}
            if not original_links <= current or original_links & lost:
                raise Inconclusive("原有其他连接发生中断，本轮观测受干扰")
            links = connected_links(reader, index, phone)
            if phase in {"register", "connect"}:
                if any(item["address"] == phone and item["address_type"] in (1, 2)
                       for item in mgmt.disconnect_events[event_start:]):
                    raise LinkLost("准备期间目标 LE 已断线；重连不能替代本轮原连接")
                if initial_handle is not None and (len(links) != 1 or links[0].handle != initial_handle):
                    raise LinkLost("准备期间原目标 LE 消失或连接句柄已改变")
            state.update(
                gatt_registered=signals["gatt_registered"], advertisement_registered=signals["registered"],
                hid_uuid_present="1812" in uuids(current_adapter),
                advertising_instances=objects[adapter_path][AD_MANAGER].get("ActiveInstances", 0),
                bluez_connected=current_phone.get("Connected", False),
                phone_le_connected=any(peer == phone and kind in (1, 2) for peer, kind in current),
                hci_le_links=len(links), hci_encrypted=len(links) == 1 and links[0].encrypted,
                encrypted_hid_read_seen=signals["hid_read"],
            )
            return objects, links

        child = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(Path(__file__).resolve()), "--provider",
            "--phone-file", str(Path(args.phone_file).resolve()), "--adapter", args.adapter,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def read_events():
            async for line in child.stdout:
                item = json.loads(line)
                if item["event"] == "gatt_registered":
                    signals["gatt_registered"] = True
                    show("gatt_registered", hid_uuid=HID_UUID)
                elif item["event"] == "registered":
                    signals.update(registered=True, owner=item["owner"])
                elif item["event"] == "hid_read":
                    if not signals["hid_read"]:
                        show("target_hid_read", encrypted=True)
                    signals["hid_read"] = True
                elif item["event"] == "provider_closed":
                    signals["closed"] = item.get("dbus_closed") is True
                elif item["event"] in {"provider_error", "error"}:
                    signals["error"] = True
                    show("provider_error", phase=item.get("phase", "entry"),
                         error_type=item.get("error_type", "Unknown"),
                         dbus_error=item.get("dbus_error"), errno=item.get("errno"),
                         origin=item.get("origin"))

        output_task = asyncio.create_task(read_events())

        def check_provider():
            if signals["error"]:
                raise Inconclusive("HID 提供进程报错，见 provider_error")
            if child.returncode is not None:
                state["provider_returncode"] = child.returncode
                raise Inconclusive(f"HID 提供进程提前退出，退出码 {child.returncode}；没有进入注销后观察阶段")
            if output_task.done():
                raise Inconclusive("HID 提供进程的诊断管道已关闭或读取失败")

        phase = "register"
        show("preparing_hid", timeout_seconds=10)
        async with asyncio.timeout(10):
            while True:
                objects, links = await snapshot()
                check_provider()
                if hid_ready(state):
                    break
                await asyncio.sleep(0.1)
        show("advertising", hid_uuid=HID_UUID, appearance=f"0x{HID_APPEARANCE:04x}",
             interval_ms=ADVERTISING_INTERVAL_MS, shared_bluez_identity=True,
             name_source="adapter_alias", **state)
        phase = "connect"
        show("checking_existing_connection" if connection_setup == "existing" else "waiting_for_connection",
             timeout_seconds=args.wait_seconds,
             connection_setup=connection_setup,
             message=("HID 服务和广播已注册；正在核对已有 LE 的加密状态，请保持手机锁屏。"
                      if connection_setup == "existing" else
                      "HID 服务和广播已注册；在手机点击电脑原配对条目连接，连上后立即锁屏。"
                      if connection_setup == "manual" else
                      "HID 服务和广播已注册；手机保持锁屏，等待目标加密 LE 连接。"))
        deadline, next_progress = time.monotonic() + args.wait_seconds, 0
        async with asyncio.timeout(args.wait_seconds):
            while True:
                objects, links = await snapshot()
                check_provider()
                if not hid_ready(state):
                    raise Inconclusive("等待连接时 HID 服务或广播被提前撤销")
                if connection_ready(state):
                    break
                now = time.monotonic()
                if now >= next_progress:
                    show("connection_state", remaining_seconds=round(max(0, deadline - now), 1), **state)
                    next_progress = now + 5
                await asyncio.sleep(0.2)
        handle = links[0].handle
        phase = "baseline"
        show("connected", baseline_seconds=5, connection_setup=connection_setup,
             message="请保持手机锁屏；5 秒后退出 HID 提供进程。", **state)
        deadline = time.monotonic() + 5
        while True:
            _objects, links = await snapshot()
            check_provider()
            if not hid_ready(state):
                raise Inconclusive("基线期间 HID 服务或广播被提前撤销")
            check_continuity(handle, links, phone, mgmt.disconnect_events[event_start:])
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.2)

        phase = "release"
        state.clear()  # 退出前的连接/注册快照不能冒充退出后的观测。
        show("releasing", message="只退出 HID 提供进程；不调用任何蓝牙断开命令。")
        await stop_provider(child)
        await output_task
        state.update(provider_returncode=child.returncode, provider_dbus_closed=signals["closed"])
        show("provider_exit", **state)
        if child.returncode != 0 or signals["error"] or not signals["closed"]:
            raise Inconclusive("HID 子进程退出或 D-Bus 关闭未通过，见 provider_exit/provider_error")
        async with asyncio.timeout(5):
            while True:
                objects, links = await snapshot()
                owner_alive = await observer.owner_alive(signals["owner"])
                if (not owner_alive and "1812" not in uuids(objects[adapter_path][ADAPTER])
                        and objects[adapter_path][AD_MANAGER].get("ActiveInstances") == 0):
                    break
                await asyncio.sleep(0.1)
        phase = "observe"
        observe_started = time.monotonic()
        show("provider_exited", hid_removed=True, advertising_instances=0,
             observe_seconds=args.observe_seconds)
        deadline, next_progress = observe_started + args.observe_seconds, observe_started
        while True:
            objects, links = await snapshot()
            if ("1812" in uuids(objects[adapter_path][ADAPTER])
                    or objects[adapter_path][AD_MANAGER].get("ActiveInstances") != 0):
                raise Inconclusive("观察期间出现新的 HID 或广播，本轮受干扰")
            check_continuity(handle, links, phone, mgmt.disconnect_events[event_start:])
            now = time.monotonic()
            if now >= deadline:
                show("result", verdict="passed", same_le_link=True, encrypted=True,
                     connection_setup=connection_setup,
                     target_state=state["target"],
                     observed_seconds=round(now - observe_started, 1),
                     message="仅证明本轮短测中，提供进程退出后原加密 LE 连接保持。")
                return 0
            if now >= next_progress:
                show("observing", elapsed_seconds=round(now - observe_started, 1),
                     same_le_link=True, encrypted=True)
                next_progress = now + 10
            await asyncio.sleep(0.2)
    except LinkLost as error:
        negative = phase in {"release", "observe"}
        show("result", verdict="failed" if negative else "inconclusive", phase=phase, reason=str(error),
             connection_setup=connection_setup,
             observed_seconds=round(time.monotonic() - observe_started, 1) if observe_started else 0)
        return 1 if negative else 2
    except Inconclusive as error:
        show("result", verdict="inconclusive", phase=phase, reason=str(error), state=state,
             connection_setup=connection_setup)
        return 2
    except TimeoutError:
        show("result", verdict="inconclusive", phase=phase,
             reason=timeout_reason(phase, state), state=state, connection_setup=connection_setup)
        return 2
    except Exception as error:
        # 底层异常可能包含地址、对象路径或文件位置，不直接打印异常正文。
        show("result", verdict="inconclusive", phase=phase,
             reason="观察后端不可用", error_type=type(error).__name__, connection_setup=connection_setup)
        return 2
    finally:
        with contextlib.suppress(Exception):
            await stop_provider(child)
        if output_task is not None:
            output_task.cancel()
            await asyncio.gather(output_task, return_exceptions=True)
        if mgmt is not None:
            await mgmt.close()
        await observer.close()
        show("closed", active_disconnect_sent=False,
             message="测试进程和观察接口已关闭；未修改配对，未主动断开任何蓝牙连接。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone-file", required=True, help="已有私有地址文件，仅含手机身份地址一行")
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--wait-seconds", type=float, default=20)
    parser.add_argument("--observe-seconds", type=float, default=60)
    parser.add_argument("--manual-connect", action="store_true",
                        help="本轮由用户点击手机原配对条目建立初始连接；结果仅用于注销后保持测试")
    parser.add_argument("--provider", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not re.fullmatch(r"hci[0-9]+", args.adapter) or int(args.adapter[3:]) >= 0xFFFF:
        parser.error("需要有效 hci 编号")
    if any(not math.isfinite(value) or not 0 < value <= 120 for value in (args.wait_seconds, args.observe_seconds)):
        parser.error("等待和观察时限须大于零且不超过 120 秒")
    logging.getLogger("dbus_fast").addHandler(logging.NullHandler())
    logging.getLogger("dbus_fast").propagate = False
    phase = "read_config"
    try:
        phone = address(Path(args.phone_file).read_text())
        phase = "event_loop"
        return asyncio.run(provider(args, phone) if args.provider else experiment(args, phone)) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        report_error("provider_error" if args.provider else "error", phase, error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
