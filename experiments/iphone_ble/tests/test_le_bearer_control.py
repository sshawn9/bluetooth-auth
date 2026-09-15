from __future__ import annotations
import importlib.util
from pathlib import Path
import tempfile, unittest
from unittest import mock

M = Path(__file__).resolve().parents[1] / "diagnostics" / "le_bearer_control.py"
S = importlib.util.spec_from_file_location("control", M)
c = importlib.util.module_from_spec(S)
S.loader.exec_module(c)
RAW = bytes.fromhex("A1B2C3D4E5F6")[::-1]
INFO = b"[LinkKey]\nKey=x\n\n[PeripheralLongTermKey]\nKey=l\n\n[SlaveLongTermKey]\nKey=s\n\n[IdentityResolvingKey]\nKey=i\n\n[LocalSignatureKey]\nKey=a\n\n[RemoteSignatureKey]\nKey=b\n\n[General]\nPreferredBearer=le\nTrusted=true\n"


def rows(*r):
    return len(r).to_bytes(2, "little") + b"".join(x + bytes((t,)) for x, t in r)


class T(unittest.TestCase):
    def test_type_zero_and_variants_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p, b = Path(d) / "info", Path(d) / "backup"
            p.write_bytes(INFO)
            calls = []

            def m(i, o, x):
                calls.append((o, x))
                if o == c.MGMT_GET_CONNECTIONS:
                    return rows((RAW, 1))
                p.write_bytes(INFO.replace(b"[LinkKey]\nKey=x\n\n", b""))
                return RAW + b"\0"

            with (
                mock.patch.object(c, "mgmt_request", side_effect=m),
                mock.patch.object(
                    c,
                    "connection_snapshot",
                    return_value=[
                        {"transport": "LE", "connected": True, "encrypted": True}
                    ],
                ),
                mock.patch.object(c, "info_path", return_value=p),
                mock.patch.object(c, "emit"),
            ):
                c.remove_classic_pairing("hci0", RAW, 0, str(b))
            self.assertEqual(calls[-1], (c.MGMT_UNPAIR_DEVICE, RAW + b"\0\0"))
            self.assertFalse(b.exists())
            self.assertIn(b"SlaveLongTermKey", p.read_bytes())

    def test_wrong_peer_and_random_le(self):
        with mock.patch.object(c, "mgmt_request", return_value=rows((bytes(6), 0))):
            with self.assertRaises(c.ControlError):
                c.run_disconnect(RAW, 0, "bredr")
        calls = []

        def m(i, o, x):
            calls.append((o, x))
            return rows((RAW, 2)) if o == c.MGMT_GET_CONNECTIONS else RAW + b"\2"

        with (
            mock.patch.object(c, "mgmt_request", side_effect=m),
            mock.patch.object(c, "emit"),
        ):
            c.run_disconnect(RAW, 0, "le")
        self.assertEqual(calls[-1], (c.MGMT_DISCONNECT, RAW + b"\2"))

    def test_bond_info_reads_info_not_dbus(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "info"
            p.write_bytes(INFO)
            with (
                mock.patch.object(c, "info_path", return_value=p),
                mock.patch.object(c, "emit") as emit,
                mock.patch.object(c, "busctl", side_effect=AssertionError()),
            ):
                c.bond_info("hci0", RAW)
            self.assertTrue(emit.call_args.kwargs["has_le_ltk"])
            self.assertEqual(emit.call_args.kwargs["preferred_bearer"], "le")

    def test_bad_reply_status_and_budget(self):
        class Sock:
            def settimeout(self, x):
                pass

            def send(self, x):
                pass

            def recv(self, n):
                return (
                    (1).to_bytes(2, "little")
                    + (0).to_bytes(2, "little")
                    + (3).to_bytes(2, "little")
                    + c.MGMT_DISCONNECT.to_bytes(2, "little")
                    + b"\x01"
                )

            def __enter__(self):
                return self

            def __exit__(self, *x):
                pass

        with mock.patch.object(c, "open_hci", return_value=Sock()):
            with self.assertRaises(c.ControlError):
                c.mgmt_request(0, c.MGMT_DISCONNECT, b"", 0.01)

    def test_mgmt_uses_actual_complete_and_status_event_numbers(self):
        opcode = c.MGMT_DISCONNECT
        status_ok = (
            (0x0002).to_bytes(2, "little")
            + (0).to_bytes(2, "little")
            + (3).to_bytes(2, "little")
            + opcode.to_bytes(2, "little")
            + b"\0"
        )
        complete = (
            (0x0001).to_bytes(2, "little")
            + (0).to_bytes(2, "little")
            + (10).to_bytes(2, "little")
            + opcode.to_bytes(2, "little")
            + b"\0"
            + RAW
            + b"\0"
        )

        class Sock:
            def __init__(self):
                self.frames = [status_ok, complete]

            def settimeout(self, x):
                pass

            def send(self, x):
                pass

            def recv(self, n):
                return self.frames.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *x):
                pass

        with mock.patch.object(c, "open_hci", return_value=Sock()):
            self.assertEqual(c.mgmt_request(0, opcode, b"", 1), RAW + b"\0")

    def test_help_no_access(self):
        with (
            mock.patch.object(c, "open_hci", side_effect=AssertionError()),
            mock.patch.object(c, "busctl", side_effect=AssertionError()),
        ):
            with self.assertRaises(SystemExit):
                c.main(["--help"])

    def test_unpair_failure_retains_private_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p, b = Path(d) / "info", Path(d) / "backup"
            p.write_bytes(INFO)
            with (
                mock.patch.object(
                    c,
                    "mgmt_request",
                    side_effect=[
                        rows((RAW, 1)),
                        c.ControlError("kernel rejected target operation"),
                    ],
                ),
                mock.patch.object(
                    c,
                    "connection_snapshot",
                    return_value=[
                        {"transport": "LE", "connected": True, "encrypted": True}
                    ],
                ),
                mock.patch.object(c, "info_path", return_value=p),
            ):
                with self.assertRaisesRegex(c.ControlError, "backup retained"):
                    c.remove_classic_pairing("hci0", RAW, 0, str(b))
            self.assertTrue(b.exists())

    def test_any_le_key_change_retains_private_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p, b = Path(d) / "info", Path(d) / "backup"
            p.write_bytes(INFO)

            def m(i, o, x):
                if o == c.MGMT_GET_CONNECTIONS:
                    return rows((RAW, 1))
                p.write_bytes(INFO.replace(b"Key=s", b"Key=changed"))
                return RAW + b"\0"

            with (
                mock.patch.object(c, "mgmt_request", side_effect=m),
                mock.patch.object(
                    c,
                    "connection_snapshot",
                    return_value=[
                        {"transport": "LE", "connected": True, "encrypted": True}
                    ],
                ),
                mock.patch.object(c, "info_path", return_value=p),
            ):
                with self.assertRaisesRegex(c.ControlError, "LE key verification"):
                    c.remove_classic_pairing("hci0", RAW, 0, str(b))
            self.assertTrue(b.exists())


if __name__ == "__main__":
    unittest.main()
