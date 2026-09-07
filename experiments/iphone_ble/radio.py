"""Isolated Bumble radio probe used by the iPhone BLE experiment.

The module deliberately does not use BlueZ or D-Bus.  The caller must take the
controller down before :func:`run_probe` is called and restore it afterwards.
"""

from __future__ import annotations

import asyncio
import collections
import ctypes
import json
import logging
import os
import socket
import struct
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bumble import hci, pairing
from bumble.core import AdvertisingData, PhysicalTransport
from bumble.device import AdvertisingType, Device, DeviceConfiguration, Peer
from bumble.gatt import (
    Attribute,
    Characteristic,
    Descriptor,
    GATT_ANCS_SERVICE,
    GATT_BATTERY_LEVEL_CHARACTERISTIC,
    GATT_BATTERY_SERVICE,
    GATT_CURRENT_TIME_CHARACTERISTIC,
    GATT_CURRENT_TIME_SERVICE,
    GATT_DEVICE_INFORMATION_SERVICE,
    GATT_HID_CONTROL_POINT_CHARACTERISTIC,
    GATT_HID_INFORMATION_CHARACTERISTIC,
    GATT_HUMAN_INTERFACE_DEVICE_SERVICE as GATT_HID_SERVICE,
    GATT_MANUFACTURER_NAME_STRING_CHARACTERISTIC,
    GATT_MODEL_NUMBER_STRING_CHARACTERISTIC,
    GATT_PNP_ID_CHARACTERISTIC,
    GATT_REPORT_CHARACTERISTIC,
    GATT_REPORT_MAP_CHARACTERISTIC,
    GATT_REPORT_REFERENCE_DESCRIPTOR,
    Service,
)
from bumble.profiles.ancs import AncsClient
from bumble.profiles.gap import GenericAccessService
from bumble.transport.common import ParserSource, Transport


_AF_BLUETOOTH = 31
_BTPROTO_HCI = 1
_HCI_CHANNEL_USER = 1
_MODES = frozenset(("ancs", "cts", "hid"))
_REPORT_TYPE_INPUT = 1
_DISCOVERY_INTERVAL_MS = 20
_CONNECTION_PROGRESS_INTERVAL = 10
_GENERIC_HID_APPEARANCE = 0x03C0

# Consumer Control, report ID 1, eight one-bit controls.  The initial report is
# all zeroes and this module never sends a report notification.
_CONSUMER_CONTROL_REPORT_MAP = bytes.fromhex(
    "050C0901A10185011500250175019508"
    "09CD09B509B609B709E909EA09E20940"
    "8102C0"
)


@dataclass(frozen=True)
class ProbeOptions:
    initial_timeout: float = 120
    reconnect_timeout: float = 30
    hold_seconds: float = 30
    cycles: int = 3
    enroll: bool = False


class _EnrollmentPairingDelegate(pairing.PairingDelegate):
    """Allow pairing only for the explicitly selected enrollment connection.

    The distributed-key mask intentionally excludes LINK_KEY, which prevents
    LE-to-BR/EDR CTKD in addition to Classic being disabled in the config.
    """

    def __init__(
        self,
        connection: Any,
        enrollment_connection: Callable[[], Any | None],
        confirm_pairing: Callable[[int, int], Awaitable[bool]],
    ):
        key_distribution = (
            self.KeyDistribution.DISTRIBUTE_ENCRYPTION_KEY
            | self.KeyDistribution.DISTRIBUTE_IDENTITY_KEY
        )
        super().__init__(
            io_capability=self.IoCapability.DISPLAY_OUTPUT_AND_YES_NO_INPUT,
            local_initiator_key_distribution=key_distribution,
            local_responder_key_distribution=key_distribution,
        )
        self.connection = connection
        self._enrollment_connection = enrollment_connection
        self._confirm_pairing = confirm_pairing

    def _allowed(self) -> bool:
        return self._enrollment_connection() is self.connection

    async def accept(self) -> bool:
        return self._allowed()

    async def confirm(self, auto: bool = False) -> bool:
        # An automatic/Just Works confirmation would bind whichever nearby
        # central connected first, so enrollment requires Numeric Comparison.
        del auto
        return False

    async def compare_numbers(self, number: int, digits: int) -> bool:
        if not self._allowed():
            return False
        return await self._confirm_pairing(number, digits)


class _HciSocketSource(ParserSource):
    def __init__(self, hci_socket: socket.socket):
        super().__init__()
        self.socket = hci_socket
        self._closed = False
        asyncio.get_running_loop().add_reader(
            self.socket.fileno(), self._receive_available
        )

    def _receive_available(self) -> None:
        while True:
            try:
                packet = self.socket.recv(4096)
                if not packet:
                    self.on_transport_lost()
                    self.close()
                    return
                self.parser.feed_data(packet)
            except BlockingIOError:
                return
            except OSError:
                self.on_transport_lost()
                self.close()
                return

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            asyncio.get_running_loop().remove_reader(self.socket.fileno())


class _HciSocketSink:
    def __init__(self, hci_socket: socket.socket):
        self.socket = hci_socket
        self.packets: collections.deque[bytes] = collections.deque()
        self.writer_added = False
        self._closed = False

    def on_packet(self, packet: bytes) -> None:
        self.packets.append(packet)
        self._send_available()

    def _send_available(self) -> None:
        while self.packets:
            packet = self.packets[0]
            try:
                written = self.socket.send(packet)
            except BlockingIOError:
                written = 0
            if written != len(packet):
                break
            self.packets.popleft()

        loop = asyncio.get_running_loop()
        if self.packets and not self.writer_added:
            loop.add_writer(self.socket.fileno(), self._send_available)
            self.writer_added = True
        elif not self.packets and self.writer_added:
            loop.remove_writer(self.socket.fileno())
            self.writer_added = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self.writer_added:
                asyncio.get_running_loop().remove_writer(self.socket.fileno())
                self.writer_added = False


class _HciUserTransport(Transport):
    def __init__(self, hci_socket: socket.socket):
        super().__init__(_HciSocketSource(hci_socket), _HciSocketSink(hci_socket))
        self.socket = hci_socket
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        try:
            self.source.close()
        finally:
            try:
                self.sink.close()
            finally:
                self.socket.close()
                self._closed = True


async def _open_hci_user_transport(controller_index: int) -> Transport:
    """Open a Linux HCI user channel without patching ``socket`` constants."""

    if controller_index < 0:
        raise ValueError("controller_index must be non-negative")

    hci_socket = socket.socket(
        _AF_BLUETOOTH,
        socket.SOCK_RAW | socket.SOCK_NONBLOCK,
        _BTPROTO_HCI,
    )
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.bind.argtypes = (
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_int,
        )
        libc.bind.restype = ctypes.c_int
        address = struct.pack(
            "<HHH", _AF_BLUETOOTH, controller_index, _HCI_CHANNEL_USER
        )
        if libc.bind(
            hci_socket.fileno(),
            ctypes.create_string_buffer(address),
            len(address),
        ):
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number))
        return _HciUserTransport(hci_socket)
    except BaseException:
        hci_socket.close()
        raise


def _validate_options(options: ProbeOptions) -> None:
    if options.initial_timeout <= 0 or options.reconnect_timeout <= 0:
        raise ValueError("connection timeouts must be positive")
    if options.hold_seconds < 0:
        raise ValueError("hold_seconds must be non-negative")
    if options.cycles < 0:
        raise ValueError("cycles must be non-negative")


def _load_and_validate_config(config_path: Path) -> DeviceConfiguration:
    with config_path.open(encoding="utf-8") as config_file:
        raw_config = json.load(config_file)
    if not isinstance(raw_config, dict):
        raise ValueError("device config must be a JSON object")

    required = ("name", "address", "irk", "keystore", "identity_address_type")
    missing = [key for key in required if key not in raw_config]
    if missing:
        raise ValueError(f"device config is missing: {', '.join(missing)}")
    if raw_config["identity_address_type"] != int(
        pairing.PairingConfig.AddressType.RANDOM
    ):
        raise ValueError("identity_address_type must explicitly be RANDOM (1)")
    if raw_config.get("classic_enabled") is not False:
        raise ValueError("classic_enabled must explicitly be false")
    if raw_config.get("classic_smp_enabled") is not False:
        raise ValueError("classic_smp_enabled must explicitly be false")
    if raw_config.get("le_enabled", True) is not True:
        raise ValueError("le_enabled must be true")
    if raw_config.get("le_privacy_enabled", False) is not False:
        raise ValueError("this probe requires a stable random-static address")
    if raw_config.get("address_generation_offload", False) is not False:
        raise ValueError("controller address generation must be disabled")
    if raw_config.get("smp_debug_mode", False) is not False:
        raise ValueError("SMP debug mode must be disabled")
    if raw_config.get("gatt_services"):
        raise ValueError("GATT services must be selected by mode, not device config")

    address = hci.Address(raw_config["address"])
    if not address.is_static:
        raise ValueError("address must be a valid random-static LE address")
    try:
        irk = bytes.fromhex(raw_config["irk"])
    except (TypeError, ValueError) as error:
        raise ValueError("irk must be 16 bytes of hexadecimal") from error
    if len(irk) != 16 or not any(irk):
        raise ValueError("irk must be a non-zero 16-byte value")

    keystore = raw_config["keystore"]
    if not isinstance(keystore, str) or not keystore.startswith("JsonKeyStore:"):
        raise ValueError("keystore must specify an explicit JsonKeyStore path")
    key_path = keystore.split(":", 1)[1]
    if not key_path or not Path(key_path).is_absolute():
        raise ValueError("JsonKeyStore path must be absolute")

    name = raw_config["name"]
    if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 29:
        raise ValueError("name must occupy between 1 and 29 UTF-8 bytes")

    return DeviceConfiguration.from_dict(raw_config)


def _advertising_payloads(mode: str, name: str) -> tuple[bytes, bytes]:
    flags = (AdvertisingData.FLAGS, b"\x06")
    if mode == "ancs":
        service = (
            AdvertisingData.LIST_OF_128_BIT_SERVICE_SOLICITATION_UUIDS,
            GATT_ANCS_SERVICE.to_pdu_bytes(),
        )
    elif mode == "cts":
        service = (
            AdvertisingData.LIST_OF_16_BIT_SERVICE_SOLICITATION_UUIDS,
            GATT_CURRENT_TIME_SERVICE.to_pdu_bytes(),
        )
    elif mode == "hid":
        service = (
            AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
            GATT_HID_SERVICE.to_pdu_bytes(),
        )
    else:
        raise ValueError(f"unsupported mode: {mode}")

    structures = [flags, service]
    if mode == "hid":
        structures.append(
            (AdvertisingData.APPEARANCE, struct.pack("<H", _GENERIC_HID_APPEARANCE))
        )
    advertising = bytes(AdvertisingData(structures))
    name_data = bytes(
        AdvertisingData(((AdvertisingData.COMPLETE_LOCAL_NAME, name.encode()),))
    )
    # Put the complete, distinguishable name in ADV_IND whenever it fits so
    # finding it does not require an active scan. The fixed HID/CTS names fit;
    # ANCS's 128-bit solicitation leaves too little room for its complete name.
    if len(advertising) + len(name_data) <= 31:
        advertising += name_data
        scan_response = b""
    else:
        scan_response = name_data
    if len(advertising) > 31 or len(scan_response) > 31:
        raise ValueError("advertising payload exceeds the legacy 31-byte limit")
    return advertising, scan_response


def _safe_error(error: Exception, phase: str) -> dict[str, Any]:
    details: dict[str, Any] = {"phase": phase, "type": type(error).__name__}
    if isinstance(error, (TimeoutError, OSError, RuntimeError)):
        details["reason"] = str(error)[:200]
    error_code = getattr(error, "error_code", None)
    if error_code is not None:
        try:
            details["protocol_error_code"] = int(error_code)
        except (TypeError, ValueError):
            details["protocol_error_code"] = str(error_code)[:40]
    return details


def _build_hogp_services() -> tuple[list[Service], Characteristic]:
    encrypted_read = Attribute.READABLE | Attribute.READ_REQUIRES_ENCRYPTION
    encrypted_write = Attribute.WRITEABLE | Attribute.WRITE_REQUIRES_ENCRYPTION
    input_report_permissions = encrypted_read

    report = Characteristic(
        GATT_REPORT_CHARACTERISTIC,
        Characteristic.READ | Characteristic.NOTIFY,
        input_report_permissions,
        b"\x00",
        descriptors=(
            Descriptor(
                GATT_REPORT_REFERENCE_DESCRIPTOR,
                encrypted_read,
                bytes((1, _REPORT_TYPE_INPUT)),
            ),
        ),
    )
    hid_service = Service(
        GATT_HID_SERVICE,
        (
            Characteristic(
                GATT_HID_INFORMATION_CHARACTERISTIC,
                Characteristic.READ,
                encrypted_read,
                b"\x11\x01\x00\x02",
            ),
            Characteristic(
                GATT_REPORT_MAP_CHARACTERISTIC,
                Characteristic.READ,
                encrypted_read,
                _CONSUMER_CONTROL_REPORT_MAP,
            ),
            Characteristic(
                GATT_HID_CONTROL_POINT_CHARACTERISTIC,
                Characteristic.WRITE_WITHOUT_RESPONSE,
                encrypted_write,
                b"\x00",
            ),
            report,
        ),
    )
    battery_service = Service(
        GATT_BATTERY_SERVICE,
        (
            Characteristic(
                GATT_BATTERY_LEVEL_CHARACTERISTIC,
                Characteristic.READ | Characteristic.NOTIFY,
                encrypted_read,
                b"\x64",
            ),
        ),
    )
    information_service = Service(
        GATT_DEVICE_INFORMATION_SERVICE,
        (
            Characteristic(
                GATT_MANUFACTURER_NAME_STRING_CHARACTERISTIC,
                Characteristic.READ,
                Attribute.READABLE,
                b"Bumble BLE Lab",
            ),
            Characteristic(
                GATT_MODEL_NUMBER_STRING_CHARACTERISTIC,
                Characteristic.READ,
                Attribute.READABLE,
                b"Passive Consumer Control Probe",
            ),
            # Report Hosts require PnP ID discovery.  Zero is deliberately
            # unassigned for this experiment; it does not claim a vendor ID.
            Characteristic(
                GATT_PNP_ID_CHARACTERISTIC,
                Characteristic.READ,
                Attribute.READABLE,
                struct.pack("<BHHH", 0x02, 0x0000, 0x0001, 0x0001),
            ),
        ),
    )
    return [hid_service, battery_service, information_service], report


async def _wait_event(
    emitter: Any, event: str, timeout: float, predicate: Callable[..., bool] | None = None
) -> tuple[Any, ...]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[Any, ...]] = loop.create_future()

    def listener(*args: Any) -> None:
        if (predicate is None or predicate(*args)) and not future.done():
            future.set_result(args)

    emitter.on(event, listener)
    try:
        return await asyncio.wait_for(future, timeout)
    finally:
        emitter.remove_listener(event, listener)


def _connection_is_active(connection: Any) -> bool:
    device = getattr(connection, "device", None)
    connections = getattr(device, "connections", None)
    if connections is None:
        return not getattr(connection, "disconnected", False)
    return any(candidate is connection for candidate in connections.values())


async def _while_connected(
    connection: Any, awaitable: Awaitable[Any], timeout: float
) -> Any:
    if not _connection_is_active(connection):
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise RuntimeError("connection ended before the operation started")

    operation = asyncio.ensure_future(awaitable)
    disconnected = asyncio.create_task(
        _wait_event(connection, connection.EVENT_DISCONNECTION, timeout)
    )
    try:
        done, _ = await asyncio.wait(
            (operation, disconnected),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnected in done:
            raise RuntimeError("connection ended during the operation")
        if operation in done:
            result = operation.result()
            if not _connection_is_active(connection):
                raise RuntimeError("connection ended as the operation completed")
            return result
        raise TimeoutError("operation timed out")
    finally:
        for task in (operation, disconnected):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, disconnected, return_exceptions=True)


async def _while_transport_open(
    transport: Transport, awaitable: Awaitable[Any], timeout: float
) -> Any:
    terminated = transport.source.terminated
    if terminated.done():
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        raise RuntimeError("HCI transport ended before the operation started")

    operation = asyncio.ensure_future(awaitable)
    lost = asyncio.ensure_future(asyncio.shield(terminated))
    try:
        done, _ = await asyncio.wait(
            (operation, lost), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if lost in done:
            raise RuntimeError("HCI transport was lost")
        if operation in done:
            result = operation.result()
            if terminated.done():
                raise RuntimeError("HCI transport ended as the operation completed")
            return result
        raise TimeoutError("operation timed out")
    finally:
        for task in (operation, lost):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, lost, return_exceptions=True)


async def _wait_connection(device: Device, timeout: float) -> Any:
    if connections := [
        connection
        for connection in device.connections.values()
        if _connection_is_active(connection)
    ]:
        return connections[0]
    (connection,) = await _wait_event(
        device,
        Device.EVENT_CONNECTION,
        timeout,
        lambda candidate: candidate.transport == PhysicalTransport.LE
        and candidate.role == hci.Role.PERIPHERAL,
    )
    return connection


async def _wait_connection_with_progress(
    device: Device, transport: Transport, timeout: float,
    emit: Callable[[str, dict], None], cycle: int, name: str, enroll: bool,
) -> Any:
    started = time.monotonic()
    emit("waiting_for_connection", {
        "cycle": cycle, "name": name, "timeout_seconds": timeout,
        "message": (f"首次配对：请在 iPhone 设置 → 蓝牙中选择 {name}。若列表没有此项，本轮尚未发现设备。"
                    if enroll else "正在等待已配对的 iPhone 自动连回；重连测试期间不要操作手机。"),
    })
    operation = asyncio.create_task(
        _while_transport_open(transport, _wait_connection(device, timeout), timeout)
    )
    try:
        while True:
            done, _ = await asyncio.wait((operation,), timeout=_CONNECTION_PROGRESS_INTERVAL)
            if operation in done:
                return operation.result()
            elapsed = time.monotonic() - started
            emit("waiting_for_connection", {
                "cycle": cycle, "name": name,
                "elapsed_seconds": round(elapsed, 1),
                "remaining_seconds": round(max(0, timeout - elapsed), 1),
                "message": "仍未收到 iPhone 的连接；超时后会停止广播并恢复适配器。",
            })
    except TimeoutError as error:
        raise TimeoutError(f"{timeout:g} 秒内未收到连接：{name}；没有进入配对或服务使用阶段") from error
    finally:
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def _bond_for_connection(device: Device, connection: Any) -> bool:
    if device.keystore is None:
        return False
    return await device.keystore.get(str(connection.peer_address)) is not None


async def _wait_bond_saved(
    device: Device, connection: Any, timeout: float
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _connection_is_active(connection):
            raise RuntimeError("connection ended before the bond was saved")
        if await _bond_for_connection(device, connection):
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("pairing keys were not saved before the deadline")


async def _wait_encrypted(connection: Any, timeout: float) -> None:
    if connection.is_encrypted:
        return
    await _while_connected(
        connection,
        _wait_event(
            connection,
            connection.EVENT_CONNECTION_ENCRYPTION_CHANGE,
            timeout,
            lambda *_: connection.is_encrypted,
        ),
        timeout,
    )


async def _prepare_ancs(peer: Peer, emit: Callable[[str, dict], None]) -> AncsClient:
    client = await AncsClient.for_peer(peer)
    if client is None:
        raise RuntimeError("ANCS service unavailable")
    client.on(AncsClient.EVENT_NOTIFICATION, lambda _: emit("ancs_notification", {}))
    await client.start()
    return client


async def _prepare_cts(peer: Peer) -> dict[str, Any]:
    services = await peer.discover_service(GATT_CURRENT_TIME_SERVICE)
    if not services:
        raise RuntimeError("Current Time Service unavailable")
    characteristics = await peer.discover_characteristics(
        (GATT_CURRENT_TIME_CHARACTERISTIC,), services[0]
    )
    if not characteristics:
        raise RuntimeError("Current Time characteristic unavailable")
    value = await characteristics[0].read_value()
    if len(value) != 10:
        raise RuntimeError("Current Time value has an invalid length")
    year = int.from_bytes(value[0:2], "little")
    fields = (year, *value[2:7])
    if not (
        1582 <= fields[0] <= 9999
        and 1 <= fields[1] <= 12
        and 1 <= fields[2] <= 31
        and fields[3] <= 23
        and fields[4] <= 59
        and fields[5] <= 59
    ):
        raise RuntimeError("Current Time value is out of range")
    return {"current_time_valid": True}


async def _prepare_hogp(
    report: Characteristic, connection: Any, timeout: float
) -> dict[str, Any]:
    subscription = connection.gatt_server.subscribers.get(connection, {}).get(
        report.handle, b"\0\0"
    )
    if subscription[0] & 1:
        return {"report_subscribed": True, "reports_sent": 0}
    await _wait_event(
        report,
        report.EVENT_SUBSCRIPTION,
        timeout,
        lambda bearer, notify, _indicate: bearer is connection and bool(notify),
    )
    return {"report_subscribed": True, "reports_sent": 0}


async def _hold_connection(connection: Any, seconds: float) -> None:
    if not _connection_is_active(connection):
        raise RuntimeError("connection ended before the hold started")
    if seconds == 0:
        return
    try:
        await _wait_event(connection, connection.EVENT_DISCONNECTION, seconds)
    except TimeoutError:
        return
    raise RuntimeError("connection dropped during hold")


async def _terminal_confirm_pairing(
    number: int,
    digits: int,
    timeout: float,
    emit: Callable[[str, dict], None],
) -> bool:
    emit(
        "pairing_confirmation_required",
        {"number": f"{number:0{digits}d}", "timeout_seconds": timeout},
    )
    if not sys.stdin.isatty():
        return False

    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()
    file_descriptor = sys.stdin.fileno()

    def read_answer() -> None:
        if not answer.done():
            answer.set_result(sys.stdin.readline())

    print(
        f"Confirm Bluetooth pairing number {number:0{digits}d}? [y/N] ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    loop.add_reader(file_descriptor, read_answer)
    try:
        response = await asyncio.wait_for(answer, timeout)
        return response.strip().casefold() in ("y", "yes")
    except TimeoutError:
        return False
    finally:
        loop.remove_reader(file_descriptor)


async def _run_probe(
    config_path: Path,
    mode: str,
    controller_index: int,
    options: ProbeOptions,
    emit: Callable[[str, dict], None],
    transport_factory: Callable[[int], Awaitable[Transport]],
    confirm_pairing: Callable[[int, int], Awaitable[bool]],
) -> dict:
    if mode not in _MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(_MODES))}")
    _validate_options(options)
    config = _load_and_validate_config(config_path)
    advertising_data, scan_response_data = _advertising_payloads(mode, config.name)
    config.advertising_data = advertising_data
    config.scan_response_data = scan_response_data

    result: dict[str, Any] = {
        "mode": mode,
        "identity": str(config.address),
        "requested_reconnects": options.cycles,
        "cycles": [],
        "success": False,
        "passed": False,
    }
    transport: Transport | None = None
    device: Device | None = None
    powered_on = False
    report: Characteristic | None = None
    enrollment_connection: Any | None = None
    enrollment_selector: Callable[[Any], None] | None = None
    previous_logging_disable = logging.root.manager.disable
    phase = "open_transport"

    try:
        # Bumble's DEBUG traces include raw HCI/ATT/SMP payloads.  Suppress them
        # while the probe owns the radio so keys and notification bodies cannot
        # leak through a caller's verbose logging configuration.
        logging.disable(logging.DEBUG)
        transport = await asyncio.wait_for(transport_factory(controller_index), 5)
        phase = "power_on"
        if mode == "hid":
            config.gap_service_enabled = False
        device = Device.from_config_with_hci(config, transport.source, transport.sink)
        if mode == "hid":
            device.add_service(GenericAccessService(config.name, _GENERIC_HID_APPEARANCE))
            services, report = _build_hogp_services()
            for service in services:
                device.add_service(service)

        def pairing_config_for(connection: Any) -> pairing.PairingConfig:
            return pairing.PairingConfig(
                sc=True,
                mitm=True,
                bonding=True,
                identity_address_type=pairing.PairingConfig.AddressType.RANDOM,
                delegate=_EnrollmentPairingDelegate(
                    connection,
                    lambda: enrollment_connection,
                    confirm_pairing,
                ),
            )

        device.pairing_config_factory = pairing_config_for

        await _while_transport_open(transport, device.power_on(), 10)
        powered_on = True
        emit("radio_ready", {"mode": mode, "controller_index": controller_index})

        existing_keys = (
            await _while_transport_open(transport, device.keystore.get_all(), 3)
            if device.keystore
            else []
        )
        enrollment_available = options.enroll and not existing_keys

        def select_enrollment_connection(candidate: Any) -> None:
            nonlocal enrollment_available, enrollment_connection
            if (
                enrollment_available
                and enrollment_connection is None
                and candidate.transport == PhysicalTransport.LE
                and candidate.role == hci.Role.PERIPHERAL
            ):
                enrollment_connection = candidate
                enrollment_available = False

        enrollment_selector = select_enrollment_connection
        device.on(Device.EVENT_CONNECTION, enrollment_selector)

        for cycle_number in range(1, options.cycles + 2):
            cycle: dict[str, Any] = {
                "cycle": cycle_number,
                "kind": "initial" if cycle_number == 1 else "reconnect",
                "reconnect": cycle_number - 1,
                "link": False,
                "encryption": False,
                "service_ready": False,
                "hold": False,
            }
            result["cycles"].append(cycle)
            started = time.monotonic()
            phase = "link" if cycle_number == 1 else "reconnect"
            await _while_transport_open(
                transport,
                device.start_advertising(
                    advertising_type=AdvertisingType.UNDIRECTED_CONNECTABLE_SCANNABLE,
                    own_address_type=hci.OwnAddressType.RANDOM,
                    auto_restart=False,
                    advertising_interval_min=_DISCOVERY_INTERVAL_MS,
                    advertising_interval_max=_DISCOVERY_INTERVAL_MS,
                ),
                5,
            )
            emit("advertising", {
                "cycle": cycle_number, "mode": mode, "name": config.name,
                "interval_ms": _DISCOVERY_INTERVAL_MS,
                "name_location": "scan_response" if scan_response_data else "advertising",
                "advertising_data_hex": advertising_data.hex(),
                "scan_response_data_hex": scan_response_data.hex(),
                "packet_type": "legacy_connectable_scannable",
                "controller_api": "extended" if getattr(device, "supports_le_extended_advertising", False) else "legacy",
                "message": "控制器已接受广播命令；尚未收到手机连接。",
            })
            timeout = (
                options.initial_timeout if cycle_number == 1 else options.reconnect_timeout
            )
            connection = await _wait_connection_with_progress(
                device, transport, timeout, emit, cycle_number, config.name,
                options.enroll and cycle_number == 1,
            )
            cycle["link"] = True
            cycle["link_seconds"] = round(time.monotonic() - started, 3)
            emit("link", {"cycle": cycle_number})

            known_peer = await _while_transport_open(
                transport, _bond_for_connection(device, connection), 3
            )
            enrolling = not known_peer
            if not known_peer and enrollment_connection is not connection:
                await _while_transport_open(transport, connection.disconnect(), 5)
                raise RuntimeError("incoming peer is not present in this probe's keystore")
            if not known_peer:
                connection.request_pairing()
            elif not connection.is_encrypted:
                # As a Peripheral this sends an SMP Security Request.  A bonded
                # Central should answer by enabling encryption with the stored
                # LTK; the delegate above still rejects any fresh pairing.
                emit("security_request", {"cycle": cycle_number})
                connection.request_pairing()

            phase = "encryption"
            await _while_transport_open(
                transport, _wait_encrypted(connection, timeout), timeout
            )
            enrollment_connection = None
            cycle["encryption"] = True
            emit("encryption", {"cycle": cycle_number})
            if enrolling:
                phase = "encryption"
                await _while_transport_open(
                    transport,
                    _while_connected(
                        connection,
                        _wait_bond_saved(device, connection, timeout),
                        timeout,
                    ),
                    timeout,
                )
                cycle["bond_saved"] = True
                emit("bond_saved", {"cycle": cycle_number})

            ready_started = time.monotonic()
            if mode == "ancs":
                phase = "ancs_subscription"
                profile = await _while_transport_open(
                    transport,
                    _while_connected(
                        connection, _prepare_ancs(Peer(connection), emit), timeout
                    ),
                    timeout,
                )
                ready_details = {"subscriptions": 2}
            elif mode == "cts":
                phase = "cts_read"
                profile = None
                ready_details = await _while_transport_open(
                    transport,
                    _while_connected(
                        connection, _prepare_cts(Peer(connection)), timeout
                    ),
                    timeout,
                )
            else:
                phase = "hid_subscription"
                profile = None
                assert report is not None
                ready_details = await _while_transport_open(
                    transport,
                    _while_connected(
                        connection,
                        _prepare_hogp(report, connection, timeout),
                        timeout,
                    ),
                    timeout,
                )
            cycle.update(ready_details)
            cycle["service_ready"] = True
            cycle["service_ready_seconds"] = round(time.monotonic() - ready_started, 3)
            emit("service_ready", {"cycle": cycle_number, "mode": mode})

            phase = "hold"
            await _while_transport_open(
                transport,
                _hold_connection(connection, options.hold_seconds),
                max(options.hold_seconds + 1, 1),
            )
            cycle["hold"] = True
            emit("hold_complete", {"cycle": cycle_number})

            if profile is not None:
                try:
                    await _while_transport_open(transport, profile.stop(), 3)
                except Exception:
                    pass
            if cycle_number <= options.cycles:
                phase = "reconnect"
                emit("cycle_disconnect", {"cycle": cycle_number})
                await _while_transport_open(transport, connection.disconnect(), 5)

        result["success"] = True
        result["passed"] = True
        return result
    except asyncio.CancelledError:
        raise
    except Exception as error:
        result["error"] = _safe_error(error, phase)
        emit("probe_error", result["error"])
        return result
    finally:
        cleanup_failures: list[str] = []
        if device is not None and enrollment_selector is not None:
            try:
                device.remove_listener(Device.EVENT_CONNECTION, enrollment_selector)
            except (KeyError, ValueError):
                pass
        if device is not None and powered_on:
            try:
                await asyncio.wait_for(device.stop_advertising(), 3)
            except BaseException as error:
                cleanup_failures.append(f"stop_advertising:{type(error).__name__}")
            try:
                await asyncio.wait_for(device.power_off(), 3)
            except BaseException as error:
                cleanup_failures.append(f"power_off:{type(error).__name__}")
        transport_closed = transport is None
        if transport is not None:
            try:
                close_task = asyncio.create_task(transport.close())
                await asyncio.wait_for(asyncio.shield(close_task), 3)
                transport_closed = True
            except BaseException as error:
                cleanup_failures.append(f"transport_close:{type(error).__name__}")
                raw_socket = getattr(transport, "socket", None)
                if raw_socket is not None:
                    try:
                        raw_socket.close()
                        transport_closed = True
                    except Exception:
                        transport_closed = False
        if cleanup_failures:
            result["cleanup_failures"] = cleanup_failures
            result["success"] = False
            result["passed"] = False
        logging.disable(previous_logging_disable)
        if transport is not None and transport_closed:
            emit("radio_closed", {"controller_index": controller_index})
        elif transport is not None:
            emit("radio_close_failed", {"controller_index": controller_index})


async def run_probe(
    config_path: Path,
    mode: str,
    controller_index: int,
    options: ProbeOptions,
    emit: Callable[[str, dict], None],
) -> dict:
    """Run a bounded peripheral-only probe and always release the user channel."""

    async def confirm_pairing(number: int, digits: int) -> bool:
        return await _terminal_confirm_pairing(
            number,
            digits,
            min(options.initial_timeout, 60),
            emit,
        )

    return await _run_probe(
        config_path,
        mode,
        controller_index,
        options,
        emit,
        _open_hci_user_transport,
        confirm_pairing,
    )
