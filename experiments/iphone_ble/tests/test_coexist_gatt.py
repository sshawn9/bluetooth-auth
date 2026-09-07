import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dbus_fast import Variant
from dbus_fast.errors import DBusError
import coexist_gatt as gatt


class FakeBus:
    def __init__(self, fail_on=None): self.exports, self.fail_on = {}, fail_on
    def export(self, path, interface):
        if (path, interface.name) == self.fail_on: raise RuntimeError("export failed")
        self.exports[(path, interface.name)] = interface
    def unexport(self, path, interface): self.exports.pop((path, interface.name), None)


class CoexistGattTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.bus = FakeBus()
        self.emit = lambda event, details: self.events.append((event, details))
        self.app = gatt.HidApplication(self.bus, "/com/example/hid", self.emit, lambda path: path == "/org/bluez/hci0/dev_OK")
        self.options = {"device": Variant("o", "/org/bluez/hci0/dev_OK"), "link": Variant("s", "le")}

    def test_exports_complete_hogp_object_manager_with_correct_signatures(self):
        self.app.export()
        managed = self.app.managed_objects()
        self.assertEqual(set(managed), {item.path for item in self.app.objects})
        report = managed["/com/example/hid/service0/char3"][gatt.GATT_CHARACTERISTIC]
        self.assertNotIn("Value", report)
        self.assertEqual(report["Flags"].value, ["read", "encrypt-read", "notify"])
        self.assertEqual(self.app.report_reference.read_value(self.options), b"\x01\x01")
        self.assertEqual(self.app.pnp_id.value, b"\x02\x00\x00\x01\x00\x01\x00")
        self.assertIn(("/com/example/hid", gatt.OBJECT_MANAGER), self.bus.exports)
        self.app.unexport()
        self.assertFalse(self.bus.exports)

    def test_read_offsets_authorization_and_target_events(self):
        self.assertEqual(self.app.report.read_value({**self.options, "offset": Variant("q", 1)}), b"")
        with self.assertRaises(DBusError) as denied:
            self.app.report_map.read_value({"device": Variant("o", "/org/bluez/hci0/dev_NO")})
        self.assertEqual(denied.exception.type, "org.bluez.Error.NotAuthorized")
        with self.assertRaises(DBusError) as offset:
            self.app.report.read_value({**self.options, "offset": Variant("q", 2)})
        self.assertEqual(offset.exception.type, "org.bluez.Error.InvalidOffset")
        self.app.report_map.read_value(self.options)
        self.assertEqual(self.events[-1], ("target_gatt_access", {"device": "/org/bluez/hci0/dev_OK", "link": "le", "attribute": "report_map"}))
        self.app.control_point.write_value(b"\x00", self.options)
        with self.assertRaises(DBusError) as denied_write:
            self.app.control_point.write_value(b"\x00", {"device": Variant("o", "/org/bluez/hci0/dev_NO")})
        self.assertEqual(denied_write.exception.type, "org.bluez.Error.NotAuthorized")
        with self.assertRaises(DBusError) as bredr:
            self.app.report.read_value({"device": Variant("o", "/org/bluez/hci0/dev_OK"), "link": Variant("s", "bredr")})
        self.assertEqual(bredr.exception.type, "org.bluez.Error.NotAuthorized")

    def test_export_failure_unexports_every_successful_item(self):
        failing_bus = FakeBus(("/com/example/hid/service0/char1", gatt.GATT_CHARACTERISTIC))
        app = gatt.HidApplication(failing_bus, "/com/example/hid", self.emit, lambda _path: True)
        with self.assertRaisesRegex(RuntimeError, "export failed"):
            app.export()
        self.assertFalse(failing_bus.exports)
        self.assertFalse(app._exported_items)

    def test_existing_dis_and_battery_can_be_excluded_from_application(self):
        app = gatt.HidApplication(
            self.bus, "/com/example/hid", self.emit, lambda _path: True,
            include_dis=False, include_battery=False,
        )
        paths = {item.path for item in app.objects}
        self.assertIn(app.hid.path, paths)
        self.assertIn(app.report.path, paths)
        self.assertNotIn(app.dis.path, paths)
        self.assertNotIn(app.battery.path, paths)
        app.export()
        self.assertNotIn((app.dis.path, gatt.GATT_SERVICE), self.bus.exports)
        self.assertNotIn((app.battery.path, gatt.GATT_SERVICE), self.bus.exports)
        self.assertEqual(app.report.read_value(self.options), b"\x00")
        app.unexport()

    def test_never_sends_report_and_subscription_is_global(self):
        self.assertFalse(hasattr(self.app, "send_report"))
        self.assertEqual(self.app.report.value, b"\x00")
        self.app.report.StartNotify.__dict__["__DBUS_METHOD"].fn(self.app.report)
        self.assertTrue(self.app.report.notifying)
        self.assertEqual(self.events[-1], ("gatt_subscription", {"attribute": "report", "scope": "global"}))

    def test_advertisement_uses_caller_alias_hid_and_20ms_interval(self):
        ad = gatt.Advertising("Adapter Alias", self.emit)
        self.assertEqual(ad.ServiceUUIDs, [gatt.HID_UUID])
        self.assertEqual(self.app.hid.uuid, ad.ServiceUUIDs[0])
        self.assertEqual(ad.Appearance, 0x03C0)
        self.assertEqual(ad.LocalName, "Adapter Alias")
        self.assertTrue(ad.Discoverable)
        self.assertEqual(ad.MinInterval, 20)
        properties = gatt.HidApplication._properties(ad)
        self.assertEqual(properties["MinInterval"].signature, "u")
        self.assertEqual(properties["MaxInterval"].signature, "u")
        self.assertNotIn("IncludeTxPower", properties)
        ad.export(self.bus, "/com/example/hid/advertisement0")
        ad.unexport()
        self.assertFalse(self.bus.exports)
