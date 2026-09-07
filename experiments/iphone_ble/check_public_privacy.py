#!/usr/bin/env python3
"""Check publishable Git files for a small set of privacy-sensitive artifacts.

This checks the current worktree by default.  ``--staged`` reads the Git index
instead.  It deliberately does not inspect Git history, ignored files, network
resources, or Bluetooth state.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


PRIVATE_PARTS = frozenset((".venv", ".runtime", ".coexist", ".git", ".private",
                           "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"))
PRIVATE_NAMES = frozenset((".environment.json", ".environment.json.tmp", ".env"))
# These source files contain deliberately invented addresses used by fake
# backends.  Other tests are still scanned, so a newly added fixture does not
# receive a blanket exemption.
SYNTHETIC_ADDRESS_FIXTURES = frozenset((
    "experiments/iphone_ble/tests/test_adapter.py",
    "experiments/iphone_ble/tests/test_coexist.py",
    "experiments/iphone_ble/tests/test_coexist_link.py",
    "experiments/iphone_ble/tests/test_coexist_pairing.py",
    "experiments/iphone_ble/tests/test_radio.py",
    "experiments/iphone_ble/tests/test_rssi_config.py",
))
# Only these reviewed byte patterns are synthetic.  A real address pasted
# into an existing fixture is still reported.  Spaces avoid storing MAC-like
# literals in the scanner itself.
SYNTHETIC_ADDRESS_BYTES = frozenset(tuple(value.split()) for value in (
    "00 11 22 33 44 55", "01 02 03 04 05 06", "11 12 13 14 15 16",
    "11 22 33 44 55 66", "12 34 56 78 9A BC", "12 34 56 78 9A BD",
    "40 11 22 33 44 55", "AA BB CC DD EE FF", "C1 C2 C3 C4 C5 C6",
    "E0 E1 E2 E3 E4 E5", "F0 F1 F2 F3 F4 F5",
))

_HOME_PATH = re.compile(r"/(?:home|Users)/[^\s\"']+")
_MAC = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
_DEVICE_PATH = re.compile(r"dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f])")
_SOURCE_LINKAGE = re.compile(r"\bsource_(?:attachment_id|sha256)\b", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE(?: [A-Z ]*)?-----"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def _private_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = PurePosixPath(path).name
    return (bool(PRIVATE_PARTS.intersection(parts)) or name in PRIVATE_NAMES
            or (name.startswith(".env.") and name != ".env.example"))


def _placeholder_mac(value: str) -> bool:
    octets = re.split(r"[:_-]", value.upper())
    return (len(set(octets)) == 1 and octets[0] in {"00", "FF"}) or tuple(octets) == tuple("AA BB CC DD EE FF".split())


def _allowed_fixture(path: str, value: str) -> bool:
    return path in SYNTHETIC_ADDRESS_FIXTURES and tuple(re.split(r"[:_-]", value.upper())) in SYNTHETIC_ADDRESS_BYTES


def _line_issues(path: str, line: str) -> set[str]:
    issues: set[str] = set()
    if _HOME_PATH.search(line):
        issues.add("personal-absolute-path")
    for match in _DEVICE_PATH.finditer(line):
        if not _allowed_fixture(path, match[1]):
            issues.add("bluez-device-address-path")
    if _SOURCE_LINKAGE.search(line):
        issues.add("original-source-linkage")
    if any(pattern.search(line) for pattern in _SECRET_PATTERNS):
        issues.add("private-key-or-token")
    for match in _MAC.finditer(line):
        if not _placeholder_mac(match.group()) and not _allowed_fixture(path, match.group()):
            issues.add("bluetooth-address")
            break
    return issues


def scan(paths: Iterable[str], read: Callable[[str], bytes]) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for path in sorted(set(paths)):
        if _private_path(path):
            findings.append((path, 0, "private-runtime-path-tracked"))
            continue
        try:
            text = read(path).decode("utf-8", "replace")
        except OSError:
            findings.append((path, 0, "unreadable-publishable-file"))
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for issue in sorted(_line_issues(path, line)):
                findings.append((path, number, issue))
    return findings


def _git(root: str, arguments: list[str]) -> bytes:
    return subprocess.check_output(["git", "-C", root, *arguments], stderr=subprocess.PIPE)


def _paths(root: str, staged: bool) -> list[str]:
    arguments = ["ls-files", "--cached", "-z"] if staged else ["ls-files", "-co", "--exclude-standard", "-z"]
    paths = [item for item in _git(root, arguments).split(b"\0") if item]
    if not staged:
        # Git still lists tracked files after a worktree move or deletion.
        # The index scan must continue to inspect their staged content.
        deleted = set(_git(root, ["ls-files", "--deleted", "-z"]).split(b"\0"))
        paths = [path for path in paths if path not in deleted]
    return [path.decode("utf-8", "surrogateescape") for path in paths]


def _worktree_reader(root: str) -> Callable[[str], bytes]:
    def read(path: str) -> bytes:
        candidate = Path(root) / PurePosixPath(path)
        if candidate.is_symlink():
            return str(candidate.readlink()).encode()
        if not candidate.resolve().is_relative_to(Path(root).resolve()):
            raise OSError("publishable path resolves outside the worktree")
        return candidate.read_bytes()
    return read


def _index_reader(root: str) -> Callable[[str], bytes]:
    def read(path: str) -> bytes:
        return _git(root, ["show", f":{path}"])
    return read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="read tracked content from the Git index")
    parser.add_argument("--root", default=".", help="Git worktree to inspect (default: current directory)")
    arguments = parser.parse_args(argv)
    root = str(PurePosixPath(arguments.root))
    try:
        paths = _paths(root, arguments.staged)
        findings = scan(paths, _index_reader(root) if arguments.staged else _worktree_reader(root))
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"privacy-check: cannot read Git worktree: {type(error).__name__}", file=sys.stderr)
        return 2
    for path, line, issue in findings:
        print(f"{path}:{line}:{issue}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
