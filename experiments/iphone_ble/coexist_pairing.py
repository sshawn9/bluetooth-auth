"""Narrow BlueZ Agent1 implementation for explicit target-phone repair.

The object is intentionally inert until a caller exports it and registers it
with AgentManager1.  It neither opens D-Bus nor changes pairing state itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from dbus_fast.errors import DBusError
from dbus_fast.service import ServiceInterface, dbus_method


AGENT = "org.bluez.Agent1"
HID_UUIDS = frozenset(("1812", "00001812-0000-1000-8000-00805f9b34fb"))


def _rejected(reason: str) -> DBusError:
    return DBusError("org.bluez.Error.Rejected", reason)


class PairingAgent(ServiceInterface):
    """DisplayYesNo agent that permits one target Numeric Comparison only."""

    def __init__(
        self,
        allow_device: Callable[[str], Awaitable[bool]],
        confirm: Callable[[int], Awaitable[bool]],
        emit: Callable[[str, dict], None],
    ):
        super().__init__(AGENT)
        self._allow_device = allow_device
        self._confirm = confirm
        self._emit = emit
        self.accepted = False
        self.confirmed_device: str | None = None
        self._confirmation_requested = False
        self._pending_task: asyncio.Task[object] | None = None

    async def _target(self, device: str) -> bool:
        try:
            return isinstance(device, str) and await self._allow_device(device)
        except Exception:
            return False

    def _emit_rejected(self, method: str, device: str | None = None) -> None:
        payload = {"method": method}
        if device is not None:
            payload["device"] = device
        self._emit("pairing_agent_rejected", payload)

    async def request_confirmation(self, device: str, passkey: int) -> None:
        if not await self._target(device):
            self._emit_rejected("RequestConfirmation", device)
            raise _rejected("only the selected phone may repair pairing")
        if not isinstance(passkey, int) or isinstance(passkey, bool) or not 0 <= passkey <= 999999:
            self._emit_rejected("RequestConfirmation", device)
            raise _rejected("invalid Numeric Comparison value")
        if self._confirmation_requested or self.confirmed_device is not None or self.accepted:
            self._emit_rejected("RequestConfirmation", device)
            raise _rejected("a pairing confirmation was already decided")
        if self._pending_task is not None and not self._pending_task.done():
            self._emit_rejected("RequestConfirmation", device)
            raise _rejected("another pairing confirmation is pending")

        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("RequestConfirmation requires an asyncio task")
        self._confirmation_requested = True
        self._pending_task = task
        self._emit("pairing_confirmation_requested", {"device": device, "number": passkey})
        try:
            approved = await self._confirm(passkey)
        except asyncio.CancelledError:
            self._emit("pairing_confirmation_cancelled", {"device": device})
            raise DBusError("org.bluez.Error.Canceled", "pairing confirmation cancelled")
        except Exception:
            self._emit_rejected("RequestConfirmation", device)
            raise _rejected("pairing confirmation failed") from None
        finally:
            if self._pending_task is task:
                self._pending_task = None

        if approved is not True:
            self._emit_rejected("RequestConfirmation", device)
            raise _rejected("Numeric Comparison was not approved")
        self.accepted = True
        self.confirmed_device = device
        self._emit("pairing_confirmation_accepted", {"device": device, "number": passkey})

    async def authorize_service(self, device: str, uuid: str) -> None:
        if not await self._target(device) or not isinstance(uuid, str) or uuid.lower() not in HID_UUIDS:
            self._emit_rejected("AuthorizeService", device)
            raise _rejected("only the selected phone HID service is allowed")
        self._emit("pairing_hid_service_authorized", {"device": device, "uuid": uuid.lower()})

    def cancel_pending(self, source: str) -> None:
        task = self._pending_task
        if task is not None and not task.done():
            task.cancel()
        self._emit("pairing_agent_cancel", {"source": source})

    @dbus_method()
    def Release(self):
        self.cancel_pending("Release")

    @dbus_method()
    async def RequestPinCode(self, device: "o") -> "s":
        self._emit_rejected("RequestPinCode", device)
        raise _rejected("PIN pairing is disabled")

    @dbus_method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        del pincode
        self._emit_rejected("DisplayPinCode", device)
        raise _rejected("PIN pairing is disabled")

    @dbus_method()
    async def RequestPasskey(self, device: "o") -> "u":
        self._emit_rejected("RequestPasskey", device)
        raise _rejected("passkey entry is disabled")

    @dbus_method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        del passkey, entered
        self._emit_rejected("DisplayPasskey", device)
        raise _rejected("only Numeric Comparison is allowed")

    @dbus_method()
    async def RequestConfirmation(self, device: "o", passkey: "u"):
        await self.request_confirmation(device, passkey)

    @dbus_method()
    def RequestAuthorization(self, device: "o"):
        self._emit_rejected("RequestAuthorization", device)
        raise _rejected("Just Works authorization is disabled")

    @dbus_method()
    async def AuthorizeService(self, device: "o", uuid: "s"):
        await self.authorize_service(device, uuid)

    @dbus_method()
    def Cancel(self):
        self.cancel_pending("Cancel")
