"""只读读取 Linux HCI 当前连接快照。"""

from __future__ import annotations

import fcntl
import socket
import struct
from dataclasses import dataclass, field

# Linux UAPI: include/net/bluetooth/hci_sock.h and hci.h.
AF_BLUETOOTH = 31
BTPROTO_HCI = 1
HCIGETCONNLIST = 0x800448D4  # _IOR('H', 212, int)
HCI_LE_LINK = 0x80
BT_CONNECTED = 1
HCI_LM_ENCRYPT = 0x0004

# Linux UAPI uses its native ABI. x86_64 and aarch64 are supported here.
_REQUEST = struct.Struct("=HH")
_CONNECTION = struct.Struct("=H6sBBHI")
_MAX_CONNECTIONS = 512


class LinkError(RuntimeError):
    """链路快照不可用或格式无效。"""


@dataclass(frozen=True)
class LinkInfo:
    """内核返回的一条 HCI 连接记录。"""

    address: str = field(repr=False)
    handle: int
    link_type: int
    state: int
    encrypted: bool


class LinkReader:
    """每次 ``read`` 打开并关闭一个只读 HCI 控制 socket。"""

    def read(self, adapter_index: int) -> list[LinkInfo]:
        """读取指定适配器的当前连接；不发送 HCI 或 MGMT 命令。"""
        if not isinstance(adapter_index, int) or isinstance(adapter_index, bool):
            raise LinkError("invalid adapter index")
        if not 0 <= adapter_index < 0xFFFF:
            raise LinkError("invalid adapter index")

        buffer = bytearray(_REQUEST.size + _MAX_CONNECTIONS * _CONNECTION.size)
        _REQUEST.pack_into(buffer, 0, adapter_index, _MAX_CONNECTIONS)
        sock: socket.socket | None = None
        try:
            sock = socket.socket(
                AF_BLUETOOTH,
                socket.SOCK_RAW | getattr(socket, "SOCK_CLOEXEC", 0),
                BTPROTO_HCI,
            )
            fcntl.ioctl(sock.fileno(), HCIGETCONNLIST, buffer, True)
            return self._parse(buffer, adapter_index)
        except LinkError:
            raise
        except (OSError, ValueError, struct.error):
            raise LinkError("link snapshot unavailable") from None
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _parse(buffer: bytearray, adapter_index: int) -> list[LinkInfo]:
        if len(buffer) < _REQUEST.size:
            raise LinkError("invalid link snapshot")
        returned_adapter, connection_count = _REQUEST.unpack_from(buffer)
        if returned_adapter != adapter_index:
            raise LinkError("invalid link snapshot")
        # A full request does not establish that the kernel had no further links.
        if connection_count >= _MAX_CONNECTIONS:
            raise LinkError("invalid link snapshot")
        required = _REQUEST.size + connection_count * _CONNECTION.size
        if required > len(buffer):
            raise LinkError("invalid link snapshot")

        links: list[LinkInfo] = []
        for offset in range(_REQUEST.size, required, _CONNECTION.size):
            handle, raw_address, link_type, _outgoing, state, link_mode = (
                _CONNECTION.unpack_from(buffer, offset)
            )
            address = ":".join(f"{octet:02X}" for octet in reversed(raw_address))
            if len(address) != 17:
                raise LinkError("invalid link snapshot")
            links.append(
                LinkInfo(
                    address=address,
                    handle=handle,
                    link_type=link_type,
                    state=state,
                    encrypted=bool(link_mode & HCI_LM_ENCRYPT),
                )
            )
        return links
