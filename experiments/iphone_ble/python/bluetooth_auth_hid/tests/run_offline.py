"""运行本包测试；拦截真实蓝牙 socket 及所有 socket 连接。"""

from pathlib import Path
import socket
import sys
import unittest


class OfflineSocket(socket.socket):
    def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
        if family == 31:
            raise RuntimeError("离线测试禁止创建真实蓝牙 socket")
        super().__init__(family, type, proto, fileno)

    def connect(self, *args, **kwargs):
        raise RuntimeError("离线测试禁止连接 socket")

    connect_ex = connect
    bind = connect


def main():
    sys.dont_write_bytecode = True
    socket.socket = OfflineSocket
    socket.SocketType = OfflineSocket
    tests = Path(__file__).resolve().parent
    sys.path.insert(0, str(tests.parents[1]))
    suite = unittest.defaultTestLoader.discover(str(tests), pattern="test_*.py")
    return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
