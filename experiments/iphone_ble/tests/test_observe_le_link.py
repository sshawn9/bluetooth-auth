"""observe_le_link 的离线判定检查。"""

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
import observe_le_link as observe


PHONE = "AA:BB:CC:DD:EE:FF"


class ObserveTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, *, connected=True, encrypted=True, hid=False, advertisements=0,
                       mgmt_connected=True, mgmt_drops=False, survive=True, reconnect_after_disconnect=False):
        world = {"time": 0.0}
        adapter = "/org/bluez/hci0"
        events, output = [], io.StringIO()
        real_sleep = asyncio.sleep
        disconnects = []

        def active(): return connected and (survive or reconnect_after_disconnect or world["time"] < 0.4)
        async def objects():
            return {adapter: {observe.ADAPTER: {"UUIDs": ["1812"] if hid else []},
                              observe.AD_MANAGER: {"ActiveInstances": int(hid) + advertisements}},
                    "/phone": {observe.DEVICE: {"Address": PHONE, "Adapter": adapter,
                        "Paired": True, "Bonded": True, "Trusted": True, "Blocked": False,
                        "Connected": active()}}}
        async def links():
            return {(PHONE, 2)} if active() and mgmt_connected and not (mgmt_drops and world["time"] >= 0.4) else set()
        def read(_):
            if not active(): return []
            return [SimpleNamespace(address=PHONE, link_type=observe.HCI_LE_LINK,
                                    state=observe.BT_CONNECTED, handle=7, encrypted=encrypted)]
        async def sleep(seconds):
            world["time"] += seconds
            if (not active() or reconnect_after_disconnect and world["time"] >= 0.4) and not disconnects:
                disconnects.append({"address": PHONE, "address_type": 2})
            await real_sleep(0)
        backend = SimpleNamespace(open=mock.AsyncMock(), close=mock.AsyncMock(), objects=objects)
        mgmt = SimpleNamespace(close=mock.AsyncMock(), connections=links, disconnect_events=disconnects)
        with mock.patch.object(observe, "Backend", return_value=backend), \
             mock.patch.object(observe, "open_link", new=mock.AsyncMock(return_value=mgmt)), \
             mock.patch.object(observe, "LinkReader", return_value=SimpleNamespace(read=read)), \
             mock.patch.object(observe.asyncio, "sleep", side_effect=sleep), \
             mock.patch.object(observe, "time", SimpleNamespace(monotonic=lambda: world["time"])), \
             contextlib.redirect_stdout(output):
            code = await observe.observe(SimpleNamespace(adapter="hci0", wait_seconds=0.2, observe_seconds=1), PHONE)
        self.assertNotIn(PHONE, output.getvalue())
        self.assertEqual(mgmt.close.await_count, 1)
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    async def test_encrypted_link_passes_without_hid(self):
        code, events = await self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(next(item for item in events if item["event"] == "result")["verdict"], "passed")

    async def test_hid_or_advertisement_is_inconclusive(self):
        for kwargs in ({"hid": True}, {"advertisements": 1}):
            with self.subTest(kwargs=kwargs):
                code, events = await self.run_case(**kwargs)
                self.assertEqual(code, 2)
                self.assertEqual(next(item for item in events if item["event"] == "result")["verdict"], "inconclusive")

    def test_missing_resource_metadata_is_inconclusive(self):
        for objects in ({}, {"/org/bluez/hci0": {observe.ADAPTER: {"UUIDs": []}}},
                        {"/org/bluez/hci0": {observe.ADAPTER: {}, observe.AD_MANAGER: {"ActiveInstances": 0}}},
                        {"/org/bluez/hci0": {observe.ADAPTER: {"UUIDs": []}, observe.AD_MANAGER: {}}}):
            with self.subTest(objects=objects):
                with self.assertRaises(observe.Inconclusive):
                    observe.clean_resources(objects, "/org/bluez/hci0")

    async def test_unencrypted_link_times_out(self):
        code, events = await self.run_case(encrypted=False)
        self.assertEqual(code, 2)
        self.assertEqual(next(item for item in events if item["event"] == "result")["phase"], "connect")

    async def test_mgmt_and_hci_must_agree(self):
        code, events = await self.run_case(mgmt_connected=False)
        self.assertEqual(code, 2)
        self.assertEqual(next(item for item in events if item["event"] == "result")["phase"], "connect")

    async def test_mgmt_loss_during_observation_cannot_pass_with_hci_still_present(self):
        code, events = await self.run_case(mgmt_drops=True)
        self.assertEqual(code, 2)
        result = next(item for item in events if item["event"] == "result")
        self.assertEqual(result["verdict"], "inconclusive")
        self.assertEqual(result["phase"], "observe")

    async def test_disconnect_during_observation_fails(self):
        code, events = await self.run_case(survive=False)
        self.assertEqual(code, 1)
        self.assertEqual(next(item for item in events if item["event"] == "result")["verdict"], "failed")

    async def test_disconnect_event_with_reused_handle_fails(self):
        code, events = await self.run_case(reconnect_after_disconnect=True)
        self.assertEqual(code, 1)
        self.assertEqual(next(item for item in events if item["event"] == "result")["verdict"], "failed")


if __name__ == "__main__":
    unittest.main()
