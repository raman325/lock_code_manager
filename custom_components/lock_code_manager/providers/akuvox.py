"""Local Akuvox lock provider.

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

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError

from ..data import get_managed_slots
from ..exceptions import LockCodeManagerProviderError, LockDisconnected
from ..models import SlotCode
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
    """Local Akuvox lock provider implementation.

    Users on Akuvox controllers are identified by a device-internal ID
    and a name, not by slot numbers. This provider assigns virtual slot
    numbers by embedding a ``[LCM:<slot>]`` tag in each user's name.

    PINs are readable from the device, so occupied slots report the actual
    PIN value. Cleared slots report SlotCode.EMPTY.
    """

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
        """Call ``local_akuvox.list_users`` and return the user list.

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
        try:
            await self.hass.services.async_call(
                AKUVOX_DOMAIN,
                "add_user",
                service_data={
                    "name": name,
                    "private_pin": pin,
                    "schedules": _DEFAULT_SCHEDULE_IDS,
                    "lift_floor_num": _DEFAULT_LIFT_FLOOR_NUM,
                },
                target={"entity_id": entity_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise LockDisconnected(f"Failed to add user on {entity_id}: {err}") from err

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
        try:
            await self.hass.services.async_call(
                AKUVOX_DOMAIN,
                "modify_user",
                service_data=service_data,
                target={"entity_id": entity_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Failed to modify user {device_user_id} on {entity_id}: {err}"
            ) from err

    async def _async_delete_user(self, device_user_id: str) -> None:
        """Delete a user by their device-internal ID."""
        entity_id = self.lock.entity_id
        try:
            await self.hass.services.async_call(
                AKUVOX_DOMAIN,
                "delete_user",
                service_data={"id": device_user_id},
                target={"entity_id": entity_id},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Failed to delete user {device_user_id} on {entity_id}: {err}"
            ) from err

    def _get_managed_slots(self) -> set[int]:
        """Return managed slot numbers for this lock."""
        return get_managed_slots(self.hass, self.lock.entity_id)

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock by tagging any pre-existing unmanaged users."""
        await super().async_setup(config_entry)
        await self._async_tag_unmanaged_users()

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Re-tag unmanaged users, then return all codes."""
        await self._async_tag_unmanaged_users()
        return await self.async_get_usercodes()

    async def _async_tag_unmanaged_users(self) -> None:
        """Discover untagged local users and tag them with a slot number.

        Untagged local users that have a PIN are assigned to the next
        available managed slot and their names are updated on the device
        to include the ``[LCM:<slot>]`` tag via ``modify_user``.
        """
        managed_slots = self._get_managed_slots()
        if not managed_slots:
            return

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
            except LockDisconnected:
                LOGGER.error(
                    "Lock %s: failed to tag user '%s' for slot %d",
                    self.lock.entity_id,
                    original_name,
                    slot_num,
                )
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

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes.

        Users already bearing a ``[LCM:<slot>]`` tag in their name are
        mapped to the embedded slot number. Only reads and classifies;
        auto-tagging of unmanaged users is handled separately by
        ``_async_tag_unmanaged_users()``.

        Only codes whose slot numbers fall within the managed set are returned.
        """
        managed_slots = self._get_managed_slots()
        if not managed_slots:
            return {}

        users = await self._async_list_users()

        # Start with all managed slots empty
        result: dict[int, str | SlotCode] = {
            slot: SlotCode.EMPTY for slot in managed_slots
        }

        for user in users:
            if not _is_local_user(user):
                continue
            name = user.get("name", "")
            pin = user.get("private_pin", "")
            slot_num, _ = _parse_tag(name)

            if slot_num is not None and slot_num in managed_slots:
                result[slot_num] = pin if pin else SlotCode.EMPTY

        LOGGER.debug(
            "Lock %s: %s managed slots, %s occupied",
            self.lock.entity_id,
            len(managed_slots),
            sum(1 for v in result.values() if v is not SlotCode.EMPTY),
        )
        return result

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """Set user code on a virtual slot.

        If a user already exists for the given slot, the PIN (and
        optionally the name) is updated via ``modify_user``. Otherwise
        a new user is created via ``add_user``.

        Returns True unconditionally because the Akuvox API does not
        indicate whether the value actually changed.
        """
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
                existing_device_id, name=tagged_name, pin=usercode
            )
        else:
            await self._async_add_user(tagged_name, usercode)

        LOGGER.debug(
            "Lock %s: set usercode on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear user code from a virtual slot by deleting the user.

        Returns True if a user was deleted, False if the slot was already empty.
        """
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
            "Lock %s: cleared usercode from slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True
