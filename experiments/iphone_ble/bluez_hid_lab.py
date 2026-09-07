#!/usr/bin/env python3
"""Short, user-operated HID coexistence test on the existing BlueZ adapter."""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from state import atomic_json, check_path, read_json, sync_directory

HERE = Path(__file__).absolute().parent
KIND = "bluetooth-auth/bluez-hid-coexist/v1"
ADAPTER = "org.bluez.Adapter1"
DEVICE = "org.bluez.Device1"
AD_MANAGER = "org.bluez.LEAdvertisingManager1"
GATT_MANAGER = "org.bluez.GattManager1"
MEDIA_TRANSPORT = "org.bluez.MediaTransport1"
PROPS = ("Address", "Alias", "Powered", "Pairable", "PairableTimeout",
         "Discoverable", "DiscoverableTimeout", "Connectable")
PHONE_PROPS = ("Paired", "Bonded", "Trusted", "Blocked")
ROOT_PATH = "/org/bluetooth_auth/coexist"
APP_PATH = ROOT_PATH + "/app"
AD_PATH = ROOT_PATH + "/advertisement"
AGENT_PATH = ROOT_PATH + "/agent"
AGENT_MANAGER = "org.bluez.AgentManager1"
POLL_SECONDS = 0.25
PROGRESS_SECONDS = 5
HID_UUID = "00001812-0000-1000-8000-00805f9b34fb"


class ObservationIncomplete(RuntimeError):
    """A live link alone cannot prove the required target GATT operation."""


def show(event: str, details: dict) -> None:
    print(json.dumps({"event": event, "mode": "bluez-hid-coexist", **details},
                     ensure_ascii=False, indent=2), flush=True)


def address(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", value):
        raise ValueError("手机地址必须是六组十六进制字节")
    return value


def configured_phone(args) -> str:
    if args.phone:
        return address(args.phone)
    if args.phone_file is None:
        raise ValueError("请用 --phone-file 指定地址文件，或设置 BLUETOOTH_AUTH_ADDRESS_FILE")
    try:
        value = args.phone_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ValueError("无法读取配置的手机地址文件；请检查文件和权限") from None
    return address(value)


class State:
    def __init__(self, root: Path):
        self.root = check_path(root)
        self.journal = self.root / "restore.json"

    def pending(self) -> bool:
        return self.journal.exists() or self.journal.with_suffix(".json.tmp").exists()

    @contextlib.contextmanager
    def lock(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise RuntimeError("共存测试状态目录必须为 0700")
        with contextlib.ExitStack() as stack:
            # Share the old lab's lock if present, without reading its keys or
            # creating an isolated identity. The old entrypoint also checks ours.
            old_root = HERE / ".runtime"
            for journal in (old_root / "adapter-restore.json", old_root / "adapter-restore.json.tmp"):
                if journal.exists():
                    raise RuntimeError("独占实验有待恢复记录；先执行 ble_lab.py restore")
            paths = [self.root / "run.lock"]
            old_lock = old_root / "run.lock"
            if old_lock.exists():
                paths.append(old_lock)
            for path in paths:
                fd = os.open(check_path(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                stack.callback(os.close, fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RuntimeError("已有实验运行；拒绝并行测试或恢复") from error
            yield

    def emit(self, event: str, details: dict) -> None:
        fd = os.open(check_path(self.root / "events.jsonl"),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), "event": event, **details}, ensure_ascii=False) + "\n")
        show(event, details)

    def save(self, journal: dict) -> None:
        atomic_json(self.journal, journal)

    def load(self) -> dict:
        path = self.journal if self.journal.exists() else self.journal.with_suffix(".json.tmp")
        journal = read_json(path)
        if journal.get("kind") != KIND or journal.get("root") != str(self.root):
            raise ValueError("不是本共存测试的恢复记录")
        props = journal.get("properties", {})
        if address(journal["address"]) != props.get("Address"):
            raise ValueError("适配器恢复记录地址不一致")
        address(journal["phone"])
        if not isinstance(props.get("Alias"), str) or props.get("Powered") is not True:
            raise ValueError("恢复记录缺少原名称或电源状态")
        if type(props.get("Pairable")) is not bool:
            raise ValueError("恢复记录 Pairable 不合法")
        if not isinstance(journal.get("phone_properties"), dict):
            raise ValueError("恢复记录缺少原配对状态")
        for peer, kind in journal["baseline_links"]:
            address(peer)
            if type(kind) is not int or kind not in (0, 1, 2):
                raise ValueError("恢复记录连接类型不合法")
        if not re.fullmatch(r":\d+\.\d+", journal.get("owner", "")):
            raise ValueError("恢复记录缺少 D-Bus 所有者")
        if not isinstance(journal.get("bus_id"), str):
            raise ValueError("恢复记录缺少 D-Bus 标识")
        if type(journal.get("repair_phone_pairing", False)) is not bool:
            raise ValueError("恢复记录配对模式不合法")
        for peer in journal.get("pairing_addresses", []):
            address(peer)
        candidate = journal.get("pairing_candidate_path")
        if candidate is not None and not re.fullmatch(r"/org/bluez/hci\d+/dev_(?:[0-9A-F]{2}_){5}[0-9A-F]{2}", candidate):
            raise ValueError("恢复记录配对候选路径不合法")
        return journal

    def complete(self) -> None:
        for path in (self.journal, self.journal.with_suffix(".json.tmp")):
            if path.exists():
                path.unlink()
        sync_directory(self.root)


class Backend:
    """D-Bus operations only; no StartDiscovery/Connect/Pair/RemoveDevice."""
    def __init__(self):
        self.connection = None
        self.bus = None
        self.bluez_owner = None
        self.bus_id = None
        self.changes: list[tuple[str, str, dict]] = []
        self.dead = False

    async def open(self):
        from adapter import BlueZBackend
        self.connection = await BlueZBackend().open()
        self.bus = self.connection.bus
        try:
            self.bluez_owner = (await self.dbus("GetNameOwner", "s", ["org.bluez"]))[0]
            self.bus_id = (await self.dbus("GetId"))[0]
            self.bus.add_message_handler(self._message)
            for rule in (
                "type='signal',sender='org.bluez',path_namespace='/org/bluez'",
                "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'",
            ):
                await self.dbus("AddMatch", "s", [rule])
            return self
        except BaseException:
            await self.close()
            raise

    async def dbus(self, member, signature="", body=None):
        return await self.connection.call("/org/freedesktop/DBus", "org.freedesktop.DBus",
                                          member, signature, body, destination="org.freedesktop.DBus")

    def _message(self, message):
        from dbus_fast import Message, MessageType
        if message.message_type == MessageType.METHOD_CALL and (
            message.path == ROOT_PATH or (message.path or "").startswith(ROOT_PATH + "/")
        ) and message.sender != self.bluez_owner:
            return Message.new_error(message, "org.freedesktop.DBus.Error.AccessDenied", "Only BlueZ may call this test service")
        if message.message_type != MessageType.SIGNAL:
            return None
        if message.sender == "org.freedesktop.DBus" and message.interface == "org.freedesktop.DBus" and message.member == "NameOwnerChanged":
            if message.body[0] == "org.bluez" and message.body[2] != self.bluez_owner:
                self.dead = True
        if message.sender != self.bluez_owner:
            return None
        if message.interface == "org.freedesktop.DBus.Properties" and message.member == "PropertiesChanged":
            interface, properties, _invalid = message.body
            self.changes.append((message.path, interface, {name: value.value for name, value in properties.items()}))
        elif message.interface == "org.freedesktop.DBus.ObjectManager" and message.member == "InterfacesRemoved":
            path, interfaces = message.body
            for interface in interfaces:
                self.changes.append((path, interface, {"removed": True}))
        return None

    async def objects(self):
        if self.dead:
            raise RuntimeError("测试期间 BlueZ 重启或退出")
        return await self.connection.objects()

    async def set_pairable(self, path: str, value: bool):
        await self.connection.set(path, "Pairable", value)

    async def register(self, adapter_path: str, interface: str, path: str):
        member = "RegisterAdvertisement" if interface == AD_MANAGER else "RegisterApplication"
        await self.connection.call(adapter_path, interface, member, "oa{sv}", [path, {}])

    async def unregister(self, adapter_path: str, interface: str, path: str):
        from adapter import BlueZCallError
        member = "UnregisterAdvertisement" if interface == AD_MANAGER else "UnregisterApplication"
        try:
            await self.connection.call(adapter_path, interface, member, "o", [path])
        except BlueZCallError as error:
            if error.error_name not in {"org.bluez.Error.DoesNotExist", "org.bluez.Error.NotReady",
                                        "org.freedesktop.DBus.Error.UnknownObject"}:
                raise

    async def register_agent(self):
        await self.connection.call("/org/bluez", AGENT_MANAGER, "RegisterAgent",
                                   "os", [AGENT_PATH, "DisplayYesNo"])
        await self.connection.call("/org/bluez", AGENT_MANAGER, "RequestDefaultAgent",
                                   "o", [AGENT_PATH])

    async def unregister_agent(self):
        from adapter import BlueZCallError
        try:
            await self.connection.call("/org/bluez", AGENT_MANAGER, "UnregisterAgent", "o", [AGENT_PATH])
        except BlueZCallError as error:
            if error.error_name not in {"org.bluez.Error.DoesNotExist", "org.bluez.Error.NotReady",
                                        "org.freedesktop.DBus.Error.UnknownObject"}:
                raise

    async def owner_alive(self, owner: str) -> bool:
        return (await self.dbus("NameHasOwner", "s", [owner]))[0]

    async def close(self):
        if self.connection is not None:
            if self.bus is not None:
                self.bus.remove_message_handler(self._message)
            await self.connection.close()
            self.connection = self.bus = None


def find_adapter(objects: dict, *, name: str | None = None, physical: str | None = None):
    candidates = [(path, interfaces[ADAPTER]) for path, interfaces in objects.items()
                  if ADAPTER in interfaces and (path == f"/org/bluez/{name}" if name else
                      interfaces[ADAPTER].get("Address", "").upper() == physical)]
    if len(candidates) != 1:
        raise RuntimeError("无法唯一找到指定的 BlueZ 适配器")
    return candidates[0]


def find_phone(objects: dict, adapter_path: str, phone: str, *, preferred: str | None = None):
    matches = [(path, interfaces[DEVICE]) for path, interfaces in objects.items()
               if DEVICE in interfaces and interfaces[DEVICE].get("Adapter") == adapter_path
               and interfaces[DEVICE].get("Address", "").upper() == phone]
    # BlueZ can retain both the RPA object and the original identity object
    # after merging their properties. Prefer the explicitly recorded object.
    for wanted in (preferred, adapter_path + "/dev_" + phone.replace(":", "_")):
        for item in matches:
            if item[0] == wanted:
                return item
    if len(matches) != 1:
        raise RuntimeError("BlueZ 中没有唯一的目标手机记录；不会扫描、创建配对或连接其他手机")
    return matches[0]


def links_json(links):
    return [{"address": peer, "transport": "bredr" if kind == 0 else "le", "address_type": kind}
            for peer, kind in sorted(links)]


async def restore(state: State, *, backend_factory=Backend, link_factory=None) -> dict:
    """Close only added target LE links, restore Pairable, verify all originals."""
    if not state.pending():
        return {"settings_restored": True, "changed": False, "message": "无待恢复记录；没有访问蓝牙"}
    if link_factory is None:
        from coexist_link import open as link_factory
    journal = state.load()
    backend = await backend_factory().open()
    link = None
    try:
        if backend.bus_id == journal["bus_id"] and await backend.owner_alive(journal["owner"]):
            raise RuntimeError("原共存测试的 D-Bus 连接仍在；先退出该进程，保留恢复记录")
        objects = await backend.objects()
        path, props = find_adapter(objects, physical=journal["address"])
        repair_pairing = journal.get("repair_phone_pairing", False)
        cleanup_peers = {journal["phone"]}
        if repair_pairing:
            cleanup_peers.update(journal.get("pairing_addresses", []))
            candidate_path = journal.get("pairing_candidate_path")
            if candidate_path:
                candidate_path = path + "/" + candidate_path.rsplit("/", 1)[-1]
                candidate = objects.get(candidate_path, {}).get(DEVICE, {})
                if candidate.get("Adapter") == path:
                    cleanup_peers.add(address(candidate["Address"]))
        index = int(path.rsplit("hci", 1)[-1])
        baseline = {tuple(item) for item in journal["baseline_links"]}
        link = await link_factory(index, baseline=baseline)
        current = await link.connections()
        removed = []
        for peer, kind in sorted(current - baseline):
            if peer in cleanup_peers and kind in (1, 2):
                await link.disconnect_le(peer, kind)
                removed.append((peer, kind))
        # The test only deliberately changes Pairable. Do not power-cycle or
        # rewrite a name/other application setting to make a report look clean.
        if props.get("Pairable") != journal["properties"]["Pairable"]:
            await backend.set_pairable(path, journal["properties"]["Pairable"])
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            objects = await backend.objects()
            path, props = find_adapter(objects, physical=journal["address"])
            current = await link.connections()
            added_target = {(peer, kind) for peer, kind in current - baseline
                            if peer in cleanup_peers and kind in (1, 2)}
            hid_present = HID_UUID in {uuid.lower() for uuid in props.get("UUIDs", [])}
            active_advertisements = objects.get(path, {}).get(AD_MANAGER, {}).get("ActiveInstances", 0)
            ads_released = active_advertisements <= journal.get("advertising_instances", 0)
            if not added_target and not hid_present and ads_released and props.get("Pairable") == journal["properties"]["Pairable"]:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("目标新增 LE 连接、HID 服务、广播计数或 Pairable 尚未恢复；保留恢复记录")
            await asyncio.sleep(POLL_SECONDS)
        changed = [key for key, value in journal["properties"].items() if props.get(key) != value]
        try:
            _, phone = find_phone(objects, path, journal["phone"], preferred=journal.get("phone_path"))
            pairing_changed = [key for key, value in journal["phone_properties"].items() if phone.get(key) != value]
        except RuntimeError:
            pairing_changed = ["device_record_missing"]
        disallowed_pairing_changes = [key for key in pairing_changed
                                     if not repair_pairing or key not in {"Paired", "Bonded"}]
        if changed or disallowed_pairing_changes:
            raise RuntimeError(f"恢复核对未通过：adapter={changed}, pairing={pairing_changed}；未强行改写配对记录，保留记录")
        missing = baseline - current
        added_classic = {(peer, kind) for peer, kind in current - baseline if peer in cleanup_peers and kind == 0}
        # An original connection drop cannot be repaired without an active
        # reconnect. Report it; never conceal it by calling Device1.Connect.
        result = {"settings_restored": True, "changed": True,
                  "adapter": path.rsplit("/", 1)[-1], "alias": props["Alias"],
                  "test_bus_released": True, "new_phone_le_disconnected": links_json(removed),
                  "test_hid_service_removed": True, "advertising_instances": active_advertisements,
                  "original_connections_present": not missing,
                  "connections_restored": not missing and not added_classic,
                  "connections_needing_check": links_json(missing), "pairing_properties_unchanged": not pairing_changed,
                  "new_phone_classic_connections": links_json(added_classic),
                  "original_links_interrupted": links_json({tuple(item) for item in journal.get("original_links_interrupted", [])})}
        if repair_pairing:
            result.update(pairing_records="retained", pairing_keys_restored=False,
                          test_pairing_agent_removed=True,
                          message="广播、HID 和配对代理已撤销，适配器设置已恢复；本轮 BlueZ 配对记录保留")
        if added_classic:
            result["message"] = "存在本轮新增的手机经典蓝牙连接；未擅自断开，恢复记录保留"
        if not missing and not added_classic:
            state.complete()
        else:
            result["recovery_record_retained"] = True
        return result
    finally:
        try:
            if link is not None:
                await link.close()
        finally:
            await backend.close()


async def run(args, state: State, *, backend_factory=Backend, link_factory=None,
              app_factory=None, ad_factory=None, trace_factory=None, confirm_pairing=None):
    if state.pending():
        raise RuntimeError("共存测试有待恢复记录；先执行 bluez_hid_lab.py restore")
    if link_factory is None:
        from coexist_link import open as link_factory
    if app_factory is None or ad_factory is None:
        from coexist_gatt import HidApplication, Advertising
        app_factory, ad_factory = HidApplication, Advertising
    phone_address = configured_phone(args)
    backend = None
    link = None
    trace = None
    agent = None
    attempted_agent = False
    repair_pairing = getattr(args, "repair_phone_pairing", False)
    app = ad = None
    attempted_gatt = attempted_ad = False
    path = None
    result: dict[str, Any] = {"passed": False, "reports_sent": 0,
                             "scope": "encrypted_phone_hid_access_and_existing_connections",
                             "phone_hid_subscription_verified": False,
                             "phone_audio_isolation_verified": False}
    restoration = None
    phase = "preflight"
    status = 1
    objects = {}
    hid_access = asyncio.Event()
    unexpected_release = False
    target_path = None
    lost_links = set()
    new_phone_audio = set()
    baseline_audio = set()
    journal = None
    baseline = set()
    access_disconnect_count = None
    report_subscription_seen = False
    candidate_path = None
    pairing_addresses = set()
    identity_verified = not repair_pairing

    def remember_candidate():
        if candidate_path is None or journal is None:
            return
        info = objects.get(candidate_path, {}).get(DEVICE, {})
        if info.get("Adapter") == path and info.get("Address"):
            peer = address(info["Address"])
            if peer not in pairing_addresses:
                pairing_addresses.add(peer)
                journal["pairing_addresses"] = sorted(pairing_addresses)
                state.save(journal)

    def phone_disconnect_count():
        return sum(event["address"] in ({phone_address} | pairing_addresses) and event["address_type"] in (1, 2)
                   for event in (link.disconnect_events if link is not None else ()))

    def allow_device(device_path):
        info = objects.get(device_path, {}).get(DEVICE, {})
        if info.get("Adapter") != path or info.get("Blocked"):
            return False
        if repair_pairing:
            # Numeric Comparison binds a previously unknown RPA to the phone
            # selected by the user; the verdict still requires identity match.
            return bool(agent and agent.accepted and
                        (device_path == agent.confirmed_device or info.get("Address", "").upper() == phone_address))
        return info.get("Address", "").upper() == phone_address and info.get("Paired", False)

    async def allow_pairing(device_path):
        nonlocal objects
        objects = await backend.objects()
        info = objects.get(device_path, {}).get(DEVICE, {})
        if info.get("Adapter") != path or info.get("Blocked"):
            return False
        if candidate_path is not None and device_path != candidate_path:
            return False
        peer = info.get("Address", "").upper()
        if peer != phone_address and (info.get("Paired") or info.get("Bonded") or info.get("Trusted")
                                       or any(item[0] == peer for item in baseline)):
            if device_path != candidate_path:
                return False
        session_peers = {peer} | (pairing_addresses if device_path == candidate_path else set())
        current = await link.connections()
        return any(item[0] in session_peers and item[1] in (1, 2) for item in current)

    def emit(event, payload):
        nonlocal unexpected_release, access_disconnect_count, report_subscription_seen, candidate_path
        if event == "pairing_confirmation_requested":
            candidate_path = payload["device"]
            journal["pairing_candidate_path"] = candidate_path
            state.save(journal)
            remember_candidate()
        if event == "pairing_confirmation_accepted":
            journal["pairing_confirmation_accepted"] = True
            state.save(journal)
        if event == "target_gatt_access" and allow_device(payload.get("device")) and payload.get("link", "").lower() == "le":
            hid_access.set()
            access_disconnect_count = phone_disconnect_count()
        if event == "gatt_subscription" and payload.get("attribute") == "report":
            report_subscription_seen = True
        if event == "advertisement_released" and phase not in ("cleanup", "restore"):
            unexpected_release = True
        # Values of reports, time, PairingKeys, and the phone address file never
        # enter this event log. Device paths are converted into a role label.
        payload = dict(payload)
        if "device" in payload:
            payload["device"] = ("target_phone" if allow_device(payload["device"]) else
                                 "pairing_candidate" if payload["device"] == candidate_path else "other_device")
        state.emit(event, payload)

    try:
        backend = await backend_factory().open()
        objects = await backend.objects()
        path, props = find_adapter(objects, name=args.adapter)
        if props.get("Powered") is not True or props.get("PowerState", "on") != "on":
            raise RuntimeError("适配器未稳定开启；不会替你开关电源")
        if GATT_MANAGER not in objects[path] or AD_MANAGER not in objects[path]:
            raise RuntimeError("BlueZ 未提供 GATT/广播管理接口")
        if any(key not in props for key in PROPS if key != "Connectable"):
            raise RuntimeError("无法完整读取适配器原设置，取消测试")
        uuids = {item.lower() for item in props.get("UUIDs", [])}
        if HID_UUID in uuids:
            raise RuntimeError("适配器已经提供 HID 服务；不会叠加第二份")
        if props["Pairable"] and props["PairableTimeout"]:
            raise RuntimeError("适配器正处于限时配对窗口，无法恢复其剩余计时；请待窗口结束后测试")
        target_path, phone = find_phone(objects, path, phone_address)
        if phone.get("Blocked"):
            raise RuntimeError("目标手机被阻止；不会为实验解除阻止")
        if not repair_pairing and (not phone.get("Paired") or not phone.get("Bonded", phone.get("Paired"))):
            raise RuntimeError("目标手机必须已有可用的 BlueZ 配对；不会重新配对或解除阻止")
        if not phone.get("Trusted"):
            raise RuntimeError("原手机尚未 Trusted；不会为实验修改原信任设置")
        link = await link_factory(int(args.adapter[3:]))
        baseline = await link.connections()
        companions = {(peer, kind) for peer, kind in baseline if peer != phone_address}
        if not companions:
            raise RuntimeError("共存测试需先让电脑连着一台已有蓝牙耳机或鼠标；没有注册服务或广播")
        baseline_audio = {object_path for object_path, interfaces in objects.items()
                          if interfaces.get(MEDIA_TRANSPORT, {}).get("Device") == target_path}
        journal = {"kind": KIND, "root": str(state.root), "captured_at": time.time(),
                   "adapter": args.adapter, "address": props["Address"],
                   "properties": {key: props[key] for key in PROPS if key in props},
                   "phone": phone_address, "phone_properties": {key: phone[key] for key in PHONE_PROPS if key in phone},
                   "phone_path": target_path, "repair_phone_pairing": repair_pairing,
                   "pairing_addresses": [], "pairing_confirmation_accepted": False,
                   "baseline_links": sorted(baseline), "owner": backend.bus.unique_name,
                   "original_links_interrupted": [],
                   "advertising_instances": objects[path][AD_MANAGER].get("ActiveInstances", 0),
                   "bus_id": backend.bus_id, "bluez_owner": backend.bluez_owner}
        state.save(journal)
        phase = "register"
        if getattr(args, "trace_advertising", False):
            if trace_factory is None:
                from coexist_trace import open as trace_factory
            # Monitor is receive-only. Emit a bounded sanitized summary rather
            # than a raw capture that could contain ACL/SMP keys or user data.
            trace = await trace_factory(int(args.adapter[3:]), lambda *_: None)
        if repair_pairing:
            from coexist_pairing import PairingAgent
            if confirm_pairing is None:
                from radio import _terminal_confirm_pairing

                async def confirm_pairing(number):
                    return await _terminal_confirm_pairing(number, 6, args.wait_seconds, emit)

            agent = PairingAgent(allow_pairing, confirm_pairing, emit)
            backend.bus.export(AGENT_PATH, agent)
            attempted_agent = True
            await backend.register_agent()
        if props["Pairable"] != repair_pairing:
            await backend.set_pairable(path, repair_pairing)
        app = app_factory(backend.bus, APP_PATH, emit, allow_device,
                          include_dis="0000180a-0000-1000-8000-00805f9b34fb" not in uuids,
                          include_battery="0000180f-0000-1000-8000-00805f9b34fb" not in uuids)
        app.export()
        attempted_gatt = True
        await backend.register(path, GATT_MANAGER, APP_PATH)
        ad = ad_factory(props["Alias"], emit)
        ad.export(backend.bus, AD_PATH)
        attempted_ad = True
        await backend.register(path, AD_MANAGER, AD_PATH)
        emit("advertising", {"adapter": args.adapter, "name": props["Alias"],
             "expected_name_location": "scan_response", "controller_trace_enabled": trace is not None,
             "shared_bluez_identity": True, "companion_links": links_json(companions),
             "new_pairing_allowed": repair_pairing, "wait_seconds": args.wait_seconds,
             "phone_side_pairing_confirmed": False,
             "message": (f"在 iPhone 蓝牙设置选择 {props['Alias']}，核对手机和终端的数字后确认；新配对保留。" if repair_pairing else
                         f"前提：手机和电脑均保留原配对；在 iPhone 选择 {props['Alias']}，手机已忽略配对则使用 --repair-phone-pairing。")})
        if trace is not None:
            emit("advertising_trace", trace.summary())
        phase = "wait_hid"
        deadline = asyncio.get_running_loop().time() + args.wait_seconds
        next_progress = 0
        ready_at = None
        ready_disconnect_count = None
        while True:
            objects = await backend.objects()
            remember_candidate()
            current = await link.connections()
            now = asyncio.get_running_loop().time()
            lost_links |= baseline - current
            for event in link.disconnect_events:
                candidate = (event["address"], event["address_type"])
                if candidate in baseline:
                    lost_links.add(candidate)
            for object_path, interface, change in backend.changes:
                if interface == DEVICE and (change.get("Connected") is False or change.get("removed")):
                    info = objects.get(object_path, {}).get(DEVICE, {})
                    peer = info.get("Address", object_path.rsplit("/dev_", 1)[-1].replace("_", ":")).upper()
                    lost_links |= {candidate for candidate in baseline if candidate[0] == peer}
            if lost_links:
                journal["original_links_interrupted"] = sorted(lost_links)
                state.save(journal)
                raise RuntimeError("原有蓝牙连接在共存测试期间中断")
            if unexpected_release:
                raise RuntimeError("BlueZ 提前释放测试广播")
            _, current_props = find_adapter(objects, name=args.adapter)
            if current_props.get("Powered") is not True or current_props.get("Pairable") != repair_pairing:
                raise RuntimeError("适配器电源或配对许可被改变，终止测试")
            target_path, phone_now = find_phone(objects, path, phone_address, preferred=candidate_path)
            watched = {"Trusted", "Blocked"} if repair_pairing else set(PHONE_PROPS)
            if any(phone_now.get(key) != value for key, value in journal["phone_properties"].items() if key in watched):
                raise RuntimeError("目标手机配对/信任状态发生变化，终止测试")
            if repair_pairing:
                candidate = objects.get(candidate_path, {}).get(DEVICE, {})
                identity_verified = bool(agent.accepted and candidate.get("Address", "").upper() == phone_address
                                         and candidate.get("Paired") and candidate.get("Bonded", candidate.get("Paired")))
            session_peers = {phone_address} | (pairing_addresses if identity_verified else set())
            phone_le = {(peer, kind) for peer, kind in current if peer in session_peers and kind in (1, 2)}
            phone_paths = {object_path for object_path, interfaces in objects.items()
                           if interfaces.get(DEVICE, {}).get("Adapter") == path and
                           (interfaces[DEVICE].get("Address", "").upper() == phone_address or object_path == candidate_path)}
            audio = {object_path for object_path, interfaces in objects.items()
                     if interfaces.get(MEDIA_TRANSPORT, {}).get("Device") in phone_paths}
            new_phone_audio |= audio - baseline_audio
            if new_phone_audio:
                raise RuntimeError("目标手机出现新增音频传输；HID 共存不等于音频隔离")
            if ready_at is None and identity_verified and hid_access.is_set() and phone_le and access_disconnect_count == phone_disconnect_count():
                ready_at = now
                ready_disconnect_count = phone_disconnect_count()
                deadline = now + args.hold_seconds
                phase = "hold"
                result.update(encrypted_phone_hid_access=True,
                              companion_count=len(companions))
                emit("service_ready", {"encrypted_phone_hid_access": True,
                     "report_subscription_scope": "global", "hold_seconds": args.hold_seconds,
                     "message": "现在同时使用原耳机/鼠标，检查是否卡顿或断开，并检查手机音频输出。"})
            if ready_at is not None and (not phone_le or phone_disconnect_count() != ready_disconnect_count):
                raise RuntimeError("目标 LE 连接在保持期间中断")
            if now >= deadline:
                if ready_at is None:
                    if repair_pairing and agent.accepted and not identity_verified:
                        raise ObservationIncomplete("已确认配对数字，但尚未确认候选与配置的手机身份一致且已保存配对；不判共存通过")
                    if repair_pairing and not agent.accepted and phone_le:
                        raise ObservationIncomplete("目标 LE 链路存在，但本轮没有完成数字核对；不能把电脑旧 Paired 状态当作重新配对成功")
                    if phone_le:
                        raise ObservationIncomplete("目标 LE 链路存在，但未观察到加密 HID 读取；手机可能使用缓存，当前证据不足以判断服务可用")
                    raise TimeoutError("等待期内未观察到目标手机 LE 连接；现有观测不能区分广播未被发现、手机筛选、配对记录不一致等原因")
                result.update(passed=True, hold=True, original_links_preserved=True,
                              hold_seconds=args.hold_seconds, new_phone_audio_transports=0)
                emit("hold_complete", {"seconds": args.hold_seconds})
                status = 0
                break
            if now >= next_progress:
                emit("waiting" if ready_at is None else "holding", {
                    "remaining_seconds": round(max(0, deadline - now), 1),
                    "phone_le_connected": bool(phone_le), "encrypted_hid_read_seen": hid_access.is_set(),
                    "report_subscribed_global": bool(app.report.notifying),
                    "pairing_confirmation_accepted": bool(agent and agent.accepted),
                    "original_links_present": True})
                next_progress = now + PROGRESS_SECONDS
            await asyncio.sleep(POLL_SECONDS)
    except asyncio.CancelledError:
        result["error"] = {"phase": phase, "type": "CancelledError", "reason": "用户中止测试"}
        status = 130
    except Exception as error:
        result["error"] = {"phase": phase, "type": type(error).__name__, "reason": str(error)[:350]}
        if isinstance(error, ObservationIncomplete):
            status = 3
            result["verdict"] = "inconclusive"
        emit("probe_error", result["error"])
    finally:
        phase = "cleanup"
        failures = []
        for attempted, interface, object_path in ((attempted_ad, AD_MANAGER, AD_PATH),
                                                  (attempted_gatt, GATT_MANAGER, APP_PATH)):
            if attempted:
                try:
                    await backend.unregister(path, interface, object_path)
                except BaseException as error:
                    failures.append(f"{interface}: {type(error).__name__}")
        for exported in (ad, app):
            if exported is not None:
                try:
                    exported.unexport()
                except Exception as error:
                    failures.append(f"unexport: {type(error).__name__}")
        if agent is not None:
            agent.cancel_pending("cleanup")
            # Close the temporary pairing window before releasing our agent.
            try:
                await backend.set_pairable(path, journal["properties"]["Pairable"])
            except BaseException as error:
                failures.append(f"pairable: {type(error).__name__}")
            if attempted_agent:
                try:
                    await backend.unregister_agent()
                except BaseException as error:
                    failures.append(f"unregister_agent: {type(error).__name__}")
            try:
                backend.bus.unexport(AGENT_PATH, agent)
            except Exception as error:
                failures.append(f"unexport_agent: {type(error).__name__}")
            result.update(pairing_confirmation_accepted=agent.accepted,
                          pairing_identity_verified=identity_verified, pairing_records="retained")
        if link is not None and journal is not None:
            for event in link.disconnect_events:
                candidate = (event["address"], event["address_type"])
                if candidate in baseline:
                    lost_links.add(candidate)
            if lost_links:
                journal["original_links_interrupted"] = sorted(lost_links)
                try:
                    state.save(journal)
                except Exception as error:
                    failures.append(f"record_interruption: {type(error).__name__}")
                result["passed"] = False
                if status == 0:
                    status = 1
        if trace is not None:
            result["advertising_trace"] = trace.summary()
        for resource in (trace, link, backend):
            if resource is not None:
                try:
                    await asyncio.wait_for(resource.close(), 5)
                except BaseException as error:
                    failures.append(f"close: {type(error).__name__}")
        phase = "restore"
        try:
            restoration = await asyncio.shield(restore(state, backend_factory=backend_factory, link_factory=link_factory))
            emit("restore", restoration)
            if not restoration.get("connections_restored", restoration.get("original_connections_present", True)):
                result["passed"] = False
                status = 1
        except Exception as error:
            result["passed"] = False
            result["restore_error"] = str(error)[:350]
            status = 2
            emit("restore_failed", {"reason": str(error)[:350], "message": "保留恢复记录，请运行 bluez_hid_lab.py restore"})
        if failures:
            result["unregister_errors"] = failures
        result["original_links_lost"] = links_json(lost_links)
        result["new_phone_audio_transports"] = len(new_phone_audio)
        result["global_report_subscription_observed"] = report_subscription_seen
        emit("result", result)
    return status


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--state-dir", type=Path, default=HERE / ".coexist")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("plan", help="只显示方案，不访问蓝牙")
    run_parser = commands.add_parser("run", help="由用户运行真实的 BlueZ HID 共存短测")
    run_parser.add_argument("--adapter", required=True)
    phone = run_parser.add_mutually_exclusive_group()
    phone.add_argument("--phone", help="目标手机原 BlueZ 配对地址")
    phone.add_argument("--phone-file", type=Path, default=os.environ.get("BLUETOOTH_AUTH_ADDRESS_FILE") or None,
                       help="手机地址文件；默认取 BLUETOOTH_AUTH_ADDRESS_FILE，不内置个人路径")
    run_parser.add_argument("--wait-seconds", type=float, default=45)
    run_parser.add_argument("--hold-seconds", type=float, default=45)
    run_parser.add_argument("--repair-phone-pairing", action="store_true",
                            help="手机已忽略旧配对时使用：临时允许数字核对配对；新配对保留，其他临时设置退出恢复")
    run_parser.add_argument("--trace-advertising", action="store_true",
                            help="被动核对本机控制器收到的广播命令/状态；不扫描，不记录原始捕获或密钥")
    commands.add_parser("restore", help="恢复遗留的共存测试设置及新增目标 LE 链路")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    state = State(args.state_dir)
    try:
        if args.command in (None, "plan"):
            show("plan", {"adapter": "明确指定现有 hciN，不关闭或独占控制器", "name": "沿用原 BlueZ Alias",
                 "pairing": "默认要求两端保留原配对；手机已忽略时用 --repair-phone-pairing，核对数字后保留新配对",
                 "services": "增加临时 HID/GATT 和广告；不改系统服务或禁用原音频配置文件",
                 "validation": "需要一台原已连接的其他蓝牙设备；核对目标 LE 加密 HID 读取、订阅和原连接保持",
                 "cleanup": "注销服务、广告和临时配对代理；恢复 Pairable，断开新增目标 LE 链路；重新配对的密钥保留",
                 "limits": "音频实际输出、鼠标/耳机使用质量需人工观察；不保证原身份上没有音频服务"})
            return 0
        if args.command == "restore" and not state.pending():
            show("restore", {"settings_restored": True, "changed": False, "message": "无待恢复记录；没有访问蓝牙"})
            return 0
        if os.geteuid() != 0:
            raise PermissionError("run/有遗留记录的 restore 需要 sudo；脚本不会自行提权")
        if args.command == "run":
            if not re.fullmatch(r"hci\d+", args.adapter):
                raise ValueError("--adapter 必须是明确的 hciN")
            if not all(0 < value <= 180 for value in (args.wait_seconds, args.hold_seconds)):
                raise ValueError("等待/保持时间须在 0 到 180 秒之间，本脚本用于短测")
        with state.lock():
            if args.command == "run":
                return asyncio.run(run(args, state))
            result = asyncio.run(restore(state))
            state.emit("restore", result)
            return 0 if result.get("connections_restored", result.get("original_connections_present", True)) else 1
    except (Exception, KeyboardInterrupt) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
