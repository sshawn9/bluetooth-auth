#!/usr/bin/env python3
"""无 HID 实验：建立加密 LE，使用手机 CTS/Battery 服务，观察同一连接。"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True

from adapter import BlueZCallError
from bluez_hid_lab import ADAPTER, AD_MANAGER, DEVICE, Backend
from coexist_link import open as open_link
from diagnostics.le_bearer_probe import (
    adapter_index,
    connection_snapshot,
    load_target,
    redact,
)
from hid_release_test import Inconclusive, LinkLost, target, uuids

LE = "org.bluez.Bearer.LE1"
SERVICE = "org.bluez.GattService1"
CHARACTERISTIC = "org.bluez.GattCharacteristic1"
UUID_BASE = "-0000-1000-8000-00805f9b34fb"
PROFILES = {"cts": ("00001805", "00002a2b", 10), "battery": ("0000180f", "00002a19", 1)}


def emit(event, **fields):
    print(
        json.dumps(
            {
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "event": event,
                **fields,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def check_resources(objects, adapter_path):
    interfaces = objects.get(adapter_path, {})
    adapter = interfaces.get(ADAPTER, {})
    advertisements = interfaces.get(AD_MANAGER, {}).get("ActiveInstances")
    if not adapter.get("Powered"):
        raise Inconclusive("目标适配器尚未开启")
    if "UUIDs" not in adapter or type(advertisements) is not int:
        raise Inconclusive("无法确认本机 HID 和广播状态")
    if "1812" in uuids(adapter) or advertisements != 0:
        raise Inconclusive("检测到本机 HID 或 LE 广播；请停止其他连接/配对实验后再测")


def find_characteristic(objects, device_path, profile):
    service_uuid, characteristic_uuid, _ = PROFILES[profile]
    services = {
        path
        for path, interfaces in objects.items()
        if (props := interfaces.get(SERVICE, {})).get("Device") == device_path
        and props.get("UUID", "").lower() == service_uuid + UUID_BASE
    }
    matches = [
        path
        for path, interfaces in objects.items()
        if (props := interfaces.get(CHARACTERISTIC, {})).get("Service") in services
        and props.get("UUID", "").lower() == characteristic_uuid + UUID_BASE
        and {"read", "notify"}.issubset(props.get("Flags", []))
    ]
    if len(matches) > 1:
        raise Inconclusive("手机上存在多个匹配特征，无法唯一选择")
    return matches[0] if matches else None


async def connect_le(backend, path, timeout):
    """只请求 LE；不使用可能连入经典蓝牙的 Device1.Connect 回退。"""
    from dbus_fast import Message, MessageFlag, MessageType

    reply = await asyncio.wait_for(
        backend.bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface=LE,
                member="Connect",
                flags=MessageFlag.NO_AUTOSTART,
            )
        ),
        timeout,
    )
    if reply.message_type == MessageType.ERROR:
        if reply.error_name not in {
            "org.bluez.Error.AlreadyConnected",
            "org.bluez.Error.InProgress",
        }:
            raise BlueZCallError(reply.error_name, "LE.Connect")


async def run(args, raw_address):
    phone = ":".join(f"{octet:02X}" for octet in raw_address[::-1])
    index, adapter_path = adapter_index(args.adapter), "/org/bluez/" + args.adapter
    backend, mgmt, characteristic = Backend(), None, None
    subscribed, notifications, handle = False, 0, None
    phase, setup, started = "prepare", None, None
    verdict, status, reason = "inconclusive", 2, "测试未完成"
    event_cursor, observed_seconds, cleanup_errors = 0, 0, []

    def record_disconnects(cleanup=False):
        nonlocal event_cursor
        if mgmt is None:
            return False
        events = mgmt.disconnect_events[event_cursor:]
        event_cursor += len(events)
        events = [
            event
            for event in events
            if event["address"] == phone and event["address_type"] in (1, 2)
        ]
        for event in events:
            emit(
                "disconnected",
                reason_source="mgmt",
                reason_code=event["reason"],
                during_cleanup=cleanup,
            )
        return bool(events)

    try:
        await backend.open()
        mgmt = await open_link(index)
        objects = await backend.objects()
        check_resources(objects, adapter_path)
        phone_path, phone_info = target(objects, adapter_path, phone)
        le_info = objects[phone_path].get(LE, {})
        if not (le_info.get("Paired") and le_info.get("Bonded")):
            raise Inconclusive(
                "目标需要已有 LE 配对；本脚本不执行首次配对，不要求 Trusted=true"
            )
        saved_pairing = {
            key: phone_info.get(key)
            for key in ("Paired", "Bonded", "Trusted", "Blocked")
        }

        async def snapshot():
            nonlocal notifications
            if mgmt._closed:
                raise Inconclusive("MGMT 观察接口已关闭，无法可靠记录断线")
            objects = await backend.objects()
            if record_disconnects() and handle is not None:
                raise LinkLost("目标 LE 已断开；即使随后自动连回也不算持续保持")
            changes, backend.changes = backend.changes, []
            check_resources(objects, adapter_path)
            for path, interface, changed in changes:
                if path == adapter_path and (
                    interface == ADAPTER
                    and "1812" in uuids(changed)
                    or interface == AD_MANAGER
                    and changed.get("ActiveInstances", 0) > 0
                ):
                    raise Inconclusive("观察到其他程序临时注册 HID/广播，本轮受到干扰")
                if subscribed and path == characteristic:
                    if "Value" in changed:
                        notifications += 1
                        emit(
                            "value_changed", service=args.service, updates=notifications
                        )
                    if changed.get("Notifying") is False or changed.get("removed"):
                        raise LinkLost("手机服务的通知订阅已结束")
            current = objects.get(phone_path, {}).get(DEVICE, {})
            if str(current.get("Address", "")).upper() != phone:
                raise Inconclusive("BlueZ 目标身份发生变化")
            if {key: current.get(key) for key in saved_pairing} != saved_pairing:
                raise Inconclusive("目标配对或信任属性在测试中发生变化")
            links = connection_snapshot(raw_address, index)
            if any(link["transport"] == "BR/EDR" for link in links):
                raise Inconclusive("目标存在经典连接，不能把本轮 GATT 使用归为仅 LE")
            links = [
                link
                for link in links
                if link["transport"] == "LE" and link["connected"]
            ]
            if len(links) > 1:
                raise Inconclusive("目标 LE 连接不唯一")
            if handle is not None and (
                len(links) != 1
                or links[0]["handle"] != handle
                or not links[0]["encrypted"]
            ):
                raise LinkLost("原 LE 连接消失、句柄改变或加密失效")
            if subscribed and not objects.get(characteristic, {}).get(
                CHARACTERISTIC, {}
            ).get("Notifying"):
                raise LinkLost("无法确认手机服务仍处于订阅状态")
            return objects, links

        objects, links = await snapshot()
        setup = "existing" if links else "active"
        emit(
            "initial_state",
            connection_setup=setup,
            service=args.service,
            paired=True,
            bonded=True,
            trusted=phone_info.get("Trusted"),
            le_connected=bool(links),
            encrypted=bool(links and links[0]["encrypted"]),
            hid_present=False,
            advertising_instances=0,
        )

        phase = "connect"
        async with asyncio.timeout(args.wait_seconds):
            if not links:
                emit(
                    "connecting",
                    timeout_seconds=args.wait_seconds,
                    message="不注册 HID、不广播；只请求一次目标 LE 连接，手机保持锁屏。",
                )
                await connect_le(backend, phone_path, args.wait_seconds)
            while not (links and links[0]["encrypted"]):
                await asyncio.sleep(0.2)
                objects, links = await snapshot()
        handle = links[0]["handle"]
        emit("encrypted_link", connection_setup=setup)

        if args.service != "none":
            phase = "discover"
            emit("discovering", service=args.service, timeout_seconds=args.wait_seconds)
            async with asyncio.timeout(args.wait_seconds):
                while characteristic is None:
                    objects, _ = await snapshot()
                    characteristic = find_characteristic(
                        objects, phone_path, args.service
                    )
                    if characteristic is None:
                        await asyncio.sleep(0.2)
            phase = "read"
            value = (
                await backend.connection.call(
                    characteristic, CHARACTERISTIC, "ReadValue", "a{sv}", [{}]
                )
            )[0]
            if (
                len(value) != PROFILES[args.service][2]
                or args.service == "battery"
                and value[0] > 100
            ):
                raise Inconclusive("手机特征返回的值格式不符合所选服务")
            emit("read_complete", service=args.service, byte_count=len(value))
            # 排除本轮 ReadValue 产生的缓存变化；后续只统计变化次数，不记录值。
            await snapshot()
            phase = "subscribe"
            await backend.connection.call(characteristic, CHARACTERISTIC, "StartNotify")
            subscribed = True
            emit("subscribed", service=args.service)

        phase, started = "observe", time.monotonic()
        emit(
            "observing",
            service=args.service,
            observe_seconds=args.observe_seconds,
            message="保持手机锁屏；每分钟报告状态，断线即结束，不重试或发送周期性读取。",
        )
        next_progress = started
        while True:
            await snapshot()
            elapsed = time.monotonic() - started
            if elapsed >= args.observe_seconds:
                verdict, status, reason = (
                    "passed",
                    0,
                    "本轮观察期间，同一加密 LE 连接持续保持",
                )
                break
            if time.monotonic() >= next_progress:
                emit(
                    "holding",
                    elapsed_seconds=round(elapsed, 1),
                    encrypted=True,
                    same_le_link=True,
                    subscribed=subscribed,
                    value_updates=notifications,
                )
                next_progress = time.monotonic() + 60
            await asyncio.sleep(min(1, args.observe_seconds - elapsed))
    except LinkLost as error:
        verdict, status, reason = "failed", 1, str(error)
    except Inconclusive as error:
        reason = str(error)
    except TimeoutError:
        reason = "阶段超时；未完成该阶段，不回退 HID、不重试"
    except asyncio.CancelledError:
        verdict, status, reason = "cancelled", 130, "用户中止；不计为完整观察通过"
    except Exception as error:
        reason = redact(
            f"{type(error).__name__}: {getattr(error, 'error_name', None) or error}"
        )
    finally:
        if started is not None:
            observed_seconds = round(time.monotonic() - started, 1)
        if subscribed:
            try:
                await backend.connection.call(
                    characteristic, CHARACTERISTIC, "StopNotify"
                )
            except Exception as error:
                cleanup_errors.append(
                    {"operation": "StopNotify", "error": type(error).__name__}
                )
        record_disconnects(cleanup=True)
        for resource in (backend, mgmt):
            if resource is not None:
                try:
                    await resource.close()
                except Exception as error:
                    cleanup_errors.append(
                        {"operation": "close", "error": type(error).__name__}
                    )

    if cleanup_errors and status == 0:
        verdict, status, reason = (
            "inconclusive",
            2,
            "观察完成，但本轮资源清理未全部确认",
        )
    emit(
        "result",
        verdict=verdict,
        phase=phase,
        reason=reason,
        connection_setup=setup,
        service=args.service,
        observed_seconds=observed_seconds,
        value_updates=notifications,
    )
    emit(
        "closed",
        active_disconnect_sent=False,
        cleanup_errors=cleanup_errors,
        message="已释放本轮订阅和观察接口；保留已有配对及仍存在的连接。",
    )
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address-file", required=True)
    parser.add_argument("--adapter", default="hci0")
    parser.add_argument("--service", choices=("cts", "battery", "none"), default="cts")
    parser.add_argument("--wait-seconds", type=float, default=30)
    parser.add_argument("--observe-seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    if any(
        not math.isfinite(value) or not 0 < value <= 86400
        for value in (args.wait_seconds, args.observe_seconds)
    ):
        parser.error("等待和观察秒数须大于零且不超过 86400")
    logging.getLogger("dbus_fast").addHandler(logging.NullHandler())
    logging.getLogger("dbus_fast").propagate = False
    try:
        adapter_index(args.adapter)
        raw = load_target(args.address_file)
        # 与正式程序共用锁，防止测试中途出现临时 HID。不存在时无需创建生产设施。
        lock_path = Path("/run/bluetooth-auth") / (args.adapter + ".lock")
        fd = (
            os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
            if lock_path.exists()
            else None
        )
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return asyncio.run(run(args, raw))
        finally:
            if fd is not None:
                os.close(fd)
    except KeyboardInterrupt:
        return 130
    except BlockingIOError:
        emit(
            "result",
            verdict="inconclusive",
            phase="prepare",
            reason="连接锁已占用，请先结束其他连接测试",
        )
        return 2
    except Exception as error:
        emit(
            "result", verdict="inconclusive", phase="prepare", reason=redact(str(error))
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
