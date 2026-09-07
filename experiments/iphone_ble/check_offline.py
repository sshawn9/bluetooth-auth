#!/usr/bin/env python3
"""Run the iPhone BLE experiment's complete unittest suite in offline mode."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent


def _guard_source() -> str:
    """Startup hook installed in a temporary directory for the test child."""
    return r'''
import socket as _socket

_real_socket = _socket.socket
_bluetooth_family = getattr(_socket, "AF_BLUETOOTH", 31)

class OfflineNetworkBlocked(RuntimeError):
    pass

class _OfflineSocket(_real_socket):
    def __init__(self, family=_socket.AF_INET, type=_socket.SOCK_STREAM, proto=0, fileno=None):
        if family == _bluetooth_family:
            raise OfflineNetworkBlocked("offline tests forbid AF_BLUETOOTH sockets")
        super().__init__(family, type, proto, fileno)

    def connect(self, *args, **kwargs):
        raise OfflineNetworkBlocked("offline tests forbid socket.connect")

    def connect_ex(self, *args, **kwargs):
        raise OfflineNetworkBlocked("offline tests forbid socket.connect_ex")

def _blocked_create_connection(*args, **kwargs):
    raise OfflineNetworkBlocked("offline tests forbid socket.create_connection")

_socket.socket = _OfflineSocket
_socket.SocketType = _OfflineSocket
_socket.create_connection = _blocked_create_connection
'''


def run_tests() -> int:
    python = HERE / ".venv" / "bin" / "python"
    tests = HERE / "tests"
    if not python.is_file():
        print(f"离线校验需要已有实验环境：{python}", file=sys.stderr)
        return 2
    if not tests.is_dir():
        print(f"找不到测试目录：{tests}", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="iphone-ble-offline-") as temporary:
        root = Path(temporary)
        guard = root / "guard"
        guard.mkdir(mode=0o700)
        (guard / "sitecustomize.py").write_text(_guard_source(), encoding="utf-8")
        environment = {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(guard), str(HERE.parents[1]))),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
        }
        # Do not inherit a user PYTHONPATH or XDG location.  Tests use only
        # their tempfile fixtures; source files are addressed absolutely.
        child_environment = dict(os.environ)
        child_environment.update(environment)
        completed = subprocess.run(
            [str(python), "-B", "-m", "unittest", "discover", "-s", str(tests), "-p", "test*.py"],
            cwd=root,
            env=child_environment,
        )
        return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    return run_tests()


if __name__ == "__main__":
    raise SystemExit(main())
