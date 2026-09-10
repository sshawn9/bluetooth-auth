import sys
import unittest
from pathlib import Path

from dbus_fast import Variant
from dbus_fast.errors import DBusError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bluetooth_auth_hid.hid import (  # noqa: E402
    ADVERTISEMENT,
    GATT_CHARACTERISTIC,
    HID_UUID,
    OBJECT_MANAGER,
    Advertisement,
    HidApplication,
)


class FakeBus:
    def __init__(self, fail_on=None):
        self.exports = {}
        self.fail_on = fail_on

    def export(self, path, interface):
        if (path, interface.name) == self.fail_on:
            raise RuntimeError("export failed")
        self.exports[path, interface.name] = interface

    def unexport(self, path, interface):
        self.exports.pop((path, interface.name), None)


class HidTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.app = HidApplication(
            self.bus,
            "/com/example/hid",
            lambda path: path == "/org/bluez/hci0/dev_ALLOWED",
        )
        self.options = {
            "device": Variant("o", "/org/bluez/hci0/dev_ALLOWED"),
            "link": Variant("s", "le"),
        }

    def test_exports_hid_hierarchy_and_object_manager(self):
        self.app.export()
        self.assertIn((self.app.root_path, OBJECT_MANAGER), self.bus.exports)
        managed = self.app.managed_objects()
        report = managed[self.app.report.path][GATT_CHARACTERISTIC]
        self.assertEqual(report["Flags"].value, ["read", "encrypt-read", "notify", "encrypt-notify"])
        self.assertNotIn("Value", report)
        self.assertEqual(self.app.report_map.read_value(self.options), self.app.report_map.value)
        self.assertEqual(self.app.report.read_value(self.options), b"\x00")
        self.app.unexport()
        self.assertFalse(self.bus.exports)

    def test_access_requires_allowed_le_device(self):
        with self.assertRaises(DBusError) as denied:
            self.app.report.read_value({"device": Variant("o", "/org/bluez/hci0/dev_DENIED"), "link": Variant("s", "le")})
        self.assertEqual(denied.exception.type, "org.bluez.Error.NotAuthorized")
        with self.assertRaises(DBusError) as bredr:
            self.app.report.read_value({"device": Variant("o", "/org/bluez/hci0/dev_ALLOWED"), "link": Variant("s", "bredr")})
        self.assertEqual(bredr.exception.type, "org.bluez.Error.NotAuthorized")
        with self.assertRaises(DBusError) as malformed:
            self.app.report.read_value({"device": Variant("s", "/org/bluez/hci0/dev_ALLOWED"), "link": Variant("s", "le")})
        self.assertEqual(malformed.exception.type, "org.bluez.Error.NotAuthorized")
        with self.assertRaises(DBusError) as bad_offset:
            self.app.report.read_value({**self.options, "offset": Variant("s", "0")})
        self.assertEqual(bad_offset.exception.type, "org.bluez.Error.InvalidOffset")
        self.app.control_point.write_value(b"\x00", self.options)
        with self.assertRaises(DBusError):
            self.app.control_point.write_value(b"\x02", self.options)

    def test_optional_services_and_export_failure_cleanup(self):
        app = HidApplication(self.bus, "/com/example/hid", lambda _path: True, include_dis=False, include_battery=False)
        self.assertEqual({item.path for item in app.objects}, {
            app.hid.path, app.hid_info.path, app.report_map.path,
            app.control_point.path, app.report.path, app.report_reference.path,
        })
        failing = FakeBus(("/com/example/hid/service0/char1", GATT_CHARACTERISTIC))
        broken = HidApplication(failing, "/com/example/hid", lambda _path: True)
        with self.assertRaisesRegex(RuntimeError, "export failed"):
            broken.export()
        self.assertFalse(failing.exports)
        self.app.export()
        exports = dict(self.bus.exports)
        self.app.export()
        self.assertEqual(self.bus.exports, exports)

    def test_advertisement_uses_hid_alias_and_release_callback(self):
        released = []
        ad = Advertisement("Adapter Alias", lambda: released.append(True))
        self.assertEqual(ad.ServiceUUIDs, [HID_UUID])
        self.assertEqual(ad.Appearance, 0x03C0)
        self.assertEqual(ad.MinInterval, 20)
        self.assertEqual(ad.MaxInterval, 20)
        ad.export(self.bus, "/com/example/hid/ad0")
        self.assertIn(("/com/example/hid/ad0", ADVERTISEMENT), self.bus.exports)
        ad.Release.__dict__["__DBUS_METHOD"].fn(ad)
        self.assertEqual(released, [True])
        with self.assertRaisesRegex(RuntimeError, "已导出"):
            ad.export(self.bus, "/com/example/hid/ad1")
        ad.unexport()
        self.assertFalse(self.bus.exports)


if __name__ == "__main__":
    unittest.main()
