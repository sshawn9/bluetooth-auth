#!/usr/bin/env python3
"""Verify this public archive offline; never reads a Bluetooth address file."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
LAB = Path(__file__).resolve().parents[1]
REPOSITORY = LAB.parents[1]
sys.path.insert(0, str(LAB))
from check_public_privacy import scan  # noqa: E402


def main() -> int:
    manifest_path = LAB / "results/LE_BEARER_RECOVERY.provenance.json"
    manifest = json.loads(manifest_path.read_text())
    failures = []
    paths = []
    for entry in manifest["files"]:
        relative = entry["path"]
        path = (LAB / relative).resolve()
        if not path.is_relative_to(LAB) or not path.is_file():
            failures.append(f"missing/invalid artifact: {relative}")
            continue
        paths.append(str(path.relative_to(REPOSITORY)))
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            failures.append(f"checksum mismatch: {relative}")
    event_count = 0
    for segment in manifest["capture_segments"]:
        rows = [
            json.loads(line)
            for line in (LAB / segment["path"]).read_text().splitlines()
        ]
        if len(rows) != segment["records"]:
            failures.append(f"record count mismatch: {segment['id']}")
        event_count += len(rows)
        for index, row in enumerate(rows, 1):
            if row.get("record_id") != f"{segment['id']}{index:04d}":
                failures.append(f"record ID mismatch: {segment['id']} row {index}")
    paths.append(str(manifest_path.relative_to(REPOSITORY)))
    for path, line, category in scan(
        paths, lambda path: (REPOSITORY / path).read_bytes()
    ):
        failures.append(f"{path}:{line}:{category}")
    for message in failures:
        print(message, file=sys.stderr)
    if failures:
        return 1
    print(
        f"Verified {len(manifest['files'])} public artifacts, {event_count} historical events and privacy patterns"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
