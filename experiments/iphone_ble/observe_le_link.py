#!/usr/bin/env python3
"""只读短测：重启后不启动 HID，观察目标加密 LE 链路是否存在并保持。"""

from __future__ import annotations

import argparse
import asyncio
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
from bluez_hid_lab import ADAPTER, AD_MANAGER, DEVICE, PHONE_PROPS, Backend, address
from coexist_link import open as open_link
from hid_release_test import Inconclusive, LinkLost, check_continuity, connected_links, target, uuids


def show(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def clean_resources(objects, adapter_path):
    interfaces = objects.get(adapter_path)
    if not isinstance(interfaces, dict) or ADAPTER not in interfaces or AD_MANAGER not in interfaces:
        raise Inconclusive("无法读取适配器、HID UUID 或 LE 广播管理接口")
    adapter, manager = interfaces[ADAPTER], interfaces[AD_MANAGER]
    if not isinstance(adapter, dict) or not isinstance(manager, dict) or not isinstance(adapter.get("UUIDs"), list):
        raise Inconclusive("无法完整读取 HID UUID 或 LE 广播状态")
    advertisements = manager.get("ActiveInstances")
    if type(advertisements) is not int or advertisements < 0:
        raise Inconclusive("无法完整读取 LE 广播实例计数")
    return "1812" not in uuids(adapter) and advertisements == 0, advertisements


async def observe(args, phone):
    observer, mgmt = Backend(), None
    phase, handle = "prepare", None
    state = {}
    try:
        adapter_path, index = "/org/bluez/" + args.adapter, int(args.adapter[3:])
        reader = LinkReader()
        await observer.open()
        mgmt = await open_link(index)
        event_start = len(mgmt.disconnect_events)
        objects = await observer.objects()
        _phone_path, phone_info = target(objects, adapter_path, phone)
        clean, advertisements = clean_resources(objects, adapter_path)
        initial_adapter = objects.get(adapter_path, {}).get(ADAPTER, {})
        show("initial_resources", hid_uuid_present="1812" in uuids(initial_adapter),
             advertising_instances=advertisements)
        if not clean:
            raise Inconclusive("检测到 HID 服务或 LE 广播；本脚本只观察无 HID 的重启后状态")
        saved_phone = {key: phone_info.get(key) for key in PHONE_PROPS}

        async def snapshot():
            objects = await observer.objects()
            current_phone = objects.get(_phone_path, {}).get(DEVICE, {})
            clean, advertisements = clean_resources(objects, adapter_path)
            if not clean:
                raise Inconclusive("观察期间出现 HID 服务或 LE 广播")
            if str(current_phone.get("Address", "")).upper() != phone:
                raise Inconclusive("手机身份发生变化，本轮不猜测 RPA 对应关系")
            if current_phone.get("Blocked"):
                raise Inconclusive("目标手机被 BlueZ 阻止")
            current_flags = {key: current_phone.get(key) for key in PHONE_PROPS}
            current = await mgmt.connections()
            links = connected_links(reader, index, phone)
            state.update(
                paired=current_flags.get("Paired"), bonded=current_flags.get("Bonded"),
                trusted=current_flags.get("Trusted"), bluez_connected=current_phone.get("Connected", False),
                phone_le_connected=any(peer == phone and kind in (1, 2) for peer, kind in current),
                hci_le_links=len(links), encrypted=len(links) == 1 and links[0].encrypted,
                advertising_instances=advertisements,
            )
            if current_flags != saved_phone:
                raise Inconclusive("手机配对或信任状态发生变化，本轮观测受干扰")
            return links

        phase = "connect"
        show("waiting_for_connection", timeout_seconds=args.wait_seconds,
             message="不启动 HID；手机保持锁屏，无需操作。")
        deadline, next_progress = time.monotonic() + args.wait_seconds, 0
        async with asyncio.timeout(args.wait_seconds):
            while True:
                links = await snapshot()
                if any(item["address"] == phone and item["address_type"] in (1, 2)
                       for item in mgmt.disconnect_events[event_start:]):
                    raise LinkLost("等待期观察到目标 LE 断线；不把后续重连当作连续链路")
                if state["phone_le_connected"] and len(links) == 1 and links[0].encrypted:
                    handle = links[0].handle
                    break
                now = time.monotonic()
                if now >= deadline:
                    raise TimeoutError
                if now >= next_progress:
                    show("connection_state", remaining_seconds=round(deadline - now, 1), **state)
                    next_progress = now + 5
                await asyncio.sleep(0.2)

        phase, started = "observe", time.monotonic()
        show("connected", observe_seconds=args.observe_seconds, **state)
        deadline, next_progress = started + args.observe_seconds, started
        while True:
            links = await snapshot()
            check_continuity(handle, links, phone, mgmt.disconnect_events[event_start:])
            if not state["phone_le_connected"]:
                raise Inconclusive("观察期间 MGMT 未确认目标 LE 连接，不能只凭 HCI 快照判通过")
            now = time.monotonic()
            if now >= deadline:
                show("result", verdict="passed", same_le_link=True, encrypted=True,
                     observed_seconds=round(now - started, 1),
                     message="仅证明本轮无 HID/广播时，同一目标加密 LE 链路连续保持。")
                return 0
            if now >= next_progress:
                show("observing", elapsed_seconds=round(now - started, 1), same_le_link=True, encrypted=True)
                next_progress = now + 10
            await asyncio.sleep(0.2)
    except LinkLost as error:
        show("result", verdict="failed", phase=phase, reason=str(error))
        return 1
    except Inconclusive as error:
        show("result", verdict="inconclusive", phase=phase, reason=str(error), state=state)
        return 2
    except TimeoutError:
        show("result", verdict="inconclusive", phase=phase,
             reason="等待期内未观察到唯一的目标加密 LE 连接", state=state)
        return 2
    except Exception as error:
        show("result", verdict="inconclusive", phase=phase, reason="观察后端不可用",
             error_type=type(error).__name__)
        return 2
    finally:
        close_errors = []
        if mgmt is not None:
            try:
                await mgmt.close()
            except Exception as error:
                close_errors.append({"interface": "mgmt", "error_type": type(error).__name__})
        try:
            await observer.close()
        except Exception as error:
            close_errors.append({"interface": "dbus", "error_type": type(error).__name__})
        show("closed", active_disconnect_sent=False,
             cleanup_errors=close_errors,
             message=("观察接口已关闭；未修改蓝牙状态，未主动断开任何连接。" if not close_errors else
                      "观察结束；部分观察接口关闭失败，见 cleanup_errors；未主动断开任何连接。"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phone-file", required=True)
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--wait-seconds", type=float, default=20)
    parser.add_argument("--observe-seconds", type=float, default=60)
    args = parser.parse_args()
    if not re.fullmatch(r"hci[0-9]+", args.adapter) or int(args.adapter[3:]) >= 0xFFFF:
        parser.error("需要有效 hci 编号")
    if any(not math.isfinite(value) or not 0 < value <= 120 for value in (args.wait_seconds, args.observe_seconds)):
        parser.error("等待和观察时限须大于零且不超过 120 秒")
    logging.getLogger("dbus_fast").addHandler(logging.NullHandler())
    logging.getLogger("dbus_fast").propagate = False
    try:
        phone = address(Path(args.phone_file).read_text())
        return asyncio.run(observe(args, phone)) or 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        show("error", message="无法读取私有配置，或启动观察后端失败")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
