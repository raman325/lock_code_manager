"""
Local Akuvox lock provider.

Akuvox door controllers manage access codes as *users* identified by an
internal device ID, not by numeric slot numbers. This provider bridges
that gap by tagging user names with a slot prefix in the format
``[LCM:<slot>] <friendly name>``. Pre-existing users discovered on the
device are automatically tagged and assigned to the next available slot
number.

All operations go through the Home Assistant ``local_akuvox`` integration
services (``list_users``, ``add_user``, ``modify_user``, ``delete_user``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from homeassistant.config_entries import ConfigEntry

from ..domain.credentials import (
    Credential,
    CredentialRef,
    User,
    WriteResult,
    user_from_slot,
)
from ..domain.exceptions import (
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
)
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import make_tagged_name as _make_tagged_name, parse_tag as _parse_tag
from .const import LOGGER

AKUVOX_DOMAIN = "local_akuvox"

# Default schedule/relay values for Lock Code Manager-managed users.
# These are required by the Akuvox add_user service but are not
# meaningful for the code-slot abstraction.
_DEFAULT_SCHEDULE_IDS = "1001"  # "Always" schedule on Akuvox devices
_DEFAULT_LIFT_FLOOR_NUM = "1"

# Akuvox firmware varies in how it marks local versus cloud users:
#   A08S / E18C: source_type "1" = local, "2" = cloud, user_type "0" for both
#   X916:        source_type None for all, user_type "-1" = local, "0" = cloud
_LOCAL_SOURCE_TYPE = "1"
_LOCAL_USER_TYPE = "-1"


def _is_local_user(user: dict[str, Any]) -> bool:
    """Return True if *user* was created locally on the device."""
    source_type = user.get("source_type")
    if source_type:
        return str(source_type) == _LOCAL_SOURCE_TYPE
    # source_type absent -- fall back to user_type (X916 pattern)
    return str(user.get("user_type", "")) == _LOCAL_USER_TYPE


@dataclass(repr=False, eq=False)
class AkuvoxLock(BaseLock):
    """
    Local Akuvox lock provider implementation.

    Users on Akuvox controllers are identified by a device-internal ID
    and a name, not by slot numbers. This provider assigns virtual slot
    numbers by embedding a ``[LCM:<slot>]`` tag in each user's name.

    PINs are readable from the device, so occupied slots report the actual
    PIN value. Cleared slots report ``SlotCredential.empty()``.
    """

    # Guards the initial auto-tag pass: skips re-tagging on reconnects so a
    # drifted device list cannot produce double-tag / rename storms. Reset
    # on full reload when the provider instance is recreated.
    _tagged_once: bool = field(default=False, init=False)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return AKUVOX_DOMAIN

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=2)

    async def _async_list_users(self) -> list[dict[str, Any]]:
        """
        Call ``local_akuvox.list_users`` and return the user list.

        Returns a list of user dicts with keys: id, name, user_id,
        private_pin, card_code, schedule_relay, lift_floor_num, etc.
        """
        entity_id = self.lock.entity_id
        response = await self.async_call_service(
            AKUVOX_DOMAIN,
            "list_users",
            service_data={},
            target={"entity_id": entity_id},
            return_response=True,
        )

        if not isinstance(response, dict):
            raise LockCodeManagerProviderError(
                f"Malformed list_users response from {entity_id}: "
                f"expected dict, got {type(response).__name__}"
            )

        # Platform entity services wrap the response per entity_id.
        entity_response = response.get(entity_id, response)
        if not isinstance(entity_response, dict):
            raise LockCodeManagerProviderError(
                f"Malformed list_users entity response from {entity_id}: "
                f"expected dict, got {type(entity_response).__name__}"
            )
        return entity_response.get("users", [])

    async def _async_add_user(self, name: str, pin: str) -> None:
        """Add a new user with the given name and PIN."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            AKUVOX_DOMAIN,
            "add_user",
            service_data={
                "name": name,
                "private_pin": pin,
                "schedules": _DEFAULT_SCHEDULE_IDS,
                "lift_floor_num": _DEFAULT_LIFT_FLOOR_NUM,
            },
            target={"entity_id": entity_id},
        )

    async def _async_modify_user(
        self,
        device_user_id: str,
        *,
        name: str | None = None,
        pin: str | None = None,
    ) -> None:
        """Modify an existing user."""
        entity_id = self.lock.entity_id
        service_data: dict[str, Any] = {"id": device_user_id}
        if name is not None:
            service_data["name"] = name
        if pin is not None:
            service_data["private_pin"] = pin
        await self.async_call_service(
            AKUVOX_DOMAIN,
            "modify_user",
            service_data=service_data,
            target={"entity_id": entity_id},
        )

    async def _async_delete_user(self, device_user_id: str) -> None:
        """Delete a user by their device-internal ID."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            AKUVOX_DOMAIN,
            "delete_user",
            service_data={"id": device_user_id},
            target={"entity_id": entity_id},
        )

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """
        Set up lock by tagging any pre-existing unmanaged users.

        Idempotent: the tag pass runs only once per provider instance to
        prevent double-tag / rename storms on reconnect (the device list
        may have drifted since the previous load).
        """
        await super().async_setup(config_entry)
        if self._tagged_once:
            return
        await self._async_tag_unmanaged_users()
        self._tagged_once = True

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """
        Re-tag unmanaged users, then return all codes.

        Hard refresh is an explicit drift-detection request, so the
        setup-time idempotency gate does not apply.
        """
        managed_slots = self.managed_slots
        if managed_slots:
            async with self._serialize_sequence():
                await self._async_run_tag_pass(managed_slots)
        return await self.async_get_usercodes()

    async def _async_tag_unmanaged_users(self) -> None:
        """
        Discover untagged local users and tag them with a slot number.

        Untagged local users that have a PIN are assigned to the next
        available managed slot and their names are updated on the device
        to include the ``[LCM:<slot>]`` tag via ``modify_user``. The
        list/modify sequence runs under the sequence lock so concurrent
        set/clear/hard-refresh callers do not interleave their own
        multi-step writes.
        """
        managed_slots = self.managed_slots
        if not managed_slots:
            return

        async with self._serialize_sequence():
            await self._async_run_tag_pass(managed_slots)

    async def _async_run_tag_pass(self, managed_slots: set[int]) -> None:
        """
        Body of the tag pass; caller holds the sequence lock.

        Per-user ``LockDisconnected`` is logged and the loop continues so
        any remaining users still get a chance to tag, but the disconnect
        is re-raised at the end. ``async_setup`` then leaves
        ``_tagged_once`` False so the reconnect path retries once the lock
        is reachable. ``LockOperationFailed`` is a per-user condition and
        does not gate idempotency.
        """
        users = await self._async_list_users()

        assigned_slots: set[int] = set()
        # (device_user_id, pin, original_name) for untagged users needing assignment
        untagged: list[tuple[str, str, str]] = []

        for user in users:
            if not _is_local_user(user):
                continue
            name = user.get("name", "")
            pin = user.get("private_pin", "")
            device_id = str(user.get("id", ""))
            slot_num, _ = _parse_tag(name)

            if slot_num is not None:
                assigned_slots.add(slot_num)
            elif pin:
                untagged.append((device_id, pin, name))

        available = sorted(managed_slots - assigned_slots)
        first_disconnect: LockDisconnected | None = None
        for device_id, _pin, original_name in untagged:
            if not available:
                LOGGER.debug(
                    "Lock %s: no managed slot available for untagged user '%s'; "
                    "leaving untouched",
                    self.lock.entity_id,
                    original_name,
                )
                break

            slot_num = available[0]
            tagged_name = _make_tagged_name(slot_num, original_name)
            try:
                await self._async_modify_user(device_id, name=tagged_name)
            except (LockDisconnected, LockOperationFailed) as err:
                LOGGER.error(
                    "Lock %s: failed to tag user '%s' for slot %d: %s",
                    self.lock.entity_id,
                    original_name,
                    slot_num,
                    err,
                )
                if first_disconnect is None and isinstance(err, LockDisconnected):
                    first_disconnect = err
                continue

            available.pop(0)
            LOGGER.debug(
                "Lock %s: tagged user '%s' (id=%s) as slot %d: '%s'",
                self.lock.entity_id,
                original_name,
                device_id,
                slot_num,
                tagged_name,
            )

        if first_disconnect is not None:
            raise LockDisconnected(
                f"Lock {self.lock.entity_id}: disconnect during tag pass; "
                "will be retried on reconnect"
            ) from first_disconnect

    async def async_get_users(self) -> list[User]:
        """
        Return users by reading tagged local users from the Akuvox device.

        Users bearing a ``[LCM:<slot>]`` tag in their name are mapped to the
        embedded slot number. Only reads and classifies; auto-tagging of
        unmanaged users is handled separately by ``_async_tag_unmanaged_users()``.
        Only users whose slot numbers fall within the managed set are returned.
        Personal Identification Numbers are readable on Akuvox, so occupied
        slots report the actual value.
        """
        managed_slots = self.managed_slots
        if not managed_slots:
            return []

        users = await self._async_list_users()

        slot_states: dict[int, SlotCredential] = dict.fromkeys(
            managed_slots, SlotCredential.empty()
        )

        for user in users:
            if not _is_local_user(user):
                continue
            name = user.get("name", "")
            pin = user.get("private_pin", "")
            slot_num, _ = _parse_tag(name)

            if slot_num is not None and slot_num in managed_slots:
                slot_states[slot_num] = (
                    SlotCredential.known(pin) if pin else SlotCredential.empty()
                )

        LOGGER.debug(
            "Lock %s: %s managed slots, %s occupied",
            self.lock.entity_id,
            len(managed_slots),
            sum(1 for v in slot_states.values() if v.is_present),
        )
        return [user_from_slot(slot, state) for slot, state in slot_states.items()]

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set a Personal Identification Number credential on a slot.

        If a user already exists for the given slot, the Personal
        Identification Number (and optionally the name) is updated via
        ``modify_user``. Otherwise a new user is created via ``add_user``.

        Returns True unconditionally because the Akuvox API does not
        indicate whether the value actually changed. The list/modify
        (or list/add) sequence runs under the sequence lock so concurrent
        callers cannot interleave their own multi-step writes against the
        same slot. Ignores ``user_id``; slot-only providers address the
        credential by slot.
        """
        code_slot = credential.slot
        async with self._serialize_sequence():
            users = await self._async_list_users()

            existing_device_id: str | None = None
            existing_friendly_name: str | None = None
            for user in users:
                if not _is_local_user(user):
                    continue
                user_name = user.get("name", "")
                parsed_slot, friendly = _parse_tag(user_name)
                if parsed_slot == code_slot:
                    existing_device_id = str(user.get("id", ""))
                    existing_friendly_name = friendly
                    break

            effective_name = name or existing_friendly_name
            tagged_name = _make_tagged_name(code_slot, effective_name)

            if existing_device_id:
                await self._async_modify_user(
                    existing_device_id, name=tagged_name, pin=pin
                )
            else:
                await self._async_add_user(tagged_name, pin)

        LOGGER.debug(
            "Lock %s: set Personal Identification Number credential on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return WriteResult.CONFIRMED

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ``ref``; return whether it changed.

        Returns True if a user was deleted, False if the slot was already
        empty. The list/delete sequence runs under the sequence lock for
        the same reason ``async_set_credential`` does.
        """
        code_slot = ref.slot
        async with self._serialize_sequence():
            users = await self._async_list_users()
            target_device_id: str | None = None
            for user in users:
                if not _is_local_user(user):
                    continue
                parsed_slot, _ = _parse_tag(user.get("name", ""))
                if parsed_slot == code_slot:
                    target_device_id = str(user.get("id", ""))
                    break

            if not target_device_id:
                return False

            await self._async_delete_user(target_device_id)
        LOGGER.debug(
            "Lock %s: deleted Personal Identification Number credential on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True
