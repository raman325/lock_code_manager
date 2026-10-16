"""
Local Akuvox lock provider.

Akuvox door controllers manage access codes as *users* identified by an
internal device ID, not by numeric slot numbers. This provider bridges
that gap by tagging user names with a slot prefix in the format
``lcm:<slot>:<friendly name>`` (older releases wrote ``[LCM:<slot>]``,
which is still read). A user without the tag is not Lock Code Manager's:
it is never read as a slot, renamed, changed or deleted.

All operations go through the Home Assistant ``local_akuvox`` integration
services (``list_users``, ``add_user``, ``modify_user``, ``delete_user``).
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, override

from ..model.credentials import (
    Credential,
    CredentialRef,
    SlotCredential,
    User,
    WriteResult,
    user_from_slot,
)
from ..model.exceptions import LockCodeManagerProviderError
from ._base import BaseLock
from ._util import make_tagged_name as _make_tagged_name, parse_full_tag
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
    numbers by embedding a ``lcm:<slot>:`` tag in each user's name.

    PINs are readable from the device, so occupied slots report the actual
    PIN value. Cleared slots report ``SlotCredential.empty()``.
    """

    @property
    @override
    def domain(self) -> str:
        """Return integration domain."""
        return AKUVOX_DOMAIN

    @property
    @override
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False

    @property
    @override
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
        # A response with no usable user list is a shape this does not
        # understand, not a device with no users. Returning an empty list
        # would say "every slot is free", which is the answer that gets a
        # real credential overwritten. The type is checked as well as the
        # key: a wrongly typed value would otherwise surface as a bare
        # TypeError from the caller's loop, past every handler that knows
        # what to do with this integration's own errors.
        users = entity_response.get("users")
        if not isinstance(users, list):
            raise LockCodeManagerProviderError(
                f"Malformed list_users entity response from {entity_id}: "
                f"expected a list of users, got {type(users).__name__}"
            )
        return users

    async def _async_add_user(self, name: str, pin: str, code_slot: int) -> None:
        """Add a new user with the given name and PIN; a refusal is ``code_slot``'s."""
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
            code_slot=code_slot,
        )

    async def _async_modify_user(
        self,
        device_user_id: str,
        *,
        name: str | None = None,
        pin: str | None = None,
        code_slot: int | None = None,
    ) -> None:
        """Modify an existing user; a refusal is ``code_slot``'s, when one is given."""
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
            code_slot=code_slot,
        )

    async def _async_delete_user(self, device_user_id: str, code_slot: int) -> None:
        """Delete a user by their device-internal ID; a refusal is ``code_slot``'s."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            AKUVOX_DOMAIN,
            "delete_user",
            service_data={"id": device_user_id},
            target={"entity_id": entity_id},
            code_slot=code_slot,
        )

    @override
    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Return all codes; the device keeps no cache, so every read is fresh."""
        return await self.async_get_usercodes()

    @override
    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Return users by reading tagged local users from the Akuvox device.

        Users bearing a ``lcm:<slot>:`` (or legacy ``[LCM:<slot>]``) tag in their name are mapped to the
        embedded slot number; a user without the tag is not one of them.
        Only users whose slot numbers fall within the managed set are returned.
        Personal Identification Numbers are readable on Akuvox, so occupied
        slots report the actual value.
        """
        managed_slots = self.managed_slots if slots is None else set(slots)
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
            slot_num, _ = parse_full_tag(name)

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

    @override
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
                parsed_slot, friendly = parse_full_tag(user_name)
                if parsed_slot == code_slot:
                    existing_device_id = str(user.get("id", ""))
                    existing_friendly_name = friendly
                    break

            effective_name = name or existing_friendly_name
            tagged_name = _make_tagged_name(code_slot, effective_name)

            if existing_device_id:
                await self._async_modify_user(
                    existing_device_id, name=tagged_name, pin=pin, code_slot=code_slot
                )
            else:
                await self._async_add_user(tagged_name, pin, code_slot)

        LOGGER.debug(
            "Lock %s: set Personal Identification Number credential on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return WriteResult.CONFIRMED

    @override
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
                parsed_slot, _ = parse_full_tag(user.get("name", ""))
                if parsed_slot == code_slot:
                    target_device_id = str(user.get("id", ""))
                    break

            if not target_device_id:
                return False

            await self._async_delete_user(target_device_id, code_slot)
        LOGGER.debug(
            "Lock %s: deleted Personal Identification Number credential on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True
