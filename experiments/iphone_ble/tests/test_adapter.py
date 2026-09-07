import asyncio
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adapter import ADAPTER, BlueZCallError, acquire, restore
from state import read_json


class FakeBlueZ:
    def __init__(self, powered=True):
        self.path = "/org/bluez/hci0"
        self.properties = {"Address": "00:11:22:33:44:55", "Alias": "x", "Powered": powered,
                           "Discoverable": powered, "DiscoverableTimeout": 180,
                           "Pairable": True, "PairableTimeout": 60, "Connectable": powered}
        self.sets = []
        self.reads = 0
        self.fail_after_set = None
        self.missing = False

    async def objects(self):
        self.reads += 1
        return {} if self.missing else {self.path: {ADAPTER: copy.deepcopy(self.properties)}}

    async def set(self, path, name, value):
        assert path == self.path
        self.sets.append((name, value))
        self.properties[name] = value
        if name == "Powered":
            self.properties["Connectable"] = value
            if not value:
                self.properties["Discoverable"] = False
        if self.fail_after_set:
            raise self.fail_after_set


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.journal = Path(self.directory.name) / "adapter-restore.json"

    async def test_restores_settings_after_index_changes(self):
        backend = FakeBlueZ()
        original = copy.deepcopy(backend.properties)
        lease = await acquire("hci0", self.journal, backend=backend)
        self.assertEqual(lease["index"], 0)
        self.assertEqual(read_json(self.journal)["properties"], original)
        backend.path = "/org/bluez/hci3"
        result = await restore(self.journal, backend=backend)
        self.assertTrue(result["settings_restored"])
        self.assertEqual(result["adapter"], "hci3")
        self.assertEqual(backend.properties, original)
        self.assertFalse(self.journal.exists())
        self.assertNotIn("Alias", [name for name, _ in backend.sets])

    async def test_partial_failure_keeps_baseline(self):
        backend = FakeBlueZ()
        backend.fail_after_set = RuntimeError("lost reply after mutation")
        with self.assertRaises(RuntimeError):
            await acquire("hci0", self.journal, backend=backend)
        self.assertFalse(backend.properties["Powered"])
        self.assertTrue(read_json(self.journal)["properties"]["Powered"])
        backend.fail_after_set = None
        await restore(self.journal, backend=backend)
        self.assertTrue(backend.properties["Powered"])

    async def test_cancellation_during_handoff_keeps_baseline(self):
        backend = FakeBlueZ()
        backend.fail_after_set = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await acquire("hci0", self.journal, backend=backend)
        self.assertTrue(self.journal.exists())
        backend.fail_after_set = None
        await restore(self.journal, backend=backend)

    async def test_originally_off_stays_off(self):
        backend = FakeBlueZ(False)
        await acquire("hci0", self.journal, backend=backend)
        await restore(self.journal, backend=backend)
        self.assertNotIn(("Powered", True), backend.sets)
        self.assertFalse(backend.properties["Powered"])

    async def test_unreplayable_off_state_is_rejected_before_mutation(self):
        for name in ("Discoverable", "Connectable"):
            backend = FakeBlueZ(False)
            backend.properties[name] = True
            with self.assertRaises(RuntimeError):
                await acquire("hci0", self.journal, backend=backend)
            self.assertEqual(backend.sets, [])
            self.assertFalse(self.journal.exists())

    async def test_external_rename_is_not_overwritten(self):
        backend = FakeBlueZ()
        await acquire("hci0", self.journal, backend=backend)
        backend.properties["Alias"] = "new user name"
        with self.assertRaises(RuntimeError):
            await restore(self.journal, backend=backend)
        self.assertEqual(backend.properties["Alias"], "new user name")
        self.assertTrue(self.journal.exists())

    async def test_missing_controller_keeps_recovery_record(self):
        backend = FakeBlueZ()
        await acquire("hci0", self.journal, backend=backend)
        backend.missing = True
        with self.assertRaises(RuntimeError):
            await restore(self.journal, backend=backend, timeout=0)
        self.assertTrue(self.journal.exists())

    async def test_no_journal_never_touches_bluez(self):
        backend = FakeBlueZ()
        result = await restore(self.journal, backend=backend)
        self.assertFalse(result["changed"])
        self.assertEqual(backend.reads, 0)
        self.assertEqual(backend.sets, [])

    async def test_restore_failure_retains_original_journal(self):
        backend = FakeBlueZ()
        await acquire("hci0", self.journal, backend=backend)
        original = read_json(self.journal)["properties"]
        backend.fail_after_set = RuntimeError("restore failed")
        with self.assertRaises(RuntimeError):
            await restore(self.journal, backend=backend)
        self.assertEqual(read_json(self.journal)["properties"], original)

    async def test_complete_temporary_journal_can_be_recovered(self):
        backend = FakeBlueZ()
        await acquire("hci0", self.journal, backend=backend)
        temporary = self.journal.with_name(self.journal.name + ".tmp")
        self.journal.rename(temporary)
        result = await restore(self.journal, backend=backend)
        self.assertTrue(result["settings_restored"])
        self.assertTrue(backend.properties["Powered"])
        self.assertFalse(temporary.exists())
        self.assertFalse(self.journal.exists())

    async def test_restore_retries_busy_and_reports_the_property(self):
        class TemporarilyBusy(FakeBlueZ):
            attempts = 0

            async def set(self, path, name, value):
                if name == "Powered" and value is True:
                    self.attempts += 1
                    if self.attempts <= 2:
                        raise BlueZCallError("org.bluez.Error.Busy", "Set", [])
                await super().set(path, name, value)

        backend = TemporarilyBusy()
        await acquire("hci0", self.journal, backend=backend)
        events = []
        result = await restore(self.journal, backend=backend, timeout=3,
                               emit=lambda event, data: events.append((event, data)))
        self.assertTrue(result["settings_restored"])
        self.assertEqual(backend.attempts, 3)
        self.assertTrue(any(data.get("property") == "Powered" and data.get("reason") == "org.bluez.Error.Busy"
                            for _, data in events))
        self.assertFalse(self.journal.exists())

    async def test_busy_after_applied_write_is_not_written_twice(self):
        backend = FakeBlueZ()
        await acquire("hci0", self.journal, backend=backend)
        backend.fail_after_set = BlueZCallError("org.bluez.Error.Busy", "Set", [])
        result = await restore(self.journal, backend=backend)
        self.assertTrue(result["settings_restored"])
        self.assertEqual(backend.sets.count(("Powered", True)), 1)

    async def test_persistent_busy_has_deadline_and_keeps_baseline(self):
        class AlwaysBusy(FakeBlueZ):
            async def set(self, path, name, value):
                if name == "Powered" and value is True:
                    raise BlueZCallError("org.bluez.Error.Busy", "Set", [])
                await super().set(path, name, value)

        backend = AlwaysBusy()
        await acquire("hci0", self.journal, backend=backend)
        baseline = read_json(self.journal)["properties"]
        with self.assertRaisesRegex(RuntimeError, "Powered=True.*Busy"):
            await restore(self.journal, backend=backend, timeout=0.01)
        self.assertEqual(read_json(self.journal)["properties"], baseline)

    async def test_waits_for_bluez_automatic_power_on_without_competing_set(self):
        class PoweringOn(FakeBlueZ):
            transition_reads = None

            async def objects(self):
                if self.transition_reads is not None:
                    if self.transition_reads:
                        self.properties["PowerState"] = "off-enabling"
                        self.transition_reads -= 1
                    else:
                        self.properties.update(Powered=True, Connectable=True, PowerState="on")
                return await super().objects()

        backend = PoweringOn()
        await acquire("hci0", self.journal, backend=backend)
        backend.transition_reads = 3
        result = await restore(self.journal, backend=backend, timeout=3)
        self.assertTrue(result["settings_restored"])
        self.assertNotIn(("Powered", True), backend.sets)


if __name__ == "__main__":
    unittest.main()
