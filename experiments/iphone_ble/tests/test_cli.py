import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import adapter
import ble_lab
from state import KIND, LabState, atomic_json
from test_adapter import FakeBlueZ


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.state = self.base / "runtime"
        # Cross-entrypoint guards must inspect a fixture, never the user's
        # sudo-owned runtime directory beside the real script.
        here = mock.patch.object(ble_lab, "HERE", self.base / "tool")
        here.start()
        self.addCleanup(here.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ble_lab.main(["--state-dir", str(self.state), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_offline_commands_cannot_reach_hardware(self):
        with mock.patch.object(adapter.BlueZBackend, "open", side_effect=AssertionError("hardware access")):
            self.assertEqual(self.invoke("plan")[0], 0)
            self.assertFalse(self.state.exists())
            self.assertEqual(self.invoke("prepare")[0], 0)
            self.assertEqual(self.invoke("report")[0], 0)
            self.assertEqual(self.invoke("restore")[0], 0)
            self.assertEqual(self.invoke("clean")[0], 0)
            self.assertFalse(self.state.exists())

    def test_no_bond_refused_before_handoff(self):
        LabState(self.state).prepare()
        fake_radio = types.SimpleNamespace(ProbeOptions=object, run_probe=mock.AsyncMock())
        with mock.patch.dict(sys.modules, {"radio": fake_radio}), \
             mock.patch.object(ble_lab, "require_root"), \
             mock.patch.object(ble_lab, "require_dependencies"), \
             mock.patch.object(adapter, "acquire", new_callable=mock.AsyncMock) as acquire:
            code, _, error = self.invoke("run", "ancs", "--adapter", "hci0")
            self.assertEqual(code, 2)
            self.assertIn("尚无配对", error)
            acquire.assert_not_awaited()

    def test_reenroll_refused_before_handoff(self):
        state = LabState(self.state)
        state.prepare()
        (self.state / "hid" / "keys.json").write_text(json.dumps({"namespace": {"phone": {"ltk": {"value": "12" * 16}}}}))
        fake_radio = types.SimpleNamespace(ProbeOptions=object, run_probe=mock.AsyncMock())
        with mock.patch.dict(sys.modules, {"radio": fake_radio}), \
             mock.patch.object(ble_lab, "require_root"), \
             mock.patch.object(ble_lab, "require_dependencies"), \
             mock.patch.object(adapter, "acquire", new_callable=mock.AsyncMock) as acquire:
            code, _, error = self.invoke("run", "hid", "--adapter", "hci0", "--enroll")
            self.assertEqual(code, 2)
            self.assertIn("已有配对", error)
            acquire.assert_not_awaited()

    def test_uninstall_removes_only_copied_owned_tool(self):
        tool = self.base / "experiments" / "iphone_ble"
        tool.mkdir(parents=True)
        for relative in ble_lab.SOURCE_FILES:
            path = tool / relative
            path.parent.mkdir(exist_ok=True)
            path.write_text("owned fixture")
        runtime = LabState(tool / ".runtime")
        runtime.prepare()
        environment = tool / ".venv"
        environment.mkdir()
        # Environment symlinks must be unlinked, not followed.
        outside = self.base / "unrelated"
        outside.write_text("preserve")
        (environment / "python").symlink_to(outside)
        atomic_json(tool / ".environment.json", {"kind": KIND, "environment": str(environment)})
        with mock.patch.object(ble_lab, "HERE", tool), contextlib.redirect_stdout(io.StringIO()):
            ble_lab.uninstall(runtime)
        self.assertFalse(tool.exists())
        self.assertEqual(outside.read_text(), "preserve")

    def test_uninstall_with_extra_notes_refuses_without_deleting(self):
        tool = self.base / "tool"
        tool.mkdir()
        (tool / "ble_lab.py").write_text("owned")
        (tool / "my-notes.txt").write_text("user")
        runtime = LabState(tool / ".runtime")
        runtime.prepare()
        with mock.patch.object(ble_lab, "HERE", tool), self.assertRaises(RuntimeError):
            ble_lab.uninstall(runtime)
        self.assertTrue(runtime.manifest_path.exists())
        self.assertTrue((tool / "ble_lab.py").exists())

    def test_probe_error_restores_adapter_before_returning(self):
        LabState(self.state).prepare()
        backend = FakeBlueZ()
        original = dict(backend.properties)
        fake_radio = types.SimpleNamespace(
            ProbeOptions=lambda **kwargs: types.SimpleNamespace(**kwargs),
            run_probe=mock.AsyncMock(side_effect=RuntimeError("fake HCI failure")),
        )
        with mock.patch.dict(sys.modules, {"radio": fake_radio}), \
             mock.patch.object(ble_lab, "require_root"), \
             mock.patch.object(ble_lab, "require_dependencies"), \
             mock.patch.object(adapter, "_owned_backend", new=mock.AsyncMock(return_value=(backend, False))):
            code, _, _ = self.invoke("run", "ancs", "--adapter", "hci0", "--enroll", "--cycles", "0")
        self.assertEqual(code, 1)
        self.assertEqual(backend.properties, original)
        self.assertFalse(LabState(self.state).journal.exists())
        self.assertIn("restore", [item["event"] for item in LabState(self.state).results()])

    def test_probe_success_cannot_mask_restore_failure(self):
        state = LabState(self.state)
        state.prepare()
        backend = FakeBlueZ()

        async def fake_probe(*_args):
            backend.fail_after_set = RuntimeError("restore did not finish")
            return {"passed": True}

        fake_radio = types.SimpleNamespace(
            ProbeOptions=lambda **kwargs: types.SimpleNamespace(**kwargs), run_probe=fake_probe,
        )
        with mock.patch.dict(sys.modules, {"radio": fake_radio}), \
             mock.patch.object(ble_lab, "require_root"), \
             mock.patch.object(ble_lab, "require_dependencies"), \
             mock.patch.object(adapter, "_owned_backend", new=mock.AsyncMock(return_value=(backend, False))):
            code, _, _ = self.invoke("run", "ancs", "--adapter", "hci0", "--enroll", "--cycles", "0")
        self.assertEqual(code, 2)
        self.assertTrue(state.journal.exists())
        with self.assertRaises(RuntimeError):
            state.purge()


if __name__ == "__main__":
    unittest.main()
