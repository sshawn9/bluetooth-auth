from __future__ import annotations

import importlib.util
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "check_public_privacy.py"
SPEC = importlib.util.spec_from_file_location("public_privacy", MODULE)
privacy = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(privacy)


def sample(*parts: str) -> str:
    return "".join(parts)


class PublicPrivacyTests(unittest.TestCase):
    def test_reports_locations_without_echoing_sensitive_values(self):
        mac = sample("12", ":34", ":56", ":78", ":9A", ":BC")
        home = sample("/", "home", "/person/project")
        token = sample("gh", "p_", "A" * 24)
        source_key = sample("source_", "attachment_id")
        content = f"{home}\n{mac}\n{token}\n{source_key}\n".encode()
        findings = privacy.scan(["notes.txt"], lambda _path: content)
        self.assertEqual([item[1:] for item in findings], [
            (1, "personal-absolute-path"), (2, "bluetooth-address"),
            (3, "private-key-or-token"), (4, "original-source-linkage"),
        ])
        rendered = "\n".join(":".join(map(str, item)) for item in findings)
        self.assertNotIn(mac, rendered)
        self.assertNotIn(token, rendered)

    def test_private_paths_and_fixture_policy_are_narrow(self):
        device_path = sample("/org/bluez/hci0/dev_", "AA_BB_CC_DD_EE_FF")
        findings = privacy.scan(
            [".runtime/events.jsonl", "experiments/iphone_ble/tests/test_radio.py", "tests/new_test.py"],
            lambda path: ((b"11" + b":22:33:44:55:66\n" + device_path.encode()) if path.endswith(".py") else b""),
        )
        self.assertIn((".runtime/events.jsonl", 0, "private-runtime-path-tracked"), findings)
        self.assertNotIn(("experiments/iphone_ble/tests/test_radio.py", 1, "bluetooth-address"), findings)
        self.assertIn(("tests/new_test.py", 1, "bluetooth-address"), findings)
        self.assertIn(("tests/new_test.py", 2, "bluez-device-address-path"), findings)

    def test_existing_fixture_does_not_allow_arbitrary_address(self):
        unknown = ":".join(("AB", "BC", "CD", "DE", "EF", "F1"))
        findings = privacy.scan(["experiments/iphone_ble/tests/test_radio.py"], lambda _path: unknown.encode())
        self.assertEqual(findings, [("experiments/iphone_ble/tests/test_radio.py", 1, "bluetooth-address")])

    def test_symlink_target_is_never_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private-data"
            target.write_text(sample("gh", "p_", "A" * 24))
            (root / "link").symlink_to("private-data")
            self.assertEqual(privacy._worktree_reader(str(root))("link"), b"private-data")

    def test_git_failure_does_not_echo_private_root(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = privacy.main(["--root", str(Path(directory) / "private-location")])
        self.assertEqual(status, 2)
        self.assertNotIn(directory, stderr.getvalue())

    def test_worktree_and_index_readers_are_not_needed_for_fixture_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "safe.txt").write_text("public", encoding="utf-8")
            findings = privacy.scan(["safe.txt"], privacy._worktree_reader(str(root)))
            self.assertEqual(findings, [])

    def test_deleted_worktree_files_remain_visible_in_staged_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "present.txt").write_text("public", encoding="utf-8")
            contents = b"present.txt\0removed.txt\0"
            private = sample("gh", "p_", "A" * 24).encode()

            def git_read(_root, arguments):
                if arguments == ["ls-files", "--deleted", "-z"]:
                    return b"removed.txt\0"
                if arguments[0] == "ls-files":
                    return contents
                return private if arguments == ["show", ":removed.txt"] else b"public"

            with mock.patch.object(privacy, "_git", side_effect=git_read):
                self.assertEqual(privacy.main(["--root", str(root)]), 0)
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(privacy.main(["--root", str(root), "--staged"]), 1)
            self.assertEqual(output.getvalue(), "removed.txt:1:private-key-or-token\n")


if __name__ == "__main__":
    unittest.main()
