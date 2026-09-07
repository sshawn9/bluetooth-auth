"""BlueZ handoff and write-ahead recovery, invoked only by explicit run/restore.

This module never scans, pairs, connects a Device, removes a Device, edits BlueZ
storage, stops a daemon, or opens an HCI socket. The caller closes its HCI user
channel BEFORE invoking restore(). Imports alone perform no system operations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time

from state import KIND, atomic_json, check_path, read_json, sync_directory

ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
SAVED = (
    "Address", "Alias", "Powered", "Pairable", "PairableTimeout",
    "Discoverable", "DiscoverableTimeout", "Connectable",
)
SIGNATURES = {
    "Powered": "b", "Pairable": "b", "PairableTimeout": "u",
    "Discoverable": "b", "DiscoverableTimeout": "u", "Connectable": "b",
}
TRANSIENT_ERRORS = {
    "org.bluez.Error.Busy", "org.bluez.Error.InProgress", "org.bluez.Error.NotReady",
    "org.freedesktop.DBus.Error.UnknownObject",
}
POWER_TRANSITIONS = {"off-enabling", "on-disabling"}


class BlueZCallError(RuntimeError):
    def __init__(self, error_name: str, operation: str, details=None):
        self.error_name = error_name
        self.operation = operation
        self.details = details
        super().__init__(f"{operation}: {error_name}: {details}")


class BlueZBackend:
    def __init__(self):
        self.bus = None

    async def open(self):
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        self.bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), 5)
        return self

    async def close(self):
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None

    async def call(self, path, interface, member, signature="", body=None, destination="org.bluez"):
        from dbus_fast import Message, MessageFlag, MessageType

        reply = await asyncio.wait_for(self.bus.call(Message(
            destination=destination, path=path, interface=interface, member=member,
            signature=signature, body=body or [], flags=MessageFlag.NO_AUTOSTART,
        )), 5)
        if reply.message_type == MessageType.ERROR:
            raise BlueZCallError(reply.error_name, member, reply.body)
        return reply.body

    async def objects(self):
        raw = (await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"))[0]
        return {path: {interface: {key: value.value for key, value in props.items()}
                       for interface, props in interfaces.items()} for path, interfaces in raw.items()}

    async def set(self, path, name, value):
        from dbus_fast import Variant

        try:
            await self.call(path, "org.freedesktop.DBus.Properties", "Set", "ssv",
                            [ADAPTER, name, Variant(SIGNATURES[name], value)])
        except BlueZCallError as error:
            raise BlueZCallError(error.error_name, f"Set({name}={value!r})", error.details) from error


async def _owned_backend(backend):
    if backend is None:
        return await BlueZBackend().open(), True
    return backend, False


async def acquire(adapter: str, journal_path: Path, *, backend=None) -> dict:
    if not re.fullmatch(r"hci[0-9]+", adapter):
        raise ValueError("适配器必须明确指定为 hciN，例如 hci0；不会自动选择")
    journal_path = check_path(journal_path)
    if journal_path.exists() or journal_path.with_name(journal_path.name + ".tmp").exists():
        raise RuntimeError("有未完成的恢复记录；先运行 restore")
    backend, owned = await _owned_backend(backend)
    try:
        objects = await backend.objects()
        path = f"/org/bluez/{adapter}"
        if ADAPTER not in objects.get(path, {}):
            raise RuntimeError(f"BlueZ 中没有 {adapter}；没有修改任何适配器")
        props = objects[path][ADAPTER]
        for name in ("Address", "Alias", "Powered", "Pairable", "Discoverable", "PairableTimeout", "DiscoverableTimeout"):
            if name not in props:
                raise RuntimeError(f"无法保存原设置 {name}，取消实验")
        # Do not capture an in-flight/inconsistent baseline that BlueZ cannot
        # reproduce through its public API (e.g. Discoverable=True while off).
        if not props["Powered"] and (props["Discoverable"] or props.get("Connectable", False)):
            raise RuntimeError("已关闭的适配器仍标记可发现/可连接；请等待 BlueZ 状态稳定后重试，没有修改设置")
        if props["Discoverable"] and props.get("Connectable") is False:
            raise RuntimeError("适配器发现/连接状态不一致；无法可靠恢复，取消实验")
        previous = {name: props[name] for name in SAVED if name in props}
        connected = [info[DEVICE].get("Address") for info in objects.values()
                     if DEVICE in info and info[DEVICE].get("Adapter") == path and info[DEVICE].get("Connected")]
        journal = {
            "kind": KIND + "/adapter", "phase": "prepared", "captured_at": time.time(),
            "adapter": adapter, "address": previous["Address"], "properties": previous,
            "previously_connected": connected,
            "limits": ["原连接需由原系统/用户重连", "无法读取或还原可发现/可配对计时器的剩余时间"],
        }
        # Persist the baseline before even attempting a state change.
        atomic_json(journal_path, journal)
        if previous["Powered"]:
            await backend.set(path, "Powered", False)
        objects = await backend.objects()
        if objects.get(path, {}).get(ADAPTER, {}).get("Powered", False):
            raise RuntimeError("适配器没有关闭，不能安全交给实验栈；恢复记录已保留")
        journal["phase"] = "handed_off"
        atomic_json(journal_path, journal)
        return {"index": int(adapter[3:]), "address": journal["address"], "previously_connected": connected}
    finally:
        if owned:
            await backend.close()


def _read_journal(path: Path) -> dict:
    journal = read_json(path)
    props = journal.get("properties", {})
    if journal.get("kind") != KIND + "/adapter" or journal.get("address") != props.get("Address"):
        raise ValueError("恢复记录不合法，拒绝修改适配器")
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", str(journal.get("address", ""))):
        raise ValueError("恢复记录地址不合法")
    for name in ("Powered", "Pairable", "Discoverable"):
        if type(props.get(name)) is not bool:
            raise ValueError(f"恢复记录缺少布尔设置 {name}")
    for name in ("PairableTimeout", "DiscoverableTimeout"):
        if type(props.get(name)) is not int or not 0 <= props[name] <= 0xFFFFFFFF:
            raise ValueError(f"恢复记录缺少超时设置 {name}")
    if not isinstance(props.get("Alias"), str):
        raise ValueError("恢复记录缺少原名称")
    if "Connectable" in props and type(props["Connectable"]) is not bool:
        raise ValueError("恢复记录 Connectable 不合法")
    return journal


async def _restore_property(backend, address, name, value, deadline, emit):
    """Wait for BlueZ initialization and verify each Set, with a shared deadline."""
    last_reason = "等待原适配器重新出现"
    announced_reason = None
    while True:
        objects = await backend.objects()
        matches = [(path, interfaces[ADAPTER]) for path, interfaces in objects.items()
                   if ADAPTER in interfaces and interfaces[ADAPTER].get("Address", "").upper() == address.upper()]
        if len(matches) > 1:
            raise RuntimeError("原物理地址对应多个适配器；拒绝恢复到不明确的目标")
        if matches:
            path, current = matches[0]
            transitioning = current.get("PowerState") in POWER_TRANSITIONS
            if name in current and current[name] == value and not transitioning:
                return
            if transitioning:
                last_reason = f"PowerState={current['PowerState']}"
            elif name not in current:
                last_reason = "属性尚未出现"
            else:
                try:
                    await backend.set(path, name, value)
                    last_reason = "等待设置结果生效"
                except BlueZCallError as error:
                    if error.error_name not in TRANSIENT_ERRORS:
                        raise
                    last_reason = error.error_name
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise RuntimeError(f"恢复 {name}={value!r} 超时：{last_reason}；恢复记录保留")
        if last_reason != announced_reason:
            emit("restore_wait", {"property": name, "target": value, "reason": last_reason,
                                  "remaining_seconds": round(remaining, 1)})
            announced_reason = last_reason
        await asyncio.sleep(min(0.2, remaining))


async def restore(journal_path: Path, *, backend=None, timeout: float = 15, emit=None) -> dict:
    emit = emit or (lambda _event, _payload: None)
    journal_path = check_path(journal_path)
    if not journal_path.exists():
        temporary = journal_path.with_name(journal_path.name + ".tmp")
        if temporary.exists():
            # A complete write-ahead temporary file is itself a valid baseline.
            # Recover it instead of permanently blocking clean after an interrupted rename.
            recovered = _read_journal(temporary)
            atomic_json(journal_path, recovered)
        else:
            return {"settings_restored": True, "changed": False, "message": "无待恢复记录；没有访问蓝牙"}
    journal = _read_journal(journal_path)
    backend, owned = await _owned_backend(backend)
    try:
        end = asyncio.get_running_loop().time() + timeout
        while True:
            objects = await backend.objects()
            matches = [(path, interfaces[ADAPTER]) for path, interfaces in objects.items()
                       if ADAPTER in interfaces and interfaces[ADAPTER].get("Address", "").upper() == journal["address"].upper()]
            if len(matches) == 1:
                path, current = matches[0]
                break
            if len(matches) > 1 or asyncio.get_running_loop().time() >= end:
                raise RuntimeError("未能按原物理地址唯一找到适配器；保留恢复记录，请释放实验进程后重试 restore")
            await asyncio.sleep(0.1)
        previous = journal["properties"]
        journal["phase"] = "restoring"
        atomic_json(journal_path, journal)
        # Alias is not changed by this experiment. Never overwrite an external rename.
        if current.get("Alias") != previous["Alias"]:
            raise RuntimeError("原适配器名称已被其他操作改变；不会覆盖，恢复记录保留")
        order = ["Powered", "PairableTimeout", "DiscoverableTimeout", "Pairable"]
        order.extend(name for name in ("Connectable", "Discoverable") if name in previous)
        for name in order:
            await _restore_property(backend, journal["address"], name, previous[name], end, emit)
        # Verify the postcondition instead of trusting successful Set replies.
        objects = await backend.objects()
        matches = [(candidate_path, interfaces[ADAPTER]) for candidate_path, interfaces in objects.items()
                   if ADAPTER in interfaces and interfaces[ADAPTER].get("Address", "").upper() == journal["address"].upper()]
        if len(matches) != 1:
            raise RuntimeError("恢复核对时无法唯一找到原适配器；恢复记录保留")
        path, current = matches[0]
        mismatch = [name for name in previous if current.get(name) != previous[name]]
        if mismatch:
            raise RuntimeError(f"恢复核对失败：{', '.join(mismatch)}；恢复记录保留")
        now_connected = {info[DEVICE].get("Address") for info in objects.values()
                         if DEVICE in info and info[DEVICE].get("Adapter") == path and info[DEVICE].get("Connected")}
        missing = [address for address in journal["previously_connected"] if address not in now_connected]
        result = {
            "settings_restored": True, "changed": True, "adapter": path.rsplit("/", 1)[-1],
            "connections_needing_check": missing, "limits": journal["limits"],
        }
        journal_path.unlink()
        temporary = journal_path.with_name(journal_path.name + ".tmp")
        if temporary.exists():
            temporary.unlink()
        sync_directory(journal_path.parent)
        return result
    finally:
        if owned:
            await backend.close()
