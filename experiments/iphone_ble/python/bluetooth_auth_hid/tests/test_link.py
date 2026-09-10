from __future__ import annotations

import unittest
from unittest.mock import patch

from bluetooth_auth_hid import link


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def fileno(self) -> int:
        return 37

    def close(self) -> None:
        self.closed = True


def write_connection(
    buffer: bytearray,
    *,
    count: int,
    adapter_index: int = 0,
    handle: int = 0x0042,
    address: bytes = bytes.fromhex("AABBCCDDEEFF"),
    link_type: int = link.HCI_LE_LINK,
    state: int = link.BT_CONNECTED,
    link_mode: int = link.HCI_LM_ENCRYPT,
) -> None:
    link._REQUEST.pack_into(buffer, 0, adapter_index, count)
    link._CONNECTION.pack_into(
        buffer,
        link._REQUEST.size,
        handle,
        address[::-1],
        link_type,
        0,
        state,
        link_mode,
    )


class LinkReaderTests(unittest.TestCase):
    def test_reads_only_returned_le_encrypted_entry_with_uapi_layout(self) -> None:
        fake_socket = FakeSocket()

        def ioctl(fd: int, command: int, buffer: bytearray, mutate: bool) -> int:
            self.assertEqual(fd, 37)
            self.assertEqual(command, 0x800448D4)
            self.assertTrue(mutate)
            self.assertEqual(len(buffer), 4 + 512 * 16)
            self.assertEqual(link._REQUEST.unpack_from(buffer), (2, 512))
            write_connection(buffer, count=1, adapter_index=2)
            # This extra record must not be returned because kernel count is one.
            link._CONNECTION.pack_into(
                buffer, 20, 0x0043, b"\x01" * 6, 1, 0, 2, 0
            )
            return 0

        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=ioctl),
        ):
            entries = link.LinkReader().read(2)

        self.assertTrue(fake_socket.closed)
        self.assertEqual(entries, [link.LinkInfo("AA:BB:CC:DD:EE:FF", 0x42, 0x80, 1, True)])
        self.assertNotIn("AA:BB:CC:DD:EE:FF", repr(entries[0]))

    def test_non_encrypted_or_non_le_fields_are_preserved(self) -> None:
        fake_socket = FakeSocket()

        def ioctl(_fd: int, _command: int, buffer: bytearray, _mutate: bool) -> int:
            write_connection(
                buffer,
                count=1,
                link_type=1,
                state=2,
                link_mode=0,
            )
            return 0

        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=ioctl),
        ):
            entry = link.LinkReader().read(0)[0]

        self.assertEqual((entry.link_type, entry.state, entry.encrypted), (1, 2, False))

    def test_rejects_count_larger_than_allocated_buffer_and_closes_socket(self) -> None:
        fake_socket = FakeSocket()

        def ioctl(_fd: int, _command: int, buffer: bytearray, _mutate: bool) -> int:
            link._REQUEST.pack_into(buffer, 0, 0, 513)
            return 0

        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=ioctl),
        ):
            with self.assertRaisesRegex(link.LinkError, "^invalid link snapshot$"):
                link.LinkReader().read(0)

        self.assertTrue(fake_socket.closed)

    def test_rejects_capacity_saturation_and_closes_socket(self) -> None:
        fake_socket = FakeSocket()

        def ioctl(_fd: int, _command: int, buffer: bytearray, _mutate: bool) -> int:
            link._REQUEST.pack_into(buffer, 0, 0, 512)
            return 0

        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=ioctl),
        ):
            with self.assertRaisesRegex(link.LinkError, "^invalid link snapshot$"):
                link.LinkReader().read(0)

        self.assertTrue(fake_socket.closed)

    def test_rejects_mismatched_returned_adapter_and_closes_socket(self) -> None:
        fake_socket = FakeSocket()

        def ioctl(_fd: int, _command: int, buffer: bytearray, _mutate: bool) -> int:
            link._REQUEST.pack_into(buffer, 0, 1, 0)
            return 0

        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=ioctl),
        ):
            with self.assertRaisesRegex(link.LinkError, "^invalid link snapshot$"):
                link.LinkReader().read(0)

        self.assertTrue(fake_socket.closed)

    def test_rejects_truncated_ioctl_buffer_and_closes_socket(self) -> None:
        fake_socket = FakeSocket()

        def ioctl(_fd: int, _command: int, buffer: bytearray, _mutate: bool) -> int:
            del buffer[link._REQUEST.size:]
            link._REQUEST.pack_into(buffer, 0, 0, 1)
            return 0

        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=ioctl),
        ):
            with self.assertRaisesRegex(link.LinkError, "^invalid link snapshot$"):
                link.LinkReader().read(0)

        self.assertTrue(fake_socket.closed)

    def test_ioctl_failure_is_redacted_and_closes_socket(self) -> None:
        fake_socket = FakeSocket()
        with (
            patch.object(link.socket, "socket", return_value=fake_socket),
            patch.object(link.fcntl, "ioctl", side_effect=OSError("AA:BB:CC:DD:EE:FF")),
        ):
            with self.assertRaisesRegex(link.LinkError, "^link snapshot unavailable$"):
                link.LinkReader().read(0)

        self.assertTrue(fake_socket.closed)

    def test_rejects_invalid_adapter_without_opening_socket(self) -> None:
        with patch.object(link.socket, "socket") as socket_factory:
            with self.assertRaisesRegex(link.LinkError, "^invalid adapter index$"):
                link.LinkReader().read(-1)
        socket_factory.assert_not_called()

    def test_rejects_reserved_adapter_without_opening_socket(self) -> None:
        with patch.object(link.socket, "socket") as socket_factory:
            with self.assertRaisesRegex(link.LinkError, "^invalid adapter index$"):
                link.LinkReader().read(0xFFFF)
        socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
