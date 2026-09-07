from __future__ import annotations

import subprocess
import sys
import tempfile
import os
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_offline


class OfflineCheckTests(unittest.TestCase):
    def test_help_does_not_run_tests_or_open_hardware(self):
        result = subprocess.run(
            [sys.executable, "-B", str(Path(check_offline.__file__)), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("offline", result.stdout.lower())

    def test_startup_guard_blocks_real_socket_operations_but_keeps_socketpair(self):
        program = '''
import socket

def blocked(operation):
    try:
        operation()
    except RuntimeError:
        return
    raise AssertionError("operation was not blocked")

blocked(lambda: socket.socket(31, socket.SOCK_RAW, 1))
left, right = socket.socketpair()
blocked(lambda: left.connect(("127.0.0.1", 9)))
blocked(lambda: left.connect_ex(("127.0.0.1", 9)))
blocked(lambda: left.connect("/tmp/no-network"))
blocked(lambda: left.connect_ex("/tmp/no-network"))
blocked(lambda: socket.create_connection(("127.0.0.1", 9)))
left.close()
right.close()
'''
        with tempfile.TemporaryDirectory() as temporary:
            guard = Path(temporary) / "guard"
            guard.mkdir()
            (guard / "sitecustomize.py").write_text(check_offline._guard_source(), encoding="utf-8")
            environment = dict(os.environ, PYTHONPATH=str(guard), PYTHONNOUSERSITE="1")
            result = subprocess.run([sys.executable, "-B", "-c", program], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_child_failure_exit_code_is_returned(self):
        completed = mock.Mock(returncode=19)
        with mock.patch.object(check_offline.subprocess, "run", return_value=completed):
            self.assertEqual(check_offline.run_tests(), 19)


if __name__ == "__main__":
    unittest.main()
