import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bluez_hid_lab as lab
import coexist_gatt as gatt
from dbus_fast import Variant

PHONE = "11:22:33:44:55:66"
MOUSE = "C1:C2:C3:C4:C5:C6"
ADAPTER_ADDRESS = "AA:BB:CC:DD:EE:FF"
PATH = "/org/bluez/hci0"
PHONE_PATH = PATH + "/dev_" + PHONE.replace(":", "_")
MOUSE_PATH = PATH + "/dev_" + MOUSE.replace(":", "_")


class Bus:
    def __init__(self, owner):
        self.unique_name = owner
        self.exports = {}

    def export(self, path, interface):
        self.exports[path, interface.name] = interface

    def unexport(self, path, interface):
        self.exports.pop((path, interface.name), None)


class Hub:
    def __init__(self, state):
        self.state = state
        self.properties = {"Address": ADAPTER_ADDRESS, "Alias": "x", "Powered": True,
                           "PowerState": "on", "Pairable": True, "PairableTimeout": 0,
                           "Discoverable": False, "DiscoverableTimeout": 180, "Connectable": False,
                           "UUIDs": ["0000180a-0000-1000-8000-00805f9b34fb"]}
        self.phone = {"Address": PHONE, "Adapter": PATH, "Paired": True, "Bonded": True,
                      "Trusted": True, "Blocked": False, "Connected": True}
        self.links = {(PHONE, 0), (MOUSE, 2)}
        self.original = set(self.links)
        self.original_props = copy.deepcopy(self.properties)
        self.owners = set()
        self.counter = 0
        self.calls = []
        self.managers = []
        self.app = None
        self.register_error = False
        self.interrupt_mouse = False
        self.skip_hid_read = False
        self.add_phone_audio = False
        self.cancel_after_register = False
        self.restore_set_error = False
        self.interrupt_phone_during_hold = False
        self.advertising_started = False
        self.post_register_snapshots = 0
        self.pairing_agent = None
        self.agent_register_error = False
        self.pairing_rpa = None
        self.pairing_device_path = None
        self.pairing_device = None
        self.resolve_pairing_identity = True
        self.mgmt_retains_rpa = False
        self.request_pairing = True
        self.traces = []
        self.add_phone_classic = False

    def backend(self):
        return FakeBackend(self)

    def app_factory(self, bus, path, emit, allow_device, **kwargs):
        self.app = gatt.HidApplication(bus, path, emit, allow_device, **kwargs)
        return self.app

    async def open_link(self, index, *, baseline=None):
        self.calls.append(("open_mgmt", index))
        link = FakeLink(self, baseline)
        self.managers.append(link)
        return link


class FakeBackend:
    def __init__(self, hub):
        self.hub = hub
        self.bus = None
        self.bus_id = "offline_bus"
        self.bluez_owner = ":1.1"
        self.changes = []

    async def open(self):
        self.hub.counter += 1
        self.bus = Bus(f":1.{self.hub.counter + 10}")
        self.hub.owners.add(self.bus.unique_name)
        self.hub.calls.append(("open_bus",))
        return self

    async def objects(self):
        if self.hub.advertising_started:
            self.hub.post_register_snapshots += 1
            if self.hub.interrupt_phone_during_hold and self.hub.post_register_snapshots == 2:
                self.hub.managers[0].disconnect_events.append(
                    {"address": PHONE, "address_type": 1, "reason": 2})
        result = {
            PATH: {lab.ADAPTER: copy.deepcopy(self.hub.properties), lab.GATT_MANAGER: {},
                   lab.AD_MANAGER: {"ActiveInstances": 0}},
            PHONE_PATH: {lab.DEVICE: {**self.hub.phone, "Connected": any(peer == PHONE for peer, _ in self.hub.links)}},
            MOUSE_PATH: {lab.DEVICE: {"Address": MOUSE, "Adapter": PATH, "Connected": (MOUSE, 2) in self.hub.links}},
        }
        if self.hub.add_phone_audio and (PHONE, 1) in self.hub.links:
            result[PHONE_PATH + "/fd0"] = {lab.MEDIA_TRANSPORT: {"Device": PHONE_PATH, "State": "active"}}
        if self.hub.pairing_device_path:
            result[self.hub.pairing_device_path] = {lab.DEVICE: copy.deepcopy(self.hub.pairing_device)}
        return result

    async def register_agent(self):
        if not self.hub.state.pending():
            raise AssertionError("agent registered before durable baseline")
        self.hub.calls.append(("register_agent",))
        self.hub.pairing_agent = self.bus.exports[lab.AGENT_PATH, "org.bluez.Agent1"]
        if self.hub.agent_register_error:
            raise TimeoutError("RegisterAgent reply lost")

    async def unregister_agent(self):
        self.hub.calls.append(("unregister_agent",))

    async def set_pairable(self, path, value):
        if not self.hub.state.pending():
            raise AssertionError("settings changed before durable baseline")
        if value and self.hub.restore_set_error:
            raise RuntimeError("restore Set failed")
        self.hub.calls.append(("set_pairable", value))
        self.hub.properties["Pairable"] = value

    async def register(self, path, interface, root):
        self.hub.calls.append(("register", interface))
        if interface != lab.AD_MANAGER:
            return
        self.hub.advertising_started = True
        pairing_rpa = self.hub.pairing_rpa
        peer = pairing_rpa or PHONE
        self.hub.links.add((peer, 2 if pairing_rpa else 1))
        if self.hub.add_phone_classic:
            self.hub.links.add((PHONE, 0))
        if self.hub.register_error:
            raise TimeoutError("reply lost after server registered advertisement")
        if self.hub.interrupt_mouse:
            # The link has already reappeared in the snapshot. Only the event
            # history proves this was not uninterrupted coexistence.
            self.hub.managers[0].disconnect_events.append(
                {"address": MOUSE, "address_type": 2, "reason": 2})
        if self.hub.cancel_after_register:
            raise asyncio.CancelledError()
        device_path = PHONE_PATH
        if self.hub.pairing_agent and self.hub.request_pairing:
            if pairing_rpa:
                device_path = PATH + "/dev_" + pairing_rpa.replace(":", "_")
                self.hub.pairing_device_path = device_path
                self.hub.pairing_device = {"Adapter": PATH, "Address": pairing_rpa, "AddressType": "random",
                                           "Paired": False, "Bonded": False, "Trusted": False, "Blocked": False}
            await self.hub.pairing_agent.request_confirmation(device_path, 240584)
            if pairing_rpa:
                if self.hub.resolve_pairing_identity:
                    self.hub.pairing_device.update(self.hub.phone)
                    self.hub.pairing_device["AddressType"] = "public"
                    if not self.hub.mgmt_retains_rpa:
                        self.hub.links.remove((pairing_rpa, 2))
                        self.hub.links.add((PHONE, 1))
                self.hub.pairing_device.update(Paired=True, Bonded=True)
            else:
                self.hub.phone.update(Paired=True, Bonded=True)
            await self.hub.pairing_agent.authorize_service(device_path, lab.HID_UUID)
        if not self.hub.skip_hid_read:
            self.hub.app.report_map.read_value({"device": Variant("o", device_path), "link": Variant("s", "LE")})
        self.hub.app.report.StartNotify.__dict__["__DBUS_METHOD"].fn(self.hub.app.report)

    async def unregister(self, path, interface, root):
        self.hub.calls.append(("unregister", interface))

    async def owner_alive(self, owner):
        return owner in self.hub.owners

    async def close(self):
        if self.bus is not None:
            if any(name == "org.bluez.Agent1" for _, name in self.bus.exports):
                raise AssertionError("pairing agent export leaked")
            self.hub.owners.discard(self.bus.unique_name)
            self.hub.calls.append(("close_bus", self.bus.unique_name))
            self.bus = None


class FakeLink:
    def __init__(self, hub, baseline):
        self.hub = hub
        self.baseline = None if baseline is None else set(baseline)
        self.disconnect_events = []
        self.closed = False

    async def connections(self):
        return set(self.hub.links)

    async def disconnect_le(self, peer, kind):
        if self.baseline is None or kind == 0 or (peer, kind) in self.baseline:
            raise AssertionError("attempt to disconnect unowned/original link")
        self.hub.calls.append(("disconnect_le", peer, kind))
        self.hub.links.remove((peer, kind))

    async def close(self):
        self.closed = True


class CoexistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        here = mock.patch.object(lab, "HERE", Path(self.directory.name) / "tool")
        here.start()
        self.addCleanup(here.stop)
        self.state = lab.State(Path(self.directory.name) / "state")
        self.hub = Hub(self.state)
        self.args = SimpleNamespace(adapter="hci0", phone=PHONE, phone_file=None,
                                    wait_seconds=0.025, hold_seconds=0.005)
        self.confirm = mock.AsyncMock(return_value=True)
        self.trace_factory = None

    async def run_probe(self):
        with self.state.lock(), contextlib.redirect_stdout(io.StringIO()), \
             mock.patch.object(lab, "POLL_SECONDS", 0.001), \
             mock.patch.object(lab, "PROGRESS_SECONDS", 0.003):
            code = await lab.run(self.args, self.state, backend_factory=self.hub.backend,
                                 link_factory=self.hub.open_link, app_factory=self.hub.app_factory,
                                 ad_factory=gatt.Advertising, confirm_pairing=self.confirm,
                                 trace_factory=self.trace_factory)
        events = [json.loads(line) for line in (self.state.root / "events.jsonl").read_text().splitlines()]
        return code, events

    def assert_restored(self):
        self.assertEqual(self.hub.properties, self.hub.original_props)
        self.assertEqual(self.hub.links, self.hub.original)
        self.assertFalse(self.hub.owners)
        self.assertFalse(self.state.pending())
        self.assertTrue(all(link.closed for link in self.hub.managers))

    async def test_success_preserves_phone_classic_and_mouse_le(self):
        code, events = await self.run_probe()
        self.assertEqual(code, 0, events)
        result = events[-1]
        self.assertTrue(result["passed"])
        self.assertTrue(result["encrypted_phone_hid_access"])
        self.assertFalse(result["phone_hid_subscription_verified"])
        self.assertFalse(result["phone_audio_isolation_verified"])
        self.assertTrue(next(item for item in events if item["event"] == "restore")["settings_restored"])
        self.assertIn(("disconnect_le", PHONE, 1), self.hub.calls)
        self.assertNotIn(self.hub.app.dis, self.hub.app.objects)  # Existing DIS is reused.
        self.assert_restored()

    async def test_no_companion_fails_before_any_mutation(self):
        self.hub.links = {(PHONE, 0)}
        code, events = await self.run_probe()
        self.assertEqual(code, 1)
        self.assertIn("需先", events[-1]["error"]["reason"])
        self.assertFalse(any(call[0] in {"set_pairable", "register", "disconnect_le"} for call in self.hub.calls))
        self.assertFalse(self.state.pending())

    async def test_unpaired_phone_refused_before_advertisement(self):
        self.hub.phone["Paired"] = False
        code, _ = await self.run_probe()
        self.assertEqual(code, 1)
        self.assertFalse(any(call[0] in {"set_pairable", "register", "disconnect_le", "open_mgmt"} for call in self.hub.calls))

    async def test_registration_reply_loss_still_cleans_and_restores(self):
        self.hub.register_error = True
        code, events = await self.run_probe()
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["error"]["phase"], "register")
        self.assertIn(("unregister", lab.AD_MANAGER), self.hub.calls)
        self.assert_restored()

    async def test_transient_original_drop_never_passes_even_if_reconnected(self):
        self.hub.interrupt_mouse = True
        code, events = await self.run_probe()
        self.assertEqual(code, 1)
        self.assertFalse(events[-1]["passed"])
        self.assertEqual(events[-1]["original_links_lost"][0]["address"], MOUSE)
        restoration = next(item for item in events if item["event"] == "restore")
        self.assertEqual(restoration["original_links_interrupted"][0]["address"], MOUSE)
        self.assert_restored()

    async def test_cancellation_after_registration_retains_drop_evidence(self):
        self.hub.interrupt_mouse = self.hub.cancel_after_register = True
        code, events = await self.run_probe()
        self.assertEqual(code, 130)
        self.assertFalse(events[-1]["passed"])
        self.assertTrue(events[-1]["original_links_lost"])
        self.assert_restored()

    async def test_existing_new_phone_audio_fails_without_disconnecting_classic(self):
        self.hub.add_phone_audio = True
        code, events = await self.run_probe()
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["new_phone_audio_transports"], 1)
        self.assert_restored()

    async def test_target_transient_drop_during_hold_is_not_hidden_by_reconnect(self):
        self.hub.interrupt_phone_during_hold = True
        code, events = await self.run_probe()
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["error"]["phase"], "hold")
        self.assertFalse(events[-1]["passed"])
        self.assert_restored()

    async def test_global_subscription_without_target_read_is_inconclusive(self):
        self.hub.skip_hid_read = True
        code, events = await self.run_probe()
        self.assertEqual(code, 3)
        self.assertEqual(events[-1]["verdict"], "inconclusive")
        self.assertFalse(events[-1]["passed"])
        self.assert_restored()

    async def test_restore_failure_retains_journal_and_cannot_pass(self):
        self.hub.restore_set_error = True
        code, events = await self.run_probe()
        self.assertEqual(code, 2)
        self.assertFalse(events[-1]["passed"])
        self.assertTrue(self.state.pending())
        self.assertIn("restore_error", events[-1])
        self.assertEqual(self.hub.links, self.hub.original)
        self.assertFalse(self.hub.owners)

    def enable_repair(self):
        self.args.repair_phone_pairing = True
        self.hub.properties["Pairable"] = False
        self.hub.original_props["Pairable"] = False

    async def test_repair_requires_comparison_and_keeps_new_bond(self):
        self.enable_repair()
        self.hub.phone.update(Paired=False, Bonded=False)
        code, events = await self.run_probe()
        self.assertEqual(code, 0, events)
        self.confirm.assert_awaited_once_with(240584)
        self.assertTrue(self.hub.phone["Bonded"])
        self.assertTrue(events[-1]["pairing_identity_verified"])
        restoration = next(item for item in events if item["event"] == "restore")
        self.assertEqual(restoration["pairing_records"], "retained")
        self.assertFalse(restoration["pairing_keys_restored"])
        self.assertFalse(restoration["pairing_properties_unchanged"])
        self.assertIn(("set_pairable", True), self.hub.calls)
        self.assertIn(("unregister_agent",), self.hub.calls)
        self.assert_restored()

    async def test_rpa_numeric_comparison_precedes_identity_resolution(self):
        self.enable_repair()
        self.hub.pairing_rpa = "40:11:22:33:44:55"
        code, events = await self.run_probe()
        self.assertEqual(code, 0, events)
        self.assertTrue(events[-1]["pairing_identity_verified"])
        self.confirm.assert_awaited_once()
        self.assert_restored()

    async def test_unresolved_rpa_never_passes_and_its_le_link_is_cleaned(self):
        self.enable_repair()
        self.hub.pairing_rpa = "40:11:22:33:44:55"
        self.hub.resolve_pairing_identity = False
        code, events = await self.run_probe()
        self.assertEqual(code, 3, events)
        self.assertFalse(events[-1]["pairing_identity_verified"])
        self.assertIn(("disconnect_le", self.hub.pairing_rpa, 2), self.hub.calls)
        self.assert_restored()

    async def test_identity_update_does_not_lose_recorded_rpa_link(self):
        self.enable_repair()
        self.hub.pairing_rpa = "40:11:22:33:44:55"
        self.hub.mgmt_retains_rpa = True
        code, events = await self.run_probe()
        self.assertEqual(code, 0, events)
        self.assertTrue(events[-1]["pairing_identity_verified"])
        self.assertIn(("disconnect_le", self.hub.pairing_rpa, 2), self.hub.calls)
        self.assert_restored()

    async def test_repair_refused_confirmation_cleans_agent_and_window(self):
        self.enable_repair()
        self.confirm.return_value = False
        code, events = await self.run_probe()
        self.assertEqual(code, 1, events)
        self.assertFalse(events[-1]["pairing_confirmation_accepted"])
        self.assertIn(("unregister_agent",), self.hub.calls)
        self.assert_restored()

    async def test_repair_agent_registration_reply_loss_still_unregistered(self):
        self.enable_repair()
        self.hub.agent_register_error = True
        code, events = await self.run_probe()
        self.assertEqual(code, 1, events)
        self.assertIn(("unregister_agent",), self.hub.calls)
        self.assertFalse(any(item[0] == "register" for item in self.hub.calls))
        self.assert_restored()

    async def test_repair_existing_pair_flag_is_not_fresh_pairing_evidence(self):
        self.enable_repair()
        self.hub.request_pairing = False
        self.hub.skip_hid_read = True
        code, events = await self.run_probe()
        self.assertEqual(code, 3, events)
        self.assertFalse(events[-1]["pairing_confirmation_accepted"])
        self.assertFalse(events[-1]["passed"])
        self.assert_restored()

    async def test_added_classic_link_retains_recovery_and_never_claims_complete(self):
        self.enable_repair()
        self.hub.links = self.hub.original = {(MOUSE, 2)}
        self.hub.add_phone_classic = True
        code, events = await self.run_probe()
        self.assertEqual(code, 1, events)
        self.assertTrue(self.state.pending())
        restoration = next(item for item in events if item["event"] == "restore")
        self.assertFalse(restoration["connections_restored"])
        self.assertEqual(restoration["new_phone_classic_connections"][0]["address"], PHONE)
        self.assertFalse(events[-1]["passed"])

    async def test_trace_is_closed_without_claiming_packets_from_open_alone(self):
        self.args.trace_advertising = True
        trace = SimpleNamespace(summary=lambda: {"scope": "local_controller_commands", "commands": [], "statuses": []},
                                close=mock.AsyncMock())
        self.trace_factory = mock.AsyncMock(return_value=trace)
        code, events = await self.run_probe()
        self.assertEqual(code, 0, events)
        trace.close.assert_awaited_once()
        advertisement = next(item for item in events if item["event"] == "advertising")
        self.assertTrue(advertisement["controller_trace_enabled"])
        self.assertNotIn("controller_packets_observed", advertisement)
        self.assert_restored()

    async def test_trace_open_failure_restores_without_registering_advertisement(self):
        self.args.trace_advertising = True
        self.trace_factory = mock.AsyncMock(side_effect=RuntimeError("monitor unavailable"))
        code, events = await self.run_probe()
        self.assertEqual(code, 1, events)
        self.assertFalse(any(item[0] == "register" for item in self.hub.calls))
        self.assert_restored()

    async def test_run_with_pending_journal_does_not_silently_restore(self):
        with self.state.lock():
            self.state.journal.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "待恢复"):
                await lab.run(self.args, self.state, backend_factory=self.hub.backend,
                              link_factory=self.hub.open_link)
        self.assertFalse(self.hub.calls)

    async def test_restore_without_journal_never_opens_anything(self):
        result = await lab.restore(self.state, backend_factory=mock.Mock(side_effect=AssertionError("bus")),
                                   link_factory=mock.Mock(side_effect=AssertionError("mgmt")))
        self.assertFalse(result["changed"])
        self.assertFalse(self.state.root.exists())

    def test_plan_does_not_create_state_or_import_radio(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lab.main(["--state-dir", str(self.state.root), "plan"]), 0)
        self.assertFalse(self.state.root.exists())

    def test_phone_file_environment_and_explicit_override(self):
        default_path = Path(self.directory.name) / "configured-address"
        explicit_path = Path(self.directory.name) / "override-address"
        default_path.write_text(PHONE.lower() + "\n")
        explicit_path.write_text(MOUSE + "\n")
        with mock.patch.dict("os.environ", {"BLUETOOTH_AUTH_ADDRESS_FILE": str(default_path)}):
            args = lab.parser().parse_args(["run", "--adapter", "hci0"])
            self.assertEqual(lab.configured_phone(args), PHONE)
            args = lab.parser().parse_args(["run", "--adapter", "hci0", "--phone-file", str(explicit_path)])
            self.assertEqual(lab.configured_phone(args), MOUSE)

    async def test_missing_private_configuration_never_opens_backend(self):
        self.args.phone = None
        self.args.phone_file = None
        with self.assertRaisesRegex(ValueError, "BLUETOOTH_AUTH_ADDRESS_FILE"):
            await lab.run(self.args, self.state, backend_factory=self.hub.backend,
                          link_factory=self.hub.open_link)
        self.assertEqual(self.hub.calls, [])

    def test_phone_file_error_does_not_echo_private_path_or_contents(self):
        self.args.phone = None
        self.args.phone_file = Path(self.directory.name) / "private-file"
        with self.assertRaises(ValueError) as failed:
            lab.configured_phone(self.args)
        self.assertNotIn(self.directory.name, str(failed.exception))
        self.args.phone_file.write_text("private-invalid-content")
        with self.assertRaises(ValueError) as failed:
            lab.configured_phone(self.args)
        self.assertNotIn("private-invalid-content", str(failed.exception))


if __name__ == "__main__":
    unittest.main()
