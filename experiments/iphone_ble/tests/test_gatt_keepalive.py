"""无无线硬件的流程回归：真实 iPhone 结果必须由实机日志提供。"""

import asyncio
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gatt_keepalive_test as lab

PHONE = ":".join(["AA", "BB", "CC", "DD", "EE", "FF"])
RAW = bytes.fromhex(PHONE.replace(":", ""))[::-1]
ADAPTER = "/org/bluez/hci0"
PHONE_PATH = ADAPTER + "/phone"
CTS = PHONE_PATH + "/cts/value"
BATTERY = PHONE_PATH + "/battery/value"


def objects(connected=True, notifying=False, hid=False):
    result = {
        ADAPTER: {
            lab.ADAPTER: {
                "Powered": True,
                "UUIDs": ["00001812" + lab.UUID_BASE] if hid else [],
            },
            lab.AD_MANAGER: {"ActiveInstances": 0},
        },
        PHONE_PATH: {
            lab.DEVICE: {
                "Address": PHONE,
                "Adapter": ADAPTER,
                "Paired": True,
                "Bonded": True,
                "Trusted": False,
                "Blocked": False,
                "Connected": connected,
            },
            lab.LE: {"Paired": True, "Bonded": True, "Connected": connected},
        },
    }
    for name, (service, characteristic, _) in lab.PROFILES.items():
        path = PHONE_PATH + "/" + name
        result[path] = {
            lab.SERVICE: {"Device": PHONE_PATH, "UUID": service + lab.UUID_BASE}
        }
        result[path + "/value"] = {
            lab.CHARACTERISTIC: {
                "Service": path,
                "UUID": characteristic + lab.UUID_BASE,
                "Flags": ["read", "notify"],
                "Notifying": notifying,
            }
        }
    return result


class KeepaliveTests(unittest.IsolatedAsyncioTestCase):
    async def case(self, *, service="cts", connected=True, scenario=None):
        from dbus_fast import MessageType

        world = {
            "time": 0.0,
            "connected": connected,
            "encrypted": True,
            "notifying": False,
            "hid": scenario == "hid",
        }
        calls, output = [], io.StringIO()
        backend = SimpleNamespace(
            changes=[], open=mock.AsyncMock(), close=mock.AsyncMock()
        )
        mgmt = SimpleNamespace(
            disconnect_events=[], close=mock.AsyncMock(), _closed=False
        )
        real_sleep = asyncio.sleep

        async def get_objects():
            result = objects(world["connected"], world["notifying"], world["hid"])
            if scenario == "missing_bond":
                result[PHONE_PATH][lab.LE]["Bonded"] = False
            return result

        async def bus_call(message):
            calls.append((message.path, message.interface, message.member))
            world["connected"] = True
            if scenario == "connect_error":
                return SimpleNamespace(
                    message_type=MessageType.ERROR, error_name="org.bluez.Error.Failed"
                )
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN)

        async def call(path, interface, member, *args):
            calls.append((path, interface, member))
            if member == "ReadValue":
                if scenario == "invalid_value":
                    return [b""]
                return [
                    bytes.fromhex("EA0709070C2238050000") if path == CTS else b"\x40"
                ]
            if member == "StartNotify":
                if scenario == "subscribe_error":
                    raise lab.BlueZCallError(
                        "org.bluez.Error.NotAuthorized", member, [PHONE]
                    )
                world["notifying"] = True
            if member == "StopNotify":
                if scenario == "cleanup_error":
                    raise RuntimeError("cleanup failed")
                world["notifying"] = False
            return []

        def links(_raw, _index):
            if not world["connected"]:
                return []
            return [
                {
                    "handle": 7,
                    "transport": "BR/EDR" if scenario == "classic" else "LE",
                    "connected": True,
                    "encrypted": world["encrypted"],
                }
            ]

        async def sleep(seconds):
            world["time"] += seconds
            if scenario in ("disconnect", "disconnect_and_subscription_lost"):
                # 模拟断开后马上以相同句柄连回：快照仍正常，必须由事件判失败。
                mgmt.disconnect_events.append(
                    {"address": PHONE, "address_type": 1, "reason": 3}
                )
                if scenario == "disconnect_and_subscription_lost":
                    world["notifying"] = False
                    backend.changes.append(
                        (CTS, lab.CHARACTERISTIC, {"Notifying": False})
                    )
            elif scenario == "encryption_lost":
                world["encrypted"] = False
            elif scenario == "subscription_lost":
                world["notifying"] = False
            elif scenario == "transient_hid":
                backend.changes.append(
                    (ADAPTER, lab.ADAPTER, {"UUIDs": ["00001812" + lab.UUID_BASE]})
                )
            elif scenario == "notification":
                backend.changes.append(
                    (CTS, lab.CHARACTERISTIC, {"Value": b"private-phone-time"})
                )
            elif scenario == "cancel":
                raise asyncio.CancelledError
            elif scenario == "monitor_lost":
                mgmt._closed = True
            await real_sleep(0)

        backend.objects = get_objects
        backend.bus = SimpleNamespace(call=bus_call)
        backend.connection = SimpleNamespace(call=call)
        args = SimpleNamespace(
            adapter="hci0", service=service, wait_seconds=0.05, observe_seconds=2
        )
        with (
            mock.patch.object(lab, "Backend", return_value=backend),
            mock.patch.object(lab, "open_link", new=mock.AsyncMock(return_value=mgmt)),
            mock.patch.object(lab, "connection_snapshot", side_effect=links),
            mock.patch.object(
                lab, "time", SimpleNamespace(monotonic=lambda: world["time"])
            ),
            mock.patch.object(lab.asyncio, "sleep", side_effect=sleep),
            contextlib.redirect_stdout(output),
        ):
            status = await lab.run(args, RAW)
        self.assertEqual(backend.close.await_count, 1)
        self.assertEqual(mgmt.close.await_count, 1)
        text = output.getvalue()
        self.assertNotIn(PHONE, text)
        self.assertNotIn("private-phone-time", text)
        self.assertNotIn(PHONE_PATH, text)
        events = [json.loads(line) for line in text.splitlines()]
        return status, events, calls

    async def test_existing_connection_reads_and_subscribes_without_connecting(self):
        status, events, calls = await self.case(scenario="notification")
        self.assertEqual(status, 0)
        self.assertEqual(
            [member for _, _, member in calls],
            ["ReadValue", "StartNotify", "StopNotify"],
        )
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["connection_setup"], "existing")
        self.assertEqual(result["verdict"], "passed")
        self.assertGreater(result["value_updates"], 0)

    async def test_active_connect_uses_only_le_bearer_once(self):
        status, events, calls = await self.case(connected=False)
        self.assertEqual(status, 0)
        self.assertEqual(calls[0], (PHONE_PATH, lab.LE, "Connect"))
        self.assertEqual(sum(member == "Connect" for _, _, member in calls), 1)
        self.assertEqual(
            next(event for event in events if event["event"] == "result")[
                "connection_setup"
            ],
            "active",
        )

    async def test_battery_selects_phone_battery_and_control_does_not_use_gatt(self):
        status, _, calls = await self.case(service="battery")
        self.assertEqual(status, 0)
        self.assertTrue(all(path == BATTERY for path, _, _ in calls))
        status, _, calls = await self.case(service="none")
        self.assertEqual(status, 0)
        self.assertEqual(calls, [])

    async def test_disconnect_encryption_and_subscription_loss_cannot_pass(self):
        for scenario in ("disconnect", "encryption_lost", "subscription_lost"):
            with self.subTest(scenario=scenario):
                status, events, calls = await self.case(scenario=scenario)
                self.assertEqual(status, 1)
                self.assertEqual(
                    next(event for event in events if event["event"] == "result")[
                        "verdict"
                    ],
                    "failed",
                )
                self.assertEqual(calls[-1][2], "StopNotify")
                self.assertNotIn("Connect", [member for _, _, member in calls])

    async def test_hid_classic_and_missing_bond_do_not_start_service_use(self):
        for scenario in ("hid", "classic", "missing_bond"):
            with self.subTest(scenario=scenario):
                status, _, calls = await self.case(scenario=scenario)
                self.assertEqual(status, 2)
                self.assertEqual(calls, [])

    async def test_transient_hid_is_not_missed_between_snapshots(self):
        status, events, _ = await self.case(scenario="transient_hid")
        self.assertEqual(status, 2)
        self.assertEqual(
            next(event for event in events if event["event"] == "result")["verdict"],
            "inconclusive",
        )

    async def test_subscription_loss_does_not_hide_disconnect_reason(self):
        status, events, _ = await self.case(scenario="disconnect_and_subscription_lost")
        self.assertEqual(status, 1)
        drops = [event for event in events if event["event"] == "disconnected"]
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["reason_code"], 3)

    async def test_failure_never_falls_back_to_another_service_or_generic_connect(self):
        status, _, calls = await self.case(connected=False, scenario="connect_error")
        self.assertEqual(status, 2)
        self.assertEqual(calls, [(PHONE_PATH, lab.LE, "Connect")])
        for scenario in ("subscribe_error", "invalid_value"):
            with self.subTest(scenario=scenario):
                status, _, calls = await self.case(scenario=scenario)
                self.assertEqual(status, 2)
                self.assertTrue(all(path == CTS for path, _, _ in calls))
                self.assertNotIn("StopNotify", [member for _, _, member in calls])

    async def test_cancel_releases_subscription_and_is_not_passed(self):
        status, events, calls = await self.case(scenario="cancel")
        self.assertEqual(status, 130)
        self.assertEqual(
            next(event for event in events if event["event"] == "result")["verdict"],
            "cancelled",
        )
        self.assertEqual(calls[-1][2], "StopNotify")

    async def test_cleanup_failure_cannot_report_complete_success(self):
        status, events, _ = await self.case(scenario="cleanup_error")
        self.assertEqual(status, 2)
        self.assertEqual(
            next(event for event in events if event["event"] == "result")["verdict"],
            "inconclusive",
        )

    async def test_lost_disconnect_monitor_cannot_pass(self):
        status, events, _ = await self.case(scenario="monitor_lost")
        self.assertEqual(status, 2)
        self.assertEqual(
            next(event for event in events if event["event"] == "result")["verdict"],
            "inconclusive",
        )

    def test_characteristic_must_belong_to_target_service(self):
        obj = objects()
        obj[PHONE_PATH + "/cts"][lab.SERVICE]["Device"] = ADAPTER + "/other"
        self.assertIsNone(lab.find_characteristic(obj, PHONE_PATH, "cts"))

    def test_help_has_no_radio_or_file_operations(self):
        with (
            mock.patch.object(
                lab, "load_target", side_effect=AssertionError("private file read")
            ),
            mock.patch.object(
                lab, "Backend", side_effect=AssertionError("radio access")
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(SystemExit) as result:
                lab.main(["--help"])
        self.assertEqual(result.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
