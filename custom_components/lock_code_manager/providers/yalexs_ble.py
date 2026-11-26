"""
Yale Access Bluetooth (``yalexs_ble``) lock provider.

All lock operations go through the Yale Access Bluetooth integration's keypad
credential actions (home-assistant/core#185596):

* ``yalexs_ble.set_lock_credential`` (``credential_type``, ``credential_data``,
  ``credential_index``, ``name``)
* ``yalexs_ble.clear_lock_credential`` (``credential_type``, ``credential_index``)
* ``yalexs_ble.get_lock_credential_status`` -> ``credential_exists``, ``name``

PINs are write-only through those actions (the status never returns the PIN),
so occupied slots report ``SlotCredential.unreadable()`` and empty slots
``SlotCredential.empty()``, as for Schlage.

A code's use comes from the ``yalexs_ble_lock_activity`` bus event the
integration fires when the lock's activity log shows a keypad unlock
(``source: pin`` with the ``slot``).
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CALLBACK_TYPE, Event, callback

from ..model.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SlotCredential,
    User,
    WriteResult,
    user_from_slot,
)
from ..model.exceptions import LockCodeManagerProviderError
from ._base import BaseLock
from .const import LOGGER

YALEXS_BLE_DOMAIN = "yalexs_ble"
EVENT_LOCK_ACTIVITY = "yalexs_ble_lock_activity"
CREDENTIAL_TYPE_PIN = "pin"
SOURCE_PIN = "pin"


@dataclass(repr=False, eq=False)
class YaleXSBLELock(BaseLock):
    """Yale Access Bluetooth lock provider (write-only PINs, slot events)."""

    _activity_unsub: CALLBACK_TYPE | None = field(default=None, init=False)

    # One Bluetooth keycode exchange per slot. yalexs-ble retries an operation
    # up to DEFAULT_ATTEMPTS (4) times, each a connect (bleak-retry-connector's
    # own attempts, up to ~30 s through a proxy) plus up to 3 writes that each
    # wait RESPONSE_TIMEOUT (10 s) -- about 4 minutes in the worst case. This
    # sits above that, so the library's own timeouts always claim a silent
    # slot first.
    per_exchange_budget: ClassVar[float | None] = 300.0

    @property
    @override
    def domain(self) -> str:
        """Return integration domain."""
        return YALEXS_BLE_DOMAIN

    @property
    @override
    def supports_code_slot_events(self) -> bool:
        """The activity log names the keypad slot that unlocked the lock."""
        return True

    @property
    @override
    def usercode_scan_interval(self) -> timedelta:
        """Each slot read is one Bluetooth round trip; keep the scan modest."""
        return timedelta(minutes=15)

    @override
    async def async_get_capabilities(self) -> LockCapabilities:
        """PIN only, slots 1-250 and 4-8 digits, as the yalexs_ble actions validate."""
        return LockCapabilities(
            supports_user_management=False,
            max_users=0,
            credential_types={
                CredentialType.PIN: CredentialTypeCapability(
                    num_slots=250,
                    min_length=4,
                    max_length=8,
                    supports_learn=False,
                )
            },
        )

    @override
    async def async_is_device_available(self) -> bool:
        """Available while the lock entity is; reading slots to find out costs a round trip each."""
        state = self.hass.states.get(self.lock.entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    async def _async_slot_exists(self, slot: int) -> bool:
        """Return whether a keypad slot holds a PIN."""
        entity_id = self.lock.entity_id
        response = await self.async_call_service(
            YALEXS_BLE_DOMAIN,
            "get_lock_credential_status",
            service_data={
                "entity_id": entity_id,
                "credential_type": CREDENTIAL_TYPE_PIN,
                "credential_index": slot,
            },
            return_response=True,
        )
        if not isinstance(response, dict):
            raise LockCodeManagerProviderError(
                f"yalexs_ble get_lock_credential_status returned {type(response).__name__}"
                f" for {entity_id}"
            )
        entity_response: Any = response.get(entity_id, response)
        if (
            not isinstance(entity_response, dict)
            or "credential_exists" not in entity_response
        ):
            raise LockCodeManagerProviderError(
                f"yalexs_ble get_lock_credential_status malformed for {entity_id}:"
                f" {entity_response!r}"
            )
        return bool(entity_response["credential_exists"])

    @override
    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """Read each managed slot's occupancy (PINs themselves are not readable)."""
        managed_slots = self.managed_slots if slots is None else set(slots)
        users: list[User] = []
        for slot in sorted(managed_slots):
            state = (
                SlotCredential.unreadable()
                if await self._async_slot_exists(slot)
                else SlotCredential.empty()
            )
            users.append(user_from_slot(slot, state))
        return users

    @override
    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
    ) -> WriteResult:
        """Program a PIN into the slot (the lock clears the slot first)."""
        code_slot = credential.slot
        data: dict[str, Any] = {
            "entity_id": self.lock.entity_id,
            "credential_type": CREDENTIAL_TYPE_PIN,
            "credential_index": code_slot,
            "credential_data": pin,
        }
        if name:
            data["name"] = name
        async with self._serialize_sequence():
            if not name:
                # set_lock_credential stores a slot name only when one is
                # given, so without one the previous holder's name would stay
                # on the slot (in changed_by and the logbook). Clearing first
                # drops it; the set clears the slot on the lock anyway.
                await self.async_call_service(
                    YALEXS_BLE_DOMAIN,
                    "clear_lock_credential",
                    service_data={
                        "entity_id": self.lock.entity_id,
                        "credential_type": CREDENTIAL_TYPE_PIN,
                        "credential_index": code_slot,
                    },
                )
            await self.async_call_service(
                YALEXS_BLE_DOMAIN,
                "set_lock_credential",
                service_data=data,
                code_slot=code_slot,
            )
        LOGGER.debug("Lock %s: set PIN on slot %s", self.lock.entity_id, code_slot)
        return WriteResult.CONFIRMED

    @override
    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """Clear the slot; return whether it held a PIN."""
        code_slot = ref.slot
        async with self._serialize_sequence():
            if not await self._async_slot_exists(code_slot):
                return False
            await self.async_call_service(
                YALEXS_BLE_DOMAIN,
                "clear_lock_credential",
                service_data={
                    "entity_id": self.lock.entity_id,
                    "credential_type": CREDENTIAL_TYPE_PIN,
                    "credential_index": code_slot,
                },
            )
        LOGGER.debug("Lock %s: cleared slot %s", self.lock.entity_id, code_slot)
        return True

    @override
    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Every read goes to the lock, so a hard refresh is a normal read of these slots."""
        return await self.async_get_usercodes(self.managed_slots | set(slots or ()))

    @callback
    def _async_on_activity(self, event: Event) -> None:
        data = event.data
        # The integration fires the event for its main lock entity, also when
        # this provider manages the secure-mode lock entity of the same lock,
        # so match on the device; the entity id is the fallback without one.
        if self.lock.device_id is not None:
            if data.get("device_id") != self.lock.device_id:
                return
        elif data.get("entity_id") != self.lock.entity_id:
            return
        if data.get("source") != SOURCE_PIN or data.get("master_code"):
            return
        slot = data.get("slot")
        if slot is None:  # the keypad's lock button: no code was used
            return
        # A code entered on the keypad unlocks the lock.
        self.async_fire_code_slot_event(
            int(slot), to_locked=False, credential_type=CredentialType.PIN
        )

    @override
    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Listen for keypad uses (idempotent)."""
        if self._activity_unsub is None:
            self._activity_unsub = self.hass.bus.async_listen(
                EVENT_LOCK_ACTIVITY, self._async_on_activity
            )

    @override
    async def async_unload(self, remove_permanently: bool) -> None:
        """Stop listening, then the base teardown."""
        if self._activity_unsub is not None:
            self._activity_unsub()
            self._activity_unsub = None
        await super().async_unload(remove_permanently)
