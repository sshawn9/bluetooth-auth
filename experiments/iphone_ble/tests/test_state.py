import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from state import LabState, MODES, read_json


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = LabState(Path(self.directory.name) / "runtime")
        self.state.prepare()

    def test_identities_persist_and_never_share_keys(self):
        first = self.state.public_manifest()
        self.state.prepare()
        self.assertEqual(first, self.state.public_manifest())
        self.assertEqual(len({item["address"] for item in first.values()}), 3)
        configs = [read_json(self.state.config(mode)) for mode in MODES]
        self.assertEqual(len({item["irk"] for item in configs}), 3)
        self.assertEqual(len({item["keystore"] for item in configs}), 3)
        self.assertTrue(all(item["identity_address_type"] == 1 and not item["classic_enabled"] and not item["classic_smp_enabled"] for item in configs))
        self.assertEqual(os.stat(self.state.root).st_mode & 0o777, 0o700)
        self.assertTrue(all(os.stat(self.state.config(mode)).st_mode & 0o777 == 0o600 for mode in MODES))

    def test_changed_config_is_not_overwritten(self):
        path = self.state.config("hid")
        original = path.read_text()
        path.write_text(original.replace("BT-Auth-HID", "User edit"))
        with self.assertRaises(ValueError):
            self.state.prepare()
        self.assertIn("User edit", path.read_text())

    def test_pending_recovery_cannot_be_purged(self):
        self.state.journal.write_text("{}")
        with self.assertRaises(RuntimeError):
            self.state.purge()
        self.assertTrue(self.state.manifest_path.exists())
        self.assertTrue(self.state.journal.exists())

    def test_unknown_file_prevents_any_deletion(self):
        extra = self.state.root / "notes.txt"
        extra.write_text("keep")
        with self.assertRaises(RuntimeError):
            self.state.purge()
        self.assertEqual(extra.read_text(), "keep")
        self.assertTrue(self.state.config("ancs").exists())

    def test_symlink_never_followed(self):
        outside = Path(self.directory.name) / "outside"
        outside.write_text("keep")
        (self.state.root / "hid" / "keys.json").symlink_to(outside)
        with self.assertRaises((RuntimeError, ValueError)):
            self.state.purge()
        with self.assertRaises(ValueError):
            self.state.config("hid")
        self.assertEqual(outside.read_text(), "keep")

    def test_active_run_blocks_clean(self):
        with self.state.lock():
            with self.assertRaises(RuntimeError):
                self.state.purge()

    def test_purge_removes_owned_tree_including_interrupted_key_save(self):
        (self.state.root / "hid" / "keys.json.tmp").write_text("{}")
        self.state.append_event("result", {"passed": False})
        self.assertEqual(set(self.state.purge()), set(MODES.values()))
        self.assertFalse(self.state.root.exists())

    def test_bond_detection_and_redacted_manifest(self):
        self.assertFalse(self.state.has_bond("ancs"))
        (self.state.root / "ancs" / "keys.json").write_text(json.dumps({"controller": {"phone": {"ltk": {"value": "12" * 16}}}}))
        self.assertTrue(self.state.has_bond("ancs"))
        self.assertNotIn("irk", json.dumps(self.state.public_manifest()))


if __name__ == "__main__":
    unittest.main()
