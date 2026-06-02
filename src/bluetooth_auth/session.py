from __future__ import annotations

import subprocess


class Session:
    def __init__(self, user: str) -> None:
        self.user = user

    def get_session_properties(self, session_id: str) -> dict[str, str]:
        result = subprocess.run(
            ("loginctl", "show-session", session_id),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return {}

        data = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                data[key] = value
        return data

    def is_active_local_graphical_user_session(self, data: dict[str, str]) -> bool:
        return (
            data.get("Name") == self.user
            and data.get("Class") == "user"
            and data.get("Remote") == "no"
            and data.get("Active") == "yes"
            and data.get("Type") == "wayland"
        )

    def find_active_graphical_session(self) -> str:
        result = subprocess.run(
            ("loginctl", "list-sessions", "--no-legend"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return ""

        candidates = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue

            session_id = parts[0]
            data = self.get_session_properties(session_id)
            if self.is_active_local_graphical_user_session(data):
                candidates.append((data.get("Seat") != "seat0", session_id))

        if not candidates:
            return ""

        candidates.sort()
        return candidates[0][1]

    def is_locked(self) -> bool:
        session_id = self.find_active_graphical_session()
        if not session_id:
            return False

        return self.get_session_properties(session_id).get("LockedHint") == "yes"
