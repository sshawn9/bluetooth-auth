#!/usr/bin/env python3
"""Explicitly operated iPhone BLE experiment. No argument defaults to an offline plan."""

from __future__ import annotations

import sys
sys.dont_write_bytecode = True

import argparse
import asyncio
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess

from state import KIND, MODES, LabState, atomic_json, check_path, read_json

HERE = Path(__file__).absolute().parent
SOURCE_FILES = {
    "ble_lab.py", "state.py", "adapter.py", "radio.py", "README.md", "requirements.txt", ".gitignore",
    "tests/test_state.py", "tests/test_adapter.py", "tests/test_cli.py", "tests/test_radio.py",
    "bluez_hid_lab.py", "coexist_gatt.py", "coexist_link.py",
    "tests/test_coexist.py", "tests/test_coexist_gatt.py", "tests/test_coexist_link.py",
    "coexist_trace.py", "tests/test_coexist_trace.py",
    "coexist_pairing.py", "tests/test_coexist_pairing.py",
}


def show(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def setup_environment() -> None:
    marker = HERE / ".environment.json"
    environment = check_path(HERE / ".venv")
    if environment.exists() and not marker.exists():
        raise RuntimeError("实验目录已有未登记的 .venv；不会覆盖它")
    if marker.exists() and read_json(marker) != {"kind": KIND, "environment": str(environment)}:
        raise RuntimeError("环境所有权记录不匹配，拒绝覆盖")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("需要 uv 创建独立环境；不会改项目的 .venv 或系统 Python")
    atomic_json(marker, {"kind": KIND, "environment": str(environment)})
    env = dict(os.environ, UV_PYTHON_DOWNLOADS="never", PYTHONDONTWRITEBYTECODE="1")
    # No project sync/lock or persistent uv cache. All installed packages stay here.
    if not environment.exists():
        subprocess.run([uv, "--no-cache", "venv", "--python", sys.executable, str(environment)], check=True, env=env)
    subprocess.run([uv, "--no-cache", "pip", "install", "--python", str(environment / "bin/python"),
                    "-r", str(HERE / "requirements.txt")], check=True, env=env)
    print(f"独立环境已准备：{environment / 'bin/python'}；没有访问蓝牙")


def require_dependencies() -> None:
    for package, expected in (("bumble", "0.0.234"), ("dbus-fast", "5.0.17")):
        try:
            found = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(f"缺少 {package}；先执行 setup，再用实验目录 .venv/bin/python 运行") from error
        if found != expected:
            raise RuntimeError(f"{package} 必须为 {expected}（当前 {found}）；请使用实验独立环境")


def require_root() -> None:
    if os.geteuid() != 0:
        raise PermissionError("run/restore 需要管理员权限；请自行用 sudo 运行，脚本不会自动提权")


async def run_experiment(args, state: LabState) -> int:
    require_root()
    require_dependencies()
    if any((HERE / ".coexist" / name).exists() for name in ("restore.json", "restore.json.tmp")):
        raise RuntimeError("共存测试有运行中/待恢复记录；先运行 bluez_hid_lab.py restore")
    # Importing these modules opens neither D-Bus nor a Bluetooth socket.
    import adapter
    from radio import ProbeOptions, run_probe

    with state.lock():
        if state.journal.exists() or state.journal.with_name(state.journal.name + ".tmp").exists():
            raise RuntimeError("有待恢复记录；先运行 restore，不会开始新实验")
        config = state.config(args.mode)
        bonded = state.has_bond(args.mode)
        if bonded and args.enroll:
            raise RuntimeError("该模式已有配对；请去掉 --enroll，避免覆盖现有测试配对")
        if not bonded and not args.enroll:
            raise RuntimeError("该模式尚无配对；首次请用 --enroll。没有让出适配器")
        options = ProbeOptions(
            initial_timeout=args.initial_timeout, reconnect_timeout=args.reconnect_timeout,
            hold_seconds=args.hold_seconds, cycles=args.cycles, enroll=args.enroll,
        )

        def emit(event, payload):
            record = {"mode": args.mode, **payload}
            state.append_event(event, record)
            show({"event": event, **record})

        status = 1
        old_umask = os.umask(0o077)
        try:
            lease = await adapter.acquire(args.adapter, state.journal)
            identities = state.public_manifest()
            if any(identity["address"] == lease["address"] for identity in identities.values()):
                raise RuntimeError("测试地址与物理地址碰巧相同，拒绝运行；请 clean 后重新 prepare")
            emit("adapter_handoff", {"adapter": args.adapter, "previous_connections": lease["previously_connected"]})
            result = await run_probe(config, args.mode, lease["index"], options, emit)
            emit("result", result)
            status = 0 if result.get("passed", False) else 1
        except asyncio.CancelledError:
            emit("interrupted", {"message": "已中止实验，正在恢复适配器"})
            status = 130
        except Exception as error:
            emit("error", {"message": str(error)})
        finally:
            # run_probe must release HCI before reaching this point. The durable
            # journal remains if a second interrupt, crash or restore error occurs.
            try:
                restoration = await asyncio.shield(adapter.restore(state.journal, emit=emit))
                emit("restore", restoration)
            except Exception as error:
                status = 2
                emit("error", {"message": f"恢复未完成：{error}。保留记录，请执行 restore。"})
            finally:
                os.umask(old_umask)
        return status


async def restore_experiment(state: LabState) -> int:
    # No journal -> no privilege/dependency requirement and no D-Bus access.
    with state.lock():
        if state.journal.exists() or state.journal.with_name(state.journal.name + ".tmp").exists():
            require_root()
        import adapter

        def emit(event, payload):
            state.append_event(event, payload)
            show({"event": event, **payload})

        result = await adapter.restore(state.journal, emit=emit)
        state.append_event("restore", result)
        show(result)
    return 0


def remove_environment() -> None:
    environment = check_path(HERE / ".venv")
    marker = check_path(HERE / ".environment.json")
    if not marker.exists():
        if environment.exists():
            raise RuntimeError("未登记的 .venv 不会被删除")
        return
    if read_json(marker) != {"kind": KIND, "environment": str(environment)}:
        raise RuntimeError("环境所有权不匹配，拒绝删除")
    if environment.exists():
        shutil.rmtree(environment)  # Owned, reproducible environment; symlinks aren't followed.
    marker.unlink()


def uninstall(state: LabState) -> None:
    """User-explicit final deletion. Refuse unknown files instead of broad rm -rf."""
    for path in HERE.rglob("*"):
        relative = path.relative_to(HERE)
        if relative.parts[0] in {".venv", ".runtime"}:
            continue
        if "__pycache__" in relative.parts and (path.is_dir() or path.suffix == ".pyc"):
            continue
        if relative.as_posix() in SOURCE_FILES | {".environment.json", ".environment.json.tmp", "tests"}:
            if path.is_symlink():
                raise RuntimeError(f"发现符号链接，拒绝卸载：{relative}")
            continue
        raise RuntimeError(f"发现额外文件，拒绝删除工具目录：{relative}")
    # Do not leave an unselected default state tree behind when a custom path was used.
    default_state = LabState(HERE / ".runtime")
    if state.root != default_state.root and default_state.exists():
        raise RuntimeError("默认 .runtime 也存在；先分别完成恢复和清理，再卸载工具")
    environment, marker = check_path(HERE / ".venv"), check_path(HERE / ".environment.json")
    if environment.exists() and not marker.exists():
        raise RuntimeError("未登记的 .venv 不会被删除")
    if marker.exists() and read_json(marker) != {"kind": KIND, "environment": str(environment)}:
        raise RuntimeError("环境所有权不匹配，拒绝删除")
    if state.exists():
        state.purge()
    remove_environment()
    temporary = HERE / ".environment.json.tmp"
    if temporary.exists():
        temporary.unlink()
    for relative in SOURCE_FILES:
        path = HERE / relative
        if path.exists():
            path.unlink()
    for path in sorted(HERE.rglob("__pycache__"), key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink():
            raise RuntimeError("拒绝删除符号链接缓存目录")
        shutil.rmtree(path)
    for path in (HERE / "tests", HERE):
        if path.exists():
            path.rmdir()
    try:
        HERE.parent.rmdir()  # Only succeeds if the experiment's parent is empty.
    except OSError:
        pass
    print("实验源文件、已登记环境和状态目录已移除。手机测试配对/缓存和系统日志不由本工具清除。")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--state-dir", type=Path, default=HERE / ".runtime", help="实验私有状态目录；建立后不可移动")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("plan", help="只显示方案；不写文件、不读取蓝牙")
    commands.add_parser("setup", help="只下载依赖到实验 .venv，不访问蓝牙")
    commands.add_parser("prepare", help="创建固定身份/私有状态，不访问蓝牙")
    run = commands.add_parser("run", help="真实实验：临时独占适配器，仅由用户运行")
    run.add_argument("mode", choices=MODES)
    run.add_argument("--adapter", required=True, help="明确指定 hciN，不自动选择")
    run.add_argument("--enroll", action="store_true", help="首次配对时使用；允许交互确认一个手机")
    run.add_argument("--initial-timeout", type=float, default=120)
    run.add_argument("--reconnect-timeout", type=float, default=30)
    run.add_argument("--hold-seconds", type=float, default=30)
    run.add_argument("--cycles", type=int, default=3, help="电脑端断开再恢复的次数")
    commands.add_parser("report", help="只读取实验结果，不访问蓝牙")
    commands.add_parser("restore", help="只按遗留记录恢复适配器，不扫描/配对/连接手机")
    commands.add_parser("clean", help="删除已登记实验状态；有待恢复记录时拒绝")
    commands.add_parser("uninstall", help="最终清理：删除本工具源文件、已登记环境及状态")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    state = LabState(args.state_dir)
    try:
        if args.command in (None, "plan"):
            show({
                "mode": "offline_plan", "candidates": MODES, "state_directory": str(state.root),
                "radio": "只有 run 会开始实验；restore 只恢复已保存设置",
                "impact": "运行期间该适配器其他连接中断；不改原 Alias/配对库/认证配置",
                "reconnect": "电脑恢复广播，由 iPhone 发起连接；必须实测手机不再操作是否可用",
                "cleanup": "正常/异常退出尝试恢复；有遗留记录需 restore 后才允许 clean/uninstall",
                "manual_cleanup": "iPhone 忽略 BT-Auth-ANCS/HID/CTS；无法保证删除手机缓存或系统日志",
            })
        elif args.command == "setup":
            setup_environment()
        elif args.command == "prepare":
            show(state.prepare())
        elif args.command == "run":
            if args.cycles < 0 or any(not 0 < value <= 86400 for value in (args.initial_timeout, args.reconnect_timeout, args.hold_seconds)):
                raise ValueError("cycles 必须非负，超时/保持时间须在 0 到 86400 秒之间")
            return asyncio.run(run_experiment(args, state))
        elif args.command == "report":
            show({"identities": state.public_manifest(), "results": state.results(), "pending_restore": state.journal.exists()})
        elif args.command == "restore":
            if not state.exists():
                print("无实验状态；没有访问蓝牙")
            else:
                return asyncio.run(restore_experiment(state))
        elif args.command == "clean":
            if state.exists():
                names = state.purge()
                show({"managed_state_removed": True, "phone_records_to_remove_manually": names,
                      "remaining": ["本工具源文件和独立 .venv（用 uninstall 删除）", "手机缓存/系统日志无法由本工具保证清除"]})
            else:
                print("无实验状态；没有访问蓝牙")
        elif args.command == "uninstall":
            uninstall(state)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2


if __name__ == "__main__":
    raise SystemExit(main())
