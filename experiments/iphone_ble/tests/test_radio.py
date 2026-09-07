from __future__ import annotations

import asyncio
import json
import logging
import struct
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

from bumble import hci, pairing
from bumble.controller import Controller
from bumble.core import AdvertisingData
from bumble.device import Device, DeviceConfiguration, Peer
from bumble.gatt import (
    Attribute,
    Characteristic,
    GATT_CURRENT_TIME_CHARACTERISTIC,
    GATT_CURRENT_TIME_SERVICE,
    GATT_DEVICE_INFORMATION_SERVICE,
    GATT_APPEARANCE_CHARACTERISTIC,
    GATT_GENERIC_ACCESS_SERVICE,
    GATT_HID_INFORMATION_CHARACTERISTIC,
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE as GATT_HID_SERVICE,
    GATT_PNP_ID_CHARACTERISTIC,
    GATT_PROTOCOL_MODE_CHARACTERISTIC,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
    GATT_REPORT_REFERENCE_DESCRIPTOR,
    Service,
)
from bumble.link import LocalLink

from experiments.iphone_ble import radio


class _Source:
    def __init__(self):
        self.terminated = asyncio.get_running_loop().create_future()
        self.sink = None

    def set_packet_sink(self, sink):
        self.sink = sink


class _Sink:
    def on_packet(self, _packet):
        pass


class _Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, event, listener):
        self.listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener):
        self.listeners[event].remove(listener)

    def emit(self, event, *args):
        for listener in list(self.listeners.get(event, ())):
            listener(*args)


class _Keys:
    async def get(self, _name):
        return object()

    async def get_all(self):
        return [("known-peer", object())]


class _GattServer:
    def __init__(self):
        self.subscribers = {}


class _Connection(_Emitter):
    EVENT_DISCONNECTION = "disconnection"
    EVENT_CONNECTION_ENCRYPTION_CHANGE = "connection_encryption_change"

    def __init__(self):
        super().__init__()
        self.device = None
        self.disconnected = False
        self.transport = radio.PhysicalTransport.LE
        self.role = hci.Role.PERIPHERAL
        self.peer_address = hci.Address("C1:C2:C3:C4:C5:C6")
        self.is_encrypted = True
        self.gatt_server = _GattServer()
        self.pairing_requests = 0

    def request_pairing(self):
        self.pairing_requests += 1

    async def disconnect(self):
        self.disconnected = True
        if self.device is not None:
            self.device.connections = {
                handle: candidate
                for handle, candidate in self.device.connections.items()
                if candidate is not self
            }
        self.emit(self.EVENT_DISCONNECTION, 0)


class _Device(_Emitter):
    EVENT_CONNECTION = "connection"

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.connections = {}
        self.keystore = _Keys()
        self.services = []
        self.powered_off = False
        self.stop_calls = 0
        self.start_calls = 0
        self.pairing_config_factory = None

    def add_service(self, service):
        self.services.append(service)

    async def power_on(self):
        pass

    async def power_off(self):
        self.powered_off = True

    async def start_advertising(self, **_kwargs):
        self.start_calls += 1
        connection = _Connection()
        connection.device = self
        self.connections = {self.start_calls: connection}
        asyncio.get_running_loop().call_soon(
            self.emit, self.EVENT_CONNECTION, connection
        )

    async def stop_advertising(self):
        self.stop_calls += 1


class _Transport:
    def __init__(self):
        self.source = _Source()
        self.sink = object()
        self.closed = False

    async def close(self):
        self.closed = True


class _ControllerTransport:
    def __init__(self, controller):
        self.source = controller
        self.sink = controller
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def fileno(self):
        return 42

    def close(self):
        self.closed = True


class _FakeBind:
    def __init__(self):
        self.argtypes = None
        self.restype = None

    def __call__(self, *_args):
        return -1


class _FakeLibc:
    def __init__(self):
        self.bind = _FakeBind()


class _CurrentTimeCharacteristic:
    def __init__(self, value):
        self.value = value

    async def read_value(self):
        return self.value


class _CurrentTimePeer:
    def __init__(self, value):
        self.characteristic = _CurrentTimeCharacteristic(value)

    async def discover_service(self, _uuid):
        return [object()]

    async def discover_characteristics(self, _uuids, _service):
        return [self.characteristic]


class _ConfirmingDelegate(pairing.PairingDelegate):
    async def compare_numbers(self, _number, digits):
        del digits
        return True

    async def confirm(self, auto=False):
        del auto
        return True


def _config(path: Path, key_path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "name": "BLE Probe",
                "address": "F0:F1:F2:F3:F4:F5",
                "irk": "865F81FF5A8B486EAAE29A27AD9F77DC",
                "keystore": f"JsonKeyStore:{key_path}",
                "identity_address_type": 1,
                "classic_enabled": False,
                "classic_smp_enabled": False,
                "le_privacy_enabled": False,
                "address_generation_offload": False,
                "advertising_interval": 20,
            }
        ),
        encoding="utf-8",
    )


class RadioTests(unittest.IsolatedAsyncioTestCase):
    def test_hid_is_passive_consumer_control_with_one_byte_report(self):
        services, report = radio._build_hogp_services()
        hid = next(service for service in services if service.uuid == GATT_HID_SERVICE)
        uuids = {characteristic.uuid for characteristic in hid.characteristics}
        self.assertIn(GATT_HID_INFORMATION_CHARACTERISTIC, uuids)
        self.assertIn(GATT_REPORT_MAP_CHARACTERISTIC, uuids)
        self.assertIn(GATT_REPORT_CHARACTERISTIC, uuids)
        self.assertNotIn(GATT_PROTOCOL_MODE_CHARACTERISTIC, uuids)
        self.assertEqual(report.value, b"\x00")
        reference = report.get_descriptor(GATT_REPORT_REFERENCE_DESCRIPTOR)
        self.assertEqual(reference.value, b"\x01\x01")
        report_map = next(
            characteristic.value
            for characteristic in hid.characteristics
            if characteristic.uuid == GATT_REPORT_MAP_CHARACTERISTIC
        )
        self.assertIn(bytes.fromhex("050C0901"), report_map)
        self.assertFalse(hasattr(radio, "send_report"))

    async def test_fixed_random_identity_and_no_ctkd(self):
        config = DeviceConfiguration.from_dict(
            {
                "name": "BLE Probe",
                "address": "F0:F1:F2:F3:F4:F5",
                "irk": "865F81FF5A8B486EAAE29A27AD9F77DC",
                "identity_address_type": 1,
                "classic_enabled": False,
                "classic_smp_enabled": False,
            }
        )
        device = Device.from_config_with_hci(config, _Source(), _Sink())
        allowed = True
        async def confirm(_number, _digits):
            return allowed

        connection = object()
        delegate = radio._EnrollmentPairingDelegate(
            connection, lambda: connection, confirm
        )
        pairing_config = pairing.PairingConfig(
            identity_address_type=pairing.PairingConfig.AddressType.RANDOM,
            delegate=delegate,
        )
        self.assertTrue(config.address.is_static)
        self.assertEqual(
            pairing_config.identity_address_type,
            pairing.PairingConfig.AddressType.RANDOM,
        )
        self.assertFalse(config.classic_enabled)
        self.assertFalse(config.classic_smp_enabled)
        link_key = pairing.PairingDelegate.KeyDistribution.DISTRIBUTE_LINK_KEY
        self.assertFalse(delegate.local_initiator_key_distribution & link_key)
        self.assertFalse(delegate.local_responder_key_distribution & link_key)
        self.assertEqual(str(device.static_address), "F0:F1:F2:F3:F4:F5")

        rejected = radio._EnrollmentPairingDelegate(
            connection, lambda: None, confirm
        )
        self.assertFalse(await rejected.accept())
        self.assertFalse(await rejected.compare_numbers(123456, 6))

    def test_each_mode_advertises_only_its_service(self):
        for mode in ("ancs", "cts", "hid"):
            payload, scan_response = radio._advertising_payloads(mode, "BLE Probe")
            structures = AdvertisingData.from_bytes(payload).ad_structures
            service_types = {
                AdvertisingData.LIST_OF_128_BIT_SERVICE_SOLICITATION_UUIDS,
                AdvertisingData.LIST_OF_16_BIT_SERVICE_SOLICITATION_UUIDS,
                AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
            }
            service_structures = [
                entry for entry in structures if entry[0] in service_types
            ]
            self.assertEqual(len(service_structures), 1)
            appearances = [
                value
                for data_type, value in structures
                if data_type == AdvertisingData.APPEARANCE
            ]
            self.assertEqual(
                appearances,
                [struct.pack("<H", radio._GENERIC_HID_APPEARANCE)]
                if mode == "hid"
                else [],
            )
            self.assertLessEqual(len(payload), 31)
            self.assertLessEqual(len(scan_response), 31)

    def test_complete_name_uses_primary_advertisement_when_it_fits(self):
        for mode in ("ancs", "cts", "hid"):
            name = f"BT-Auth-{mode.upper()}"
            payload, scan_response = radio._advertising_payloads(mode, name)
            name_structure = (AdvertisingData.COMPLETE_LOCAL_NAME, name.encode())
            if mode == "ancs":
                self.assertNotIn(name_structure, AdvertisingData.from_bytes(payload).ad_structures)
                self.assertIn(name_structure, AdvertisingData.from_bytes(scan_response).ad_structures)
            else:
                self.assertIn(name_structure, AdvertisingData.from_bytes(payload).ad_structures)
                self.assertEqual(scan_response, b"")
            # A long configured name must remain complete in the scan response,
            # not silently become an ambiguous/truncated test name.
            _, long_response = radio._advertising_payloads(mode, "L" * 29)
            self.assertEqual(len(long_response), 31)

    async def test_cts_readiness_reads_and_validates_current_time(self):
        current_time = bytes.fromhex("EA0709070C2238050000")
        result = await radio._prepare_cts(_CurrentTimePeer(current_time))
        self.assertEqual(result, {"current_time_valid": True})

        with self.assertRaisesRegex(RuntimeError, "invalid length"):
            await radio._prepare_cts(_CurrentTimePeer(b"short"))

    async def test_hid_subscription_must_belong_to_current_connection(self):
        _services, report = radio._build_hogp_services()
        connection = _Connection()
        other_connection = _Connection()
        readiness = asyncio.create_task(
            radio._prepare_hogp(report, connection, timeout=1)
        )
        await asyncio.sleep(0)
        report.emit(report.EVENT_SUBSCRIPTION, other_connection, True, False)
        await asyncio.sleep(0)
        self.assertFalse(readiness.done())
        report.emit(report.EVENT_SUBSCRIPTION, connection, True, False)
        self.assertEqual(
            await readiness,
            {"report_subscribed": True, "reports_sent": 0},
        )

    async def test_wait_helpers_cancel_children_and_transport_loss_fails_hold(self):
        connection = _Connection()
        device = _Device(DeviceConfiguration())
        connection.device = device
        device.connections = {1: connection}
        child_finished = asyncio.Event()

        async def never_finishes():
            try:
                await asyncio.Future()
            finally:
                child_finished.set()

        waiter = asyncio.create_task(
            radio._while_connected(connection, never_finishes(), 10)
        )
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(child_finished.is_set())

        transport = _Transport()
        hold = asyncio.create_task(
            radio._while_transport_open(
                transport, radio._hold_connection(connection, 10), 10
            )
        )
        await asyncio.sleep(0)
        transport.source.terminated.set_result(None)
        with self.assertRaisesRegex(RuntimeError, "transport"):
            await hold

    async def test_hci_user_transport_uses_numeric_linux_constants_and_closes_on_error(self):
        fake_socket = _FakeSocket()
        with (
            mock.patch.object(radio.socket, "socket", return_value=fake_socket) as constructor,
            mock.patch.object(radio.ctypes, "CDLL", return_value=_FakeLibc()),
            mock.patch.object(radio.ctypes, "get_errno", return_value=16),
        ):
            with self.assertRaises(OSError):
                await radio._open_hci_user_transport(4)

        constructor.assert_called_once_with(
            31,
            radio.socket.SOCK_RAW | radio.socket.SOCK_NONBLOCK,
            1,
        )
        self.assertTrue(fake_socket.closed)

    async def test_probe_cycles_close_transport_without_real_radio(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "probe.json"
            _config(config_path, directory_path / "keys.json")
            transport = _Transport()
            fake_device = None

            async def transport_factory(index):
                self.assertEqual(index, 7)
                return transport

            def device_factory(config, _source, _sink):
                nonlocal fake_device
                fake_device = _Device(config)
                return fake_device

            events = []
            with (
                mock.patch.object(
                    radio.Device,
                    "from_config_with_hci",
                    side_effect=device_factory,
                ),
                mock.patch.object(radio, "Peer", side_effect=lambda connection: connection),
                mock.patch.object(
                    radio,
                    "_prepare_cts",
                    return_value={"current_time_valid": True},
                ),
            ):
                result = await radio._run_probe(
                    config_path,
                    "cts",
                    7,
                    radio.ProbeOptions(
                        initial_timeout=1,
                        reconnect_timeout=1,
                        hold_seconds=0,
                        cycles=2,
                    ),
                    lambda event, data: events.append((event, data)),
                    transport_factory,
                    lambda _number, _digits: asyncio.sleep(0, result=True),
                )

            self.assertTrue(result["success"], result)
            self.assertTrue(result["passed"], result)
            self.assertEqual(len(result["cycles"]), 3)
            self.assertTrue(all(cycle["service_ready"] for cycle in result["cycles"]))
            self.assertTrue(transport.closed)
            self.assertTrue(fake_device.powered_off)
            self.assertEqual(fake_device.start_calls, 3)
            self.assertEqual(events[-1][0], "radio_closed")

    async def test_no_phone_connection_reports_progress_times_out_and_closes(self):
        class SilentDevice(_Device):
            async def start_advertising(self, **kwargs):
                self.start_calls += 1
                self.last_advertising_options = kwargs

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "probe.json"
            _config(config_path, Path(directory) / "keys.json")
            transport = _Transport()
            fake_device = SilentDevice(DeviceConfiguration())
            events = []
            with mock.patch.object(radio.Device, "from_config_with_hci", return_value=fake_device), \
                 mock.patch.object(radio, "_CONNECTION_PROGRESS_INTERVAL", 0.01):
                result = await asyncio.wait_for(radio._run_probe(
                    config_path, "ancs", 0,
                    radio.ProbeOptions(initial_timeout=0.05, cycles=0, hold_seconds=0, enroll=True),
                    lambda event, data: events.append((event, data)),
                    lambda _index: asyncio.sleep(0, result=transport),
                    lambda _number, _digits: asyncio.sleep(0, result=True),
                ), 1)
            self.assertFalse(result["passed"])
            self.assertEqual(result["error"]["phase"], "link")
            self.assertEqual(result["error"]["type"], "TimeoutError")
            self.assertIn("未收到连接", result["error"]["reason"])
            waits = [data for event, data in events if event == "waiting_for_connection"]
            self.assertGreaterEqual(len(waits), 2)
            self.assertIn("iPhone", waits[0]["message"])
            self.assertTrue(transport.closed)
            self.assertTrue(fake_device.powered_off)
            self.assertEqual(fake_device.last_advertising_options["advertising_interval_min"], 20)
            self.assertEqual(fake_device.last_advertising_options["advertising_interval_max"], 20)

    async def test_local_link_pair_store_encrypt_cts_and_reconnect(self):
        previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_logging_disable)
        with tempfile.TemporaryDirectory() as directory:
            link = LocalLink()
            peripheral_controller = Controller(
                "peripheral",
                link=link,
                public_address="01:02:03:04:05:06",
            )
            central_controller = Controller(
                "central",
                link=link,
                public_address="11:12:13:14:15:16",
            )

            def device_config(name, address, irk, keys_name):
                return DeviceConfiguration.from_dict(
                    {
                        "name": name,
                        "address": address,
                        "irk": irk,
                        "keystore": f"JsonKeyStore:{directory}/{keys_name}.json",
                        "identity_address_type": 1,
                        "classic_enabled": False,
                        "classic_smp_enabled": False,
                        "advertising_interval": 20,
                    }
                )

            peripheral_config = device_config(
                "probe",
                "F0:F1:F2:F3:F4:F5",
                "00112233445566778899AABBCCDDEEFF",
                "peripheral",
            )
            peripheral_config.gap_service_enabled = False
            peripheral = Device.from_config_with_hci(
                peripheral_config,
                peripheral_controller,
                peripheral_controller,
            )
            peripheral.add_service(
                radio.GenericAccessService(
                    peripheral_config.name, radio._GENERIC_HID_APPEARANCE
                )
            )
            hid_services, server_report = radio._build_hogp_services()
            for service in hid_services:
                peripheral.add_service(service)
            central_config = device_config(
                "phone",
                "E0:E1:E2:E3:E4:E5",
                "102132435465768798A9BACBDCEDFE0F",
                "central",
            )
            central = Device.from_config_with_hci(
                central_config,
                central_controller,
                central_controller,
            )
            central.add_service(
                Service(
                    GATT_CURRENT_TIME_SERVICE,
                    (
                        Characteristic(
                            GATT_CURRENT_TIME_CHARACTERISTIC,
                            Characteristic.READ,
                            Attribute.READABLE,
                            bytes.fromhex("EA0709070C2238050000"),
                        ),
                    ),
                )
            )

            selected = [None]

            async def confirm(_number, _digits):
                return True

            peripheral.pairing_config_factory = lambda connection: pairing.PairingConfig(
                sc=True,
                mitm=True,
                bonding=True,
                identity_address_type=pairing.PairingConfig.AddressType.RANDOM,
                delegate=radio._EnrollmentPairingDelegate(
                    connection, lambda: selected[0], confirm
                ),
            )
            central.pairing_config_factory = lambda _connection: pairing.PairingConfig(
                sc=True,
                mitm=True,
                bonding=True,
                identity_address_type=pairing.PairingConfig.AddressType.RANDOM,
                delegate=_ConfirmingDelegate(
                    io_capability=pairing.PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT
                ),
            )

            await peripheral.power_on()
            await central.power_on()
            try:
                await peripheral.start_advertising()
                central_connection = await asyncio.wait_for(
                    central.connect(peripheral.random_address), 2
                )
                await asyncio.sleep(0)
                peripheral_connection = next(iter(peripheral.connections.values()))
                selected[0] = peripheral_connection
                # Bumble 0.0.234 calls one deprecated internal helper during
                # Secure Connections pairing; it is upstream code, not this API.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    await asyncio.wait_for(central_connection.pair(), 2)
                await asyncio.sleep(0.05)
                self.assertTrue(central_connection.is_encrypted)
                self.assertTrue(peripheral_connection.is_encrypted)
                self.assertTrue(await central.keystore.get_all())
                self.assertTrue(await peripheral.keystore.get_all())
                self.assertEqual(
                    await radio._prepare_cts(Peer(peripheral_connection)),
                    {"current_time_valid": True},
                )

                central_peer = Peer(central_connection)
                hid_service = (await central_peer.discover_service(GATT_HID_SERVICE))[0]
                hid_characteristics = await central_peer.discover_characteristics(
                    (), hid_service
                )
                report_map_proxy = next(
                    characteristic
                    for characteristic in hid_characteristics
                    if characteristic.uuid == GATT_REPORT_MAP_CHARACTERISTIC
                )
                report_proxy = next(
                    characteristic
                    for characteristic in hid_characteristics
                    if characteristic.uuid == GATT_REPORT_CHARACTERISTIC
                )
                self.assertEqual(
                    await report_map_proxy.read_value(),
                    radio._CONSUMER_CONTROL_REPORT_MAP,
                )
                self.assertEqual(await report_proxy.read_value(), b"\x00")
                await report_proxy.subscribe(lambda _value: None)
                self.assertEqual(
                    peripheral.gatt_server.subscribers[peripheral_connection][
                        server_report.handle
                    ],
                    b"\x01\x00",
                )

                dis_service = (
                    await central_peer.discover_service(GATT_DEVICE_INFORMATION_SERVICE)
                )[0]
                dis_characteristics = await central_peer.discover_characteristics(
                    (GATT_PNP_ID_CHARACTERISTIC,), dis_service
                )
                pnp_id = await dis_characteristics[0].read_value()
                self.assertEqual(len(pnp_id), 7)
                self.assertEqual(struct.unpack("<BHHH", pnp_id), (2, 0, 1, 1))

                gap_service = (
                    await central_peer.discover_service(GATT_GENERIC_ACCESS_SERVICE)
                )[0]
                appearance = await central_peer.discover_characteristics(
                    (GATT_APPEARANCE_CHARACTERISTIC,), gap_service
                )
                self.assertEqual(
                    await appearance[0].read_value(),
                    struct.pack("<H", radio._GENERIC_HID_APPEARANCE),
                )

                await central_connection.disconnect()
                await peripheral.power_off()
                await central.power_off()
                link = LocalLink()
                peripheral_controller = Controller(
                    "peripheral-restarted",
                    link=link,
                    public_address="01:02:03:04:05:06",
                )
                central_controller = Controller(
                    "central-restarted",
                    link=link,
                    public_address="11:12:13:14:15:16",
                )
                peripheral = Device.from_config_with_hci(
                    peripheral_config,
                    peripheral_controller,
                    peripheral_controller,
                )
                central = Device.from_config_with_hci(
                    central_config,
                    central_controller,
                    central_controller,
                )
                central.add_service(
                    Service(
                        GATT_CURRENT_TIME_SERVICE,
                        (
                            Characteristic(
                                GATT_CURRENT_TIME_CHARACTERISTIC,
                                Characteristic.READ,
                                Attribute.READABLE,
                                bytes.fromhex("EA0709070C2238050000"),
                            ),
                        ),
                    )
                )
                await peripheral.power_on()
                await central.power_on()
                await peripheral.start_advertising()
                reconnected = await asyncio.wait_for(
                    central.connect(peripheral.random_address), 2
                )
                await asyncio.wait_for(reconnected.encrypt(), 2)
                await asyncio.sleep(0.05)
                restored = next(iter(peripheral.connections.values()))
                self.assertTrue(reconnected.is_encrypted)
                self.assertTrue(restored.is_encrypted)
                self.assertEqual(
                    await radio._prepare_cts(Peer(restored)),
                    {"current_time_valid": True},
                )
                await reconnected.disconnect()
            finally:
                await peripheral.power_off()
                await central.power_off()

    async def test_production_advertising_reaches_scanner_with_expected_fields(self):
        # Exercise the actual HCI serialization and LocalLink advertising path.
        # Connecting to a known address alone does not check discovery at all.
        previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_logging_disable)
        expected_payloads = {
            "ancs": bytes.fromhex("0201061115d0002d121e4b0fa4994eceb531f40579"),
            "cts": bytes.fromhex("020106031405180c09") + b"BT-Auth-CTS",
            "hid": bytes.fromhex("020106030312180319c0030c09") + b"BT-Auth-HID",
        }
        for mode, expected_payload in expected_payloads.items():
            for extended_commands in (False, True):
                with self.subTest(mode=mode, extended_commands=extended_commands):
                    with tempfile.TemporaryDirectory() as directory:
                        path = Path(directory) / "device.json"
                        _config(path, Path(directory) / "keys.json")
                        config = json.loads(path.read_text(encoding="utf-8"))
                        name = f"BT-Auth-{mode.upper()}"
                        config["name"] = name
                        path.write_text(json.dumps(config), encoding="utf-8")
                        link = LocalLink()
                        transmitter = Controller(
                            "transmitter", link=link,
                            public_address="01:02:03:04:05:06",
                        )
                        if extended_commands:
                            transmitter.le_features |= hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
                        else:
                            transmitter.le_features &= ~hci.LeFeatureMask.LE_EXTENDED_ADVERTISING
                        receiver = Controller(
                            "scanner", link=link,
                            public_address="11:12:13:14:15:16",
                        )
                        scanner = Device.from_config_with_hci(
                            DeviceConfiguration.from_dict({
                                "name": "offline-scanner", "address": "E0:E1:E2:E3:E4:E5",
                                "classic_enabled": False,
                            }), receiver, receiver,
                        )
                        received = asyncio.Queue()
                        scanner.on(Device.EVENT_ADVERTISEMENT, received.put_nowait)
                        await scanner.power_on()
                        await scanner.start_scanning(active=False)
                        transport = _ControllerTransport(transmitter)
                        probe = asyncio.create_task(radio._run_probe(
                            path, mode, 0,
                            radio.ProbeOptions(initial_timeout=3, cycles=0),
                            lambda *_: None,
                            lambda _: asyncio.sleep(0, result=transport),
                            lambda *_: asyncio.sleep(0, result=False),
                        ))
                        try:
                            advertisement = await asyncio.wait_for(received.get(), 2)
                            self.assertEqual(bytes(advertisement.data), expected_payload)
                            self.assertEqual(advertisement.address, hci.Address("F0:F1:F2:F3:F4:F5"))
                            self.assertTrue(advertisement.is_connectable)
                            if extended_commands:
                                advertiser = next(iter(transmitter.advertising_sets.values()))
                                parameters = advertiser.parameters
                                self.assertEqual(parameters.advertising_event_properties, 0x13)
                                self.assertEqual(parameters.primary_advertising_interval_min, 32)
                                self.assertEqual(parameters.primary_advertising_interval_max, 32)
                                self.assertEqual(parameters.primary_advertising_channel_map, 7)
                                self.assertEqual(parameters.primary_advertising_phy, hci.Phy.LE_1M)
                                self.assertEqual(parameters.own_address_type, hci.OwnAddressType.RANDOM)
                                self.assertEqual(parameters.advertising_filter_policy, 0)
                                scan_response = bytes(advertiser.scan_response_data)
                            else:
                                advertiser = transmitter.le_legacy_advertiser
                                self.assertEqual(advertiser.advertising_type, 0)
                                self.assertEqual(advertiser.advertising_interval_min, 32)
                                self.assertEqual(advertiser.advertising_interval_max, 32)
                                self.assertEqual(advertiser.advertising_channel_map, 7)
                                self.assertEqual(advertiser.own_address_type, hci.OwnAddressType.RANDOM)
                                self.assertEqual(advertiser.advertising_filter_policy, 0)
                                scan_response = advertiser.scan_response_data
                            # Bumble's simulated controller repeats advertising
                            # data as SCAN_RSP instead of modeling ScanReq/ScanRsp.
                            # Check the name at the receiving HCI boundary; do not
                            # misrepresent LocalLink as verifying active scanning.
                            self.assertEqual(
                                scan_response,
                                bytes((len(name) + 1, 9)) + name.encode() if mode == "ancs" else b"",
                            )
                        finally:
                            probe.cancel()
                            await asyncio.gather(probe, return_exceptions=True)
                            await scanner.stop_scanning()
                            await scanner.power_off()
                        self.assertTrue(transport.closed)

    async def test_production_probe_runs_enrollment_over_local_link(self):
        previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, previous_logging_disable)
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            config_path = directory_path / "probe.json"
            _config(config_path, directory_path / "probe-keys.json")

            link = LocalLink()
            peripheral_controller = Controller(
                "probe-controller",
                link=link,
                public_address="01:02:03:04:05:06",
            )
            central_controller = Controller(
                "phone-controller",
                link=link,
                public_address="11:12:13:14:15:16",
            )
            central = Device.from_config_with_hci(
                DeviceConfiguration.from_dict(
                    {
                        "name": "phone",
                        "address": "E0:E1:E2:E3:E4:E5",
                        "irk": "102132435465768798A9BACBDCEDFE0F",
                        "keystore": f"JsonKeyStore:{directory}/phone-keys.json",
                        "identity_address_type": 1,
                        "classic_enabled": False,
                        "classic_smp_enabled": False,
                    }
                ),
                central_controller,
                central_controller,
            )
            central.add_service(
                Service(
                    GATT_CURRENT_TIME_SERVICE,
                    (
                        Characteristic(
                            GATT_CURRENT_TIME_CHARACTERISTIC,
                            Characteristic.READ,
                            Attribute.READABLE,
                            bytes.fromhex("EA0709070C2238050000"),
                        ),
                    ),
                )
            )
            central.pairing_config_factory = lambda _connection: pairing.PairingConfig(
                sc=True,
                mitm=True,
                bonding=True,
                identity_address_type=pairing.PairingConfig.AddressType.RANDOM,
                delegate=_ConfirmingDelegate(
                    io_capability=pairing.PairingDelegate.DISPLAY_OUTPUT_AND_YES_NO_INPUT
                ),
            )
            await central.power_on()
            transport = _ControllerTransport(peripheral_controller)
            phone_task = None
            confirmed = []
            discovered = asyncio.Queue()
            central.on(Device.EVENT_ADVERTISEMENT, discovered.put_nowait)
            await central.start_scanning(active=False)

            async def phone_connect_and_pair():
                advertisement = await asyncio.wait_for(discovered.get(), 2)
                self.assertIn(
                    (AdvertisingData.LIST_OF_16_BIT_SERVICE_SOLICITATION_UUIDS, b"\x05\x18"),
                    advertisement.data.ad_structures,
                )
                await central.stop_scanning()
                connection = await central.connect(advertisement.address)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    await connection.pair()
                return connection

            def emit(event, _payload):
                nonlocal phone_task
                if event == "advertising" and phone_task is None:
                    phone_task = asyncio.create_task(phone_connect_and_pair())

            async def confirm(number, digits):
                confirmed.append((number, digits))
                return True

            try:
                result = await radio._run_probe(
                    config_path,
                    "cts",
                    0,
                    radio.ProbeOptions(
                        initial_timeout=2,
                        reconnect_timeout=1,
                        hold_seconds=0,
                        cycles=0,
                        enroll=True,
                    ),
                    emit,
                    lambda _index: asyncio.sleep(0, result=transport),
                    confirm,
                )
                self.assertTrue(result["passed"], result)
                self.assertEqual(len(result["cycles"]), 1)
                self.assertTrue(result["cycles"][0]["bond_saved"])
                self.assertTrue(result["cycles"][0]["service_ready"])
                self.assertTrue(result["cycles"][0]["hold"])
                self.assertTrue(transport.closed)
                self.assertTrue(confirmed)
                self.assertIsNotNone(phone_task)
                await asyncio.wait_for(phone_task, 2)
            finally:
                if phone_task is not None and not phone_task.done():
                    phone_task.cancel()
                    await asyncio.gather(phone_task, return_exceptions=True)
                if central.is_scanning:
                    await central.stop_scanning()
                await central.power_off()

    async def test_invalid_config_never_opens_wireless_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "bad.json"
            _config(config_path, Path(directory) / "keys.json")
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["identity_address_type"] = 0
            config_path.write_text(json.dumps(config), encoding="utf-8")
            opened = False

            async def forbidden_factory(_index):
                nonlocal opened
                opened = True
                raise AssertionError("wireless entry was opened")

            with self.assertRaisesRegex(ValueError, "RANDOM"):
                await radio._run_probe(
                    config_path,
                    "cts",
                    0,
                    radio.ProbeOptions(cycles=1),
                    lambda _event, _data: None,
                    forbidden_factory,
                    lambda _number, _digits: asyncio.sleep(0, result=False),
                )
            self.assertFalse(opened)


if __name__ == "__main__":
    unittest.main()
