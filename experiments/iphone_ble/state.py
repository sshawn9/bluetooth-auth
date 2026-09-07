"""Private, explicitly owned experiment files. No Bluetooth imports or operations."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

KIND = "bluetooth-auth/iphone-ble-lab/v1"
MODES = {"ancs": "BT-Auth-ANCS", "hid": "BT-Auth-HID", "cts": "BT-Auth-CTS"}


def check_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError(f"拒绝符号链接路径：{part}")
    return path


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, value: dict) -> None:
    """Write ahead of mutations, with a known recoverable temporary filename."""
    path = check_path(path)
    temporary = check_path(path.with_name(path.name + ".tmp"))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = -1
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if fd != -1:
            os.close(fd)


def read_json(path: Path) -> dict:
    path = check_path(path)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(f"不是普通文件：{path}")
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"状态文件格式错误：{path}")
    return value


class LabState:
    def __init__(self, root: Path):
        self.root = check_path(root)
        self.manifest_path = self.root / "manifest.json"
        self.journal = self.root / "adapter-restore.json"

    def exists(self) -> bool:
        return self.root.exists()

    def validate(self) -> dict:
        manifest = read_json(self.manifest_path)
        if manifest.get("kind") != KIND or manifest.get("root") != str(self.root):
            raise ValueError("不是本实验创建的状态目录，或目录被移动；拒绝修改/删除")
        if stat.S_IMODE(self.root.stat().st_mode) & 0o077:
            raise ValueError("状态目录必须为 0700；里面可能包含配对密钥")
        if set(manifest.get("modes", {})) != set(MODES):
            raise ValueError("身份清单不完整，拒绝生成替代身份")
        addresses, irks = set(), set()
        for mode, identity in manifest["modes"].items():
            address, irk = identity.get("address", ""), identity.get("irk", "")
            if identity.get("name") != MODES[mode] or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                raise ValueError("实验身份名称/地址不合法")
            numeric = int(address.replace(":", ""), 16)
            if numeric >> 46 != 3 or numeric & ((1 << 46) - 1) in (0, (1 << 46) - 1):
                raise ValueError("实验身份必须使用有效的静态随机地址")
            if not re.fullmatch(r"[0-9a-f]{32}", irk) or address in addresses or irk in irks:
                raise ValueError("实验身份/IRK 必须独立且固定")
            addresses.add(address)
            irks.add(irk)
        return manifest

    def prepare(self) -> dict:
        if self.root.exists():
            self.validate()
            with self.lock():
                self._finish_prepare(self.validate())
            return self.public_manifest()
        self.root.mkdir(mode=0o700, parents=False)
        manifest = {"kind": KIND, "root": str(self.root), "created_at": time.time(), "modes": {}}
        addresses = set()
        for mode, name in MODES.items():
            while True:
                raw = bytearray(secrets.token_bytes(6))
                raw[0] = (raw[0] & 0x3F) | 0xC0
                address = ":".join(f"{byte:02X}" for byte in raw)
                if address not in addresses and (int.from_bytes(raw) & ((1 << 46) - 1)) not in (0, (1 << 46) - 1):
                    break
            addresses.add(address)
            manifest["modes"][mode] = {"name": name, "address": address, "irk": secrets.token_hex(16)}
        # The manifest fixes identities before any profile file or radio use.
        atomic_json(self.manifest_path, manifest)
        with self.lock():
            self._finish_prepare(manifest)
        return self.public_manifest()

    def _finish_prepare(self, manifest: dict) -> None:
        for mode, identity in manifest["modes"].items():
            directory = check_path(self.root / mode)
            directory.mkdir(mode=0o700, exist_ok=True)
            config_path = directory / "device.json"
            expected = {
                "name": identity["name"],
                "address": identity["address"],
                "irk": identity["irk"],
                "identity_address_type": 1,
                "le_enabled": True,
                "le_privacy_enabled": False,
                "classic_enabled": False,
                "classic_smp_enabled": False,
                "keystore": f"JsonKeyStore:{directory / 'keys.json'}",
            }
            if config_path.exists():
                if read_json(config_path) != expected:
                    raise ValueError(f"配置被修改：{config_path}；不会覆盖或重新生成身份")
            else:
                atomic_json(config_path, expected)

    def config(self, mode: str) -> Path:
        manifest = self.validate()
        self._finish_prepare(manifest)
        path = self.root / mode / "device.json"
        keys = check_path(self.root / mode / "keys.json")
        check_path(keys.with_name("keys.json.tmp"))
        return path

    def public_manifest(self) -> dict:
        manifest = self.validate()
        return {mode: {key: identity[key] for key in ("name", "address")} for mode, identity in manifest["modes"].items()}

    def has_bond(self, mode: str) -> bool:
        self.validate()
        path = check_path(self.root / mode / "keys.json")
        if not path.exists():
            return False
        data = read_json(path)
        return any(isinstance(peers, dict) and any(
            isinstance(keys, dict) and any(isinstance(keys.get(name), dict) and keys[name].get("value")
                                          for name in ("ltk", "ltk_central", "ltk_peripheral"))
            for keys in peers.values()) for peers in data.values())

    @contextlib.contextmanager
    def lock(self):
        self.validate()
        fd = os.open(check_path(self.root / "run.lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("实验仍在运行；拒绝并行运行、恢复或清理") from error
            yield
        finally:
            os.close(fd)

    def append_event(self, event: str, payload: dict) -> None:
        # Callers supply only status/metrics; never serialize Device/config/keys.
        path = check_path(self.root / "events.jsonl")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.time(), "event": event, **payload}, ensure_ascii=False) + "\n")
            stream.flush()

    def results(self) -> list[dict]:
        self.validate()
        path = check_path(self.root / "events.jsonl")
        if not path.exists():
            return []
        results = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                results.append({"event": "incomplete_log_line", "line": number})
                continue
            if item.get("event") in {"result", "restore", "error", "interrupted"}:
                results.append(item)
        return results

    def purge(self) -> list[str]:
        """Delete only the known state tree. A pending recovery is never discarded."""
        self.validate()
        with self.lock():
            if self.journal.exists() or self.journal.with_name(self.journal.name + ".tmp").exists():
                raise RuntimeError("存在未完成的适配器恢复记录；先运行 restore，禁止删除恢复依据")
            allowed = {Path(name) for name in ("manifest.json", "manifest.json.tmp", "run.lock", "events.jsonl")}
            for mode in MODES:
                allowed.add(Path(mode))
                allowed.update(Path(mode) / name for name in ("device.json", "device.json.tmp", "keys.json", "keys.json.tmp"))
            entries = list(self.root.rglob("*"))
            for path in entries:
                relative = path.relative_to(self.root)
                if path.is_symlink() or relative not in allowed:
                    raise RuntimeError(f"发现非本实验文件，拒绝清理：{relative}")
                if not (path.is_file() or (relative in map(Path, MODES) and path.is_dir())):
                    raise RuntimeError(f"不支持的文件类型：{relative}")
            names = list(MODES.values())
            for path in sorted(entries, key=lambda item: len(item.parts), reverse=True):
                if path.is_dir():
                    path.rmdir()
                elif path.name != "run.lock":
                    path.unlink()
            (self.root / "run.lock").unlink()
            self.root.rmdir()
            return names
