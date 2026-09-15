from __future__ import annotations

import importlib.util
import asyncio
import contextlib
import io
from pathlib import Path
import struct
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "diagnostics" / "le_bearer_probe.py"
SPEC = importlib.util.spec_from_file_location("le_bearer_probe", MODULE)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(probe)


def target() -> bytes:
    return bytes((0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC))[::-1]


def packet(kind: int, body: bytes) -> bytes:
    return (
        kind.to_bytes(2, "little")
        + (0).to_bytes(2, "little")
        + len(body).to_bytes(2, "little")
        + body
    )


def le_complete(raw, handle=7, role=0, status=0, enhanced=False):
    # Core HCI layout: subevent/status/handle/role/address type/address,
    # optional local+peer RPA, interval/latency/supervision timeout/SCA.
    payload = (
        bytes((0x0A if enhanced else 1, status))
        + struct.pack("<H", handle)
        + bytes((role, 0))
        + raw
    )
    if enhanced:
        payload += bytes(12)
    payload += bytes.fromhex("18000000f40100")
    return bytes((0x3E, len(payload))) + payload


class BearerProbeTests(unittest.TestCase):
    def test_hci_filters_other_peers_and_marks_pc_role_and_encryption(self):
        raw, handles = target(), set()
        le = le_complete(raw)
        self.assertEqual(
            probe.parse_hci(packet(3, le), 0, raw, handles),
            [
                {
                    "event": "le_connection_complete",
                    "handle": 7,
                    "status": 0,
                    "pc_role": "central",
                }
            ],
        )
        encrypted = bytes((8, 4, 0, 7, 0, 1))
        self.assertEqual(
            probe.parse_hci(packet(3, encrypted), 0, raw, handles)[0]["enabled"], True
        )
        other = le_complete(bytes((1, 2, 3, 4, 5, 6)), handle=8, role=1)
        self.assertEqual(probe.parse_hci(packet(3, other), 0, raw, handles), [])
        failed = le_complete(raw, handle=9, status=1)
        self.assertEqual(probe.parse_hci(packet(3, failed), 0, raw, handles), [])
        self.assertNotIn(9, handles)

    def test_reset_and_other_peer_handle_reuse_clear_target_mapping(self):
        raw, handles = target(), {7}
        other = le_complete(bytes((1, 2, 3, 4, 5, 6)), role=1)
        self.assertEqual(probe.parse_hci(packet(3, other), 0, raw, handles), [])
        self.assertNotIn(7, handles)
        handles.add(8)
        reset = (0x0C03).to_bytes(2, "little") + b"\0"
        self.assertEqual(
            probe.parse_hci(packet(2, reset), 0, raw, handles),
            [{"event": "pc_hci_reset"}],
        )
        self.assertEqual(handles, set())

    def test_hci_inner_length_and_truncation_are_rejected(self):
        raw, handles = target(), set()
        valid = le_complete(raw)
        self.assertEqual(
            probe.parse_hci(packet(3, bytes((0x3E, 20)) + valid[2:]), 0, raw, handles),
            [],
        )
        self.assertEqual(probe.parse_hci(packet(3, valid[:-1]), 0, raw, handles), [])
        self.assertEqual(
            probe.parse_hci(
                packet(3, bytes((0x3E, 12)) + valid[2:14]), 0, raw, handles
            ),
            [],
        )
        enhanced = le_complete(raw, role=1, enhanced=True)
        self.assertEqual(
            probe.parse_hci(packet(3, enhanced), 0, raw, handles)[0]["pc_role"],
            "peripheral",
        )

    def test_extended_create_uses_peer_field_not_connection_parameters(self):
        raw = target()
        payload = bytes((0, 0, 0)) + raw + bytes((1,)) + bytes(16)
        command = bytes((0x43, 0x20, 26)) + payload
        self.assertEqual(
            probe.parse_hci(packet(2, command), 0, raw, set()),
            [{"event": "pc_le_connect_command", "opcode": 0x2043}],
        )
        # The same bytes in timing fields must not identify the target.
        other = bytes(20) + raw
        self.assertEqual(
            probe.parse_hci(packet(2, command[:3] + other), 0, raw, set()), []
        )
        self.assertEqual(probe.parse_hci(packet(2, command[:-1]), 0, raw, set()), [])

    def test_snapshot_separates_acl_le_and_sco_link_types(self):
        raw = target()

        def ioctl(fd, request, buffer, mutate):
            struct.pack_into("HH", buffer, 0, 0, 3)
            for i, link_type in enumerate((1, 0, 128)):
                struct.pack_into(
                    "H6sBBHI", buffer, 4 + 16 * i, i + 7, raw, link_type, 0, 1, 4
                )

        with (
            mock.patch.object(probe.socket, "socket"),
            mock.patch.object(probe.fcntl, "ioctl", side_effect=ioctl),
        ):
            rows = probe.connection_snapshot(raw, 0)
        self.assertEqual([row["transport"] for row in rows], ["BR/EDR", "LE"])
        self.assertTrue(all(row["encrypted"] for row in rows))

    def test_acl_continuations_and_fragments_are_not_att_records(self):
        raw = target()
        pdu = bytes((0x12, 0x34, 0x12, 0xAA))
        full = struct.pack("<HHHH", 7, len(pdu) + 4, len(pdu), 4) + pdu
        self.assertEqual(
            probe.parse_hci(packet(4, full), 0, raw, {7})[0]["opcode"], 0x12
        )
        continuation = struct.pack("<H", 7 | 0x1000) + full[2:]
        self.assertEqual(probe.parse_hci(packet(4, continuation), 0, raw, {7}), [])
        self.assertEqual(probe.parse_hci(packet(4, full[:-1]), 0, raw, {7}), [])

    def test_same_path_properties_remain_bearer_independent_in_output(self):
        path, adapter = "/target", "/adapter"
        le = probe.safe_properties(
            path, "org.bluez.Bearer.LE1", {"Connected": {"data": True}}, path, adapter
        )
        br = probe.safe_properties(
            path,
            "org.bluez.Bearer.BREDR1",
            {"Connected": {"data": False}},
            path,
            adapter,
        )
        self.assertEqual(le["properties"]["Connected"], True)
        self.assertEqual(br["properties"]["Connected"], False)
        self.assertEqual(le["scope"], "target")
        self.assertNotEqual(le["interface"], br["interface"])
        for interface in ("org.bluez.Device1", "org.bluez.Bearer.LE1"):
            event, _ = probe.dbus_event(
                {"path": path, "member": "Connect", "interface": interface},
                path,
                adapter,
            )
            self.assertEqual(event["interface"], interface)

    def test_dbus_filter_redacts_sender_and_rejects_unrelated_paths(self):
        selected = probe.dbus_event(
            {
                "path": "/adapter",
                "interface": "org.bluez.GattManager1",
                "member": "RegisterApplication",
                "sender": ":9.4",
            },
            "/target",
            "/adapter",
        )
        self.assertEqual(selected[0]["event"], "resource_method_call")
        self.assertEqual(selected[1], ":9.4")
        self.assertIsNone(
            probe.dbus_event(
                {"path": "/other", "member": "Connect"}, "/target", "/adapter"
            )
        )
        self.assertEqual(
            probe.dbus_event(
                {
                    "path": "/org/freedesktop/DBus",
                    "interface": "org.freedesktop.DBus",
                    "member": "NameOwnerChanged",
                    "payload": {"data": ["org.bluez"]},
                },
                "/target",
                "/adapter",
            )[0]["event"],
            "bluez_owner_changed",
        )

    def test_trusted_and_bearer_setter_attribution_excludes_other_values(self):
        message = {
            "path": "/target",
            "interface": "org.freedesktop.DBus.Properties",
            "member": "Set",
            "sender": ":9.4",
            "payload": {"data": ["org.bluez.Device1", "Trusted", {"data": True}]},
        }
        event, sender = probe.dbus_event(message, "/target", "/adapter")
        self.assertEqual(
            (event["property"], event["value"], sender), ("Trusted", True, ":9.4")
        )
        message["payload"]["data"][1:] = ["Alias", {"data": "private name"}]
        self.assertIsNone(probe.dbus_event(message, "/target", "/adapter"))

    def test_errors_and_redaction_do_not_echo_sensitive_text(self):
        value = "prefix " + ":".join(f"{item:02X}" for item in target()[::-1]) + " :2.7"
        cleaned = probe.redact(value)
        self.assertNotIn(value.split()[1], cleaned)
        self.assertNotIn(":2.7", cleaned)

    def test_parser_and_target_loading_do_not_open_sockets(self):
        with (
            mock.patch.object(
                probe.socket, "socket", side_effect=AssertionError("socket opened")
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(probe.main(["--address-file", "missing", "snapshot"]), 1)

    def test_large_monitor_record_is_not_limited_to_readline_default(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"{" + b"x" * 70000 + b"}\n")
            reader.feed_eof()
            lines = []
            async for line in probe.read_json_lines(reader):
                lines.append(line)
            return lines

        self.assertEqual(len(asyncio.run(run())[0]), 70002)

    def test_persistent_stream_iterator_keeps_multiple_lines_from_one_chunk(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b"one\ntwo\n")
            reader.feed_eof()
            iterator = probe.read_json_lines(reader).__aiter__()
            return [await iterator.__anext__(), await iterator.__anext__()]

        self.assertEqual(asyncio.run(run()), [b"one", b"two"])


if __name__ == "__main__":
    unittest.main()
