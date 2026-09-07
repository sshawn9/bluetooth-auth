import argparse
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "diagnostics" / "monitor_rssi.py"
SPEC = importlib.util.spec_from_file_location("monitor_rssi", SCRIPT)
monitor_rssi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor_rssi)


SYNTHETIC_ADDRESS = "12:34:56:78:9A:BC"


def inquiry_packet(address, rssi=-42):
    data = bytes((4, 0x0D, 0, 0, 0))
    packet = bytearray(20 + len(data))
    import struct
    struct.pack_into("<HHH", packet, 0, 0x0012, 0, len(packet) - 6)
    packet[6:12] = bytes.fromhex(address.replace(":", ""))[::-1]
    packet[12] = 0
    packet[13] = rssi & 0xFF
    struct.pack_into("<I", packet, 14, 0)
    struct.pack_into("<H", packet, 18, len(data))
    packet[20:] = data
    return bytes(packet)


class RssiConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def test_explicit_path_has_priority_over_environment(self):
        self.assertEqual(
            monitor_rssi.resolve_address_file("/tmp/explicit", {monitor_rssi.ADDRESS_FILE_ENV: "/tmp/environment"}),
            "/tmp/explicit",
        )
        self.assertEqual(
            monitor_rssi.resolve_address_file(None, {monitor_rssi.ADDRESS_FILE_ENV: "/tmp/environment"}),
            "/tmp/environment",
        )

    def test_load_address_requires_valid_file_without_echoing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "address"
            valid.write_text(SYNTHETIC_ADDRESS + "\n", encoding="utf-8")
            self.assertEqual(monitor_rssi.load_address(valid), SYNTHETIC_ADDRESS)
            invalid = Path(directory) / "invalid"
            invalid.write_text("not-an-address", encoding="utf-8")
            with self.assertRaises(ValueError) as error:
                monitor_rssi.load_address(invalid)
            self.assertNotIn(str(invalid), str(error.exception))

    async def test_invalid_configuration_prevents_wireless_main(self):
        args = argparse.Namespace(address_file=None)
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(monitor_rssi, "main", new_callable=mock.AsyncMock) as main:
            with self.assertRaises(ValueError):
                await monitor_rssi.run(args)
        main.assert_not_awaited()

    def test_cli_config_error_has_no_traceback_or_private_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "private-address"
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--address-file", str(missing)],
                capture_output=True, text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(directory, result.stderr)

    def test_target_address_filters_inquiry_reports(self):
        self.assertEqual(monitor_rssi.scan_rssi(inquiry_packet(SYNTHETIC_ADDRESS), 0, SYNTHETIC_ADDRESS), -42)
        self.assertIsNone(monitor_rssi.scan_rssi(inquiry_packet("12:34:56:78:9A:BD"), 0, SYNTHETIC_ADDRESS))
        self.assertEqual(monitor_rssi.redact_addresses("failure " + SYNTHETIC_ADDRESS), "failure <蓝牙地址>")
