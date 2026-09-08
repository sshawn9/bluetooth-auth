"""注销短测的判定和清理检查；全部使用假后端。"""

import contextlib
import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hid_release_test as release


PHONE = "AA:BB:CC:DD:EE:FF"


def link(handle=7, encrypted=True):
    return SimpleNamespace(handle=handle, encrypted=encrypted)


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    def test_same_handle_and_encryption_are_required(self):
        release.check_continuity(7, [link()], PHONE, [])
        for links in ([], [link(8)], [link(encrypted=False)], [link(), link()]):
            with self.subTest(links=links), self.assertRaises(release.LinkLost):
                release.check_continuity(7, links, PHONE, [])

    def test_disconnect_cannot_be_hidden_by_reusing_the_handle(self):
        events = [{"address": PHONE, "address_type": 2}]
        with self.assertRaises(release.LinkLost):
            release.check_continuity(7, [link()], PHONE, events)
        release.check_continuity(7, [link()], PHONE, [{"address": PHONE, "address_type": 0}])

    def test_target_requires_exact_unblocked_record_not_authentication_policy(self):
        adapter = "/org/bluez/hci0"
        phone = {"Address": PHONE, "Adapter": adapter, "Paired": True,
                 "Bonded": True, "Trusted": True, "Blocked": False}
        objects = {"/target": {release.DEVICE: phone}}
        self.assertEqual(release.target(objects, adapter, PHONE)[0], "/target")
        for field in ("Paired", "Bonded", "Trusted"):
            with self.subTest(field=field):
                self.assertEqual(release.target(
                    {"/target": {release.DEVICE: {**phone, field: False}}}, adapter, PHONE)[0], "/target")
        missing_bonded = {key: value for key, value in phone.items() if key != "Bonded"}
        self.assertEqual(release.target({"/target": {release.DEVICE: missing_bonded}}, adapter, PHONE)[0], "/target")
        with self.assertRaisesRegex(release.Inconclusive, "Blocked=true"):
            release.target({"/target": {release.DEVICE: {**phone, "Blocked": True}}}, adapter, PHONE)
        with self.assertRaises(release.Inconclusive):
            release.target(objects, adapter, "FF:FF:FF:FF:FF:FF")

    async def test_provider_closes_only_its_bus_without_disconnect_or_settings_calls(self):
        backend = SimpleNamespace(
            bus=SimpleNamespace(unique_name=":1.77", wait_for_disconnect=mock.AsyncMock()),
            open=mock.AsyncMock(), close=mock.AsyncMock(), register=mock.AsyncMock(),
            objects=mock.AsyncMock(return_value={
                "/org/bluez/hci0": {release.ADAPTER: {"Alias": "Computer", "UUIDs": []}},
                "/target": {release.DEVICE: {"Address": PHONE, "Adapter": "/org/bluez/hci0",
                    "Paired": True, "Bonded": False, "Trusted": False, "Blocked": False}},
            }),
        )
        app, advertisement = mock.Mock(), mock.Mock()
        with mock.patch.object(release, "Backend", return_value=backend), \
             mock.patch.object(release, "HidApplication", return_value=app), \
             mock.patch.object(release, "Advertising", return_value=advertisement), \
             mock.patch.object(release.asyncio, "to_thread", new=mock.AsyncMock(return_value=b"")), \
             contextlib.redirect_stdout(io.StringIO()):
            await release.provider(SimpleNamespace(adapter="hci0"), PHONE)
        self.assertEqual(backend.register.await_args_list, [
            mock.call("/org/bluez/hci0", release.GATT_MANAGER, release.APP_PATH),
            mock.call("/org/bluez/hci0", release.AD_MANAGER, release.AD_PATH),
        ])
        backend.close.assert_awaited_once()
        backend.bus.wait_for_disconnect.assert_awaited_once()
        app.unexport.assert_not_called()
        advertisement.unexport.assert_not_called()

    async def test_provider_registration_error_also_closes_bus(self):
        from adapter import BlueZCallError
        error = BlueZCallError("org.bluez.Error.Failed", "open", [PHONE])
        backend = SimpleNamespace(bus=None, open=mock.AsyncMock(side_effect=error),
                                  close=mock.AsyncMock())
        output = io.StringIO()
        with mock.patch.object(release, "Backend", return_value=backend), contextlib.redirect_stdout(output):
            self.assertEqual(await release.provider(SimpleNamespace(adapter="hci0"), PHONE), 2)
        backend.close.assert_awaited_once()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        event = next(item for item in events if item["event"] == "provider_error")
        self.assertEqual(event["event"], "provider_error")
        self.assertEqual(event["dbus_error"], "org.bluez.Error.Failed")
        self.assertNotIn(PHONE, output.getvalue())

    async def test_provider_reports_close_error_without_leaking_exception_text(self):
        backend = SimpleNamespace(bus=None, open=mock.AsyncMock(side_effect=RuntimeError("fixture")),
                                  close=mock.AsyncMock(side_effect=OSError(9, PHONE)))
        output = io.StringIO()
        with mock.patch.object(release, "Backend", return_value=backend), contextlib.redirect_stdout(output):
            self.assertEqual(await release.provider(SimpleNamespace(adapter="hci0"), PHONE), 2)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        error = next(item for item in events if item.get("phase") == "close_dbus")
        self.assertEqual(error["error_type"], "OSError")
        self.assertEqual(error["errno"], 9)
        self.assertNotIn(PHONE, output.getvalue())
        self.assertNotIn("provider_closed", [item["event"] for item in events])

    async def test_stop_uses_pipe_eof_and_wait_not_bluetooth_disconnect(self):
        child = SimpleNamespace(stdin=mock.Mock(), wait=mock.AsyncMock(return_value=0))
        await release.stop_provider(child)
        child.stdin.close.assert_called_once()
        child.wait.assert_awaited_once()

    async def test_forced_termination_cannot_be_a_successful_stop(self):
        child = SimpleNamespace(stdin=mock.Mock(), returncode=None,
                                wait=mock.AsyncMock(side_effect=[TimeoutError(), -9]))
        child.kill = mock.Mock(side_effect=lambda: setattr(child, "returncode", -9))
        with self.assertRaisesRegex(release.Inconclusive, "退出码 -9"):
            await release.stop_provider(child)
        child.kill.assert_called_once()

    async def simulate(self, *, connected=True, encrypted=True, registration_error=False, survive=True,
                       premature_exit=False, manual_connect=False, preexisting=False,
                       disconnect_on_register=False, replace_on_register=False, lose_companion=False,
                       phone_properties=None, shutdown_events=None, exit_code=0):
        """驱动完整父进程流程；总线、链路和子进程管道均为假对象。"""
        world = {"started": False, "exited": False, "clock": 0.0}
        real_sleep = asyncio.sleep
        disconnected = []
        adapter_path = "/org/bluez/hci0"
        output = io.StringIO()
        child = SimpleNamespace(stdout=asyncio.StreamReader(), returncode=None)

        def active():
            return (connected and (preexisting or world["started"]) and not registration_error
                    and (survive or not world["exited"]))

        async def objects():
            await real_sleep(0)
            if premature_exit and world["clock"] >= 2:
                close_child()
            registered = world["started"] and not world["exited"] and not registration_error
            return {
                adapter_path: {
                    release.ADAPTER: {"Alias": "Computer", "Powered": True,
                                      "UUIDs": ["1812"] if registered else []},
                    release.GATT_MANAGER: {}, release.AD_MANAGER: {"ActiveInstances": int(registered)},
                },
                "/target": {release.DEVICE: {"Address": PHONE, "Adapter": adapter_path,
                    "Paired": True, "Bonded": True, "Trusted": True, "Blocked": False,
                    "Connected": active(), **(phone_properties or {})}},
            }

        def close_child():
            if world["exited"]:
                return
            world["exited"] = True
            child.returncode = 2 if registration_error else exit_code
            final_events = ([{"event": "provider_closed", "dbus_closed": True}]
                            if shutdown_events is None else shutdown_events)
            for event in final_events:
                child.stdout.feed_data((json.dumps(event) + "\n").encode())
            child.stdout.feed_eof()
            if not survive:
                disconnected.append({"address": PHONE, "address_type": 2})
            if lose_companion:
                disconnected.append({"address": "FF:FF:FF:FF:FF:FF", "address_type": 0})

        child.stdin = SimpleNamespace(close=close_child)
        child.wait = mock.AsyncMock(side_effect=lambda: child.returncode)

        async def spawn(*args, **kwargs):
            world["started"] = True
            if disconnect_on_register:
                disconnected.append({"address": PHONE, "address_type": 2})
            events = [{"event": "gatt_registered"}]
            if registration_error:
                events.append({"event": "provider_error", "phase": "register_advertisement",
                               "error_type": "BlueZCallError", "dbus_error": "org.bluez.Error.Failed"})
            else:
                events.append({"event": "registered", "owner": ":1.77"})
            for event in events:
                child.stdout.feed_data((json.dumps(event) + "\n").encode())
            return child

        async def sleep(seconds):
            world["clock"] += seconds
            await real_sleep(0.001 if not connected or not encrypted else 0)

        backend = SimpleNamespace(open=mock.AsyncMock(), close=mock.AsyncMock(), objects=objects,
                                  owner_alive=mock.AsyncMock(side_effect=lambda _: not world["exited"]))
        def current_links():
            result = {(PHONE, 0)}  # 目标原有 BR/EDR 仍属于受保护的其他连接。
            if not (lose_companion and world["exited"]):
                result.add(("FF:FF:FF:FF:FF:FF", 0))
            if active():
                result.add((PHONE, 2))
            return result

        mgmt = SimpleNamespace(close=mock.AsyncMock(), disconnect_events=disconnected,
                               connections=mock.AsyncMock(side_effect=current_links))
        reader = SimpleNamespace(read=lambda _: [SimpleNamespace(
            address=PHONE, link_type=release.HCI_LE_LINK, state=release.BT_CONNECTED,
            handle=8 if replace_on_register and world["started"] else 7,
            encrypted=encrypted)] if active() else [])
        with mock.patch.object(release, "Backend", return_value=backend), \
             mock.patch.object(release, "open_link", new=mock.AsyncMock(return_value=mgmt)), \
             mock.patch.object(release, "LinkReader", return_value=reader), \
             mock.patch.object(release.asyncio, "create_subprocess_exec", side_effect=spawn), \
             mock.patch.object(release.asyncio, "sleep", side_effect=sleep), \
             mock.patch.object(release, "time", SimpleNamespace(monotonic=lambda: world["clock"])), \
             contextlib.redirect_stdout(output):
            code = await release.experiment(SimpleNamespace(
                phone_file="fixture-address", adapter="hci0", wait_seconds=0.02, observe_seconds=1,
                manual_connect=manual_connect), PHONE)
        backend.close.assert_awaited_once()
        mgmt.close.assert_awaited_once()
        self.assertNotIn(PHONE, output.getvalue())
        self.assertNotIn(":1.77", output.getvalue())
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    async def test_cached_hid_without_fresh_read_can_enter_release_and_pass(self):
        code, events = await self.simulate()
        self.assertEqual(code, 0)
        names = [event["event"] for event in events]
        self.assertLess(names.index("advertising"), names.index("waiting_for_connection"))
        initial = next(event for event in events if event["event"] == "connected")
        self.assertFalse(initial["encrypted_hid_read_seen"])
        self.assertTrue(initial["hci_encrypted"])
        self.assertEqual(next(event for event in events if event["event"] == "result")["verdict"], "passed")

    async def test_manual_setup_is_labelled_without_claiming_automatic_reconnection(self):
        code, events = await self.simulate(manual_connect=True)
        self.assertEqual(code, 0)
        for name in ("waiting_for_connection", "connected", "result"):
            self.assertEqual(next(event for event in events if event["event"] == name)["connection_setup"], "manual")

    async def test_existing_link_is_reused_and_not_counted_as_a_new_connection(self):
        code, events = await self.simulate(preexisting=True)
        self.assertEqual(code, 0)
        names = [event["event"] for event in events]
        self.assertIn("existing_connection", names)
        self.assertNotIn("waiting_for_connection", names)
        self.assertIn("provider_exited", names)
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["connection_setup"], "existing")
        self.assertEqual(result["verdict"], "passed")

    async def test_existing_encrypted_link_does_not_require_bond_or_trust_flags(self):
        flags = {"Paired": False, "Bonded": False, "Trusted": False}
        code, events = await self.simulate(preexisting=True, phone_properties=flags)
        self.assertEqual(code, 0)
        reported = next(event for event in events if event["event"] == "target_state")
        for key, value in flags.items():
            self.assertEqual(reported[key], value)
        self.assertIn("provider_exited", [event["event"] for event in events])
        self.assertEqual(next(event for event in events if event["event"] == "result")["verdict"], "passed")

    async def test_relaxed_metadata_does_not_allow_an_unencrypted_link(self):
        code, events = await self.simulate(preexisting=True, encrypted=False,
            phone_properties={"Paired": False, "Bonded": None, "Trusted": False})
        self.assertEqual(code, 2)
        result = next(event for event in events if event["event"] == "result")
        self.assertIn("未确认链路加密", result["reason"])
        self.assertFalse(result["state"]["target"]["Trusted"])
        self.assertNotIn("releasing", [event["event"] for event in events])

    async def test_blocked_target_reports_the_actual_property_before_starting_provider(self):
        code, events = await self.simulate(preexisting=True, phone_properties={"Blocked": True})
        self.assertEqual(code, 2)
        result = next(event for event in events if event["event"] == "result")
        self.assertIn("Blocked=true", result["reason"])
        self.assertNotIn("preparing_hid", [event["event"] for event in events])

    async def test_existing_target_loss_after_release_is_failed_not_companion_interference(self):
        code, events = await self.simulate(preexisting=True, survive=False)
        self.assertEqual(code, 1)
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["verdict"], "failed")
        self.assertEqual(result["connection_setup"], "existing")

    async def test_existing_link_cannot_be_replaced_during_registration(self):
        for kwargs in ({"disconnect_on_register": True}, {"replace_on_register": True}):
            with self.subTest(kwargs=kwargs):
                code, events = await self.simulate(preexisting=True, **kwargs)
                self.assertEqual(code, 2)
                result = next(event for event in events if event["event"] == "result")
                self.assertEqual(result["phase"], "register")
                self.assertEqual(result["verdict"], "inconclusive")
                self.assertNotIn("releasing", [event["event"] for event in events])

    async def test_existing_link_still_requires_encryption(self):
        code, events = await self.simulate(preexisting=True, encrypted=False)
        self.assertEqual(code, 2)
        result = next(event for event in events if event["event"] == "result")
        self.assertIn("未确认链路加密", result["reason"])
        self.assertNotIn("releasing", [event["event"] for event in events])

    async def test_other_original_connections_remain_protected_when_reusing_target(self):
        code, events = await self.simulate(preexisting=True, lose_companion=True)
        self.assertEqual(code, 2)
        result = next(event for event in events if event["event"] == "result")
        self.assertIn("原有其他连接发生中断", result["reason"])

    async def test_registration_failure_never_enters_connection_wait(self):
        code, events = await self.simulate(registration_error=True)
        self.assertEqual(code, 2)
        names = [event["event"] for event in events]
        self.assertIn("provider_error", names)
        self.assertNotIn("waiting_for_connection", names)
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["phase"], "register")

    async def test_timeouts_distinguish_no_connection_from_no_encryption(self):
        for kwargs, reason in (({"connected": False}, "未观察到目标手机 LE"),
                               ({"encrypted": False}, "未确认链路加密")):
            with self.subTest(kwargs=kwargs):
                code, events = await self.simulate(**kwargs)
                self.assertEqual(code, 2)
                result = next(event for event in events if event["event"] == "result")
                self.assertIn(reason, result["reason"])
                self.assertTrue(result["state"]["gatt_registered"])
                self.assertTrue(result["state"]["advertisement_registered"])
                self.assertNotIn("releasing", [event["event"] for event in events])

    async def test_loss_on_provider_exit_still_fails_without_fresh_hid_read(self):
        code, events = await self.simulate(survive=False)
        self.assertEqual(code, 1)
        self.assertEqual(next(event for event in events if event["event"] == "result")["verdict"], "failed")

    async def test_provider_exit_during_baseline_is_not_a_successful_planned_release(self):
        code, events = await self.simulate(premature_exit=True)
        self.assertEqual(code, 2)
        result = next(event for event in events if event["event"] == "result")
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["phase"], "baseline")
        self.assertNotIn("releasing", [event["event"] for event in events])

    async def test_release_errors_are_reported_without_reusing_the_old_link_snapshot(self):
        cases = [
            {"exit_code": 2},
            {"shutdown_events": []},
            {"shutdown_events": [{"event": "provider_error", "phase": "close_dbus",
                "error_type": "OSError", "errno": 9, "message": PHONE}], "exit_code": 2},
            {"shutdown_events": [{"event": "error", "message": PHONE},
                                  {"event": "provider_closed", "dbus_closed": True}]},
        ]
        for case in cases:
            with self.subTest(case=case):
                code, events = await self.simulate(preexisting=True, **case)
                self.assertEqual(code, 2)
                result = next(event for event in events if event["event"] == "result")
                self.assertEqual(result["phase"], "release")
                self.assertNotIn("hci_encrypted", result["state"])
                self.assertNotIn("hid_uuid_present", result["state"])
                self.assertIn("provider_returncode", result["state"])
                self.assertNotIn("observing", [event["event"] for event in events])
                if any(item.get("event") in {"provider_error", "error"}
                       for item in case.get("shutdown_events", [])):
                    self.assertIn("provider_error", [event["event"] for event in events])


if __name__ == "__main__":
    unittest.main()
