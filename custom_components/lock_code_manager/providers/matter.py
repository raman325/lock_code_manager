"""Matter lock provider.

Handles PIN credential management via Matter lock services.
PINs are write-only: occupied slots report SlotCode.UNKNOWN, cleared slots report
SlotCode.EMPTY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import get_entry_data
from ..exceptions import LockCodeManagerError, LockDisconnected
from ..models import SlotCode
from ._base import BaseLock

MATTER_DOMAIN = "matter"


async def _async_call_matter_service(
    lock: MatterLock,
    service: str,
    service_data: dict[str, Any],
) -> dict[str, Any]:
    """Call a Matter service and return the per-entity response data.

    Calls the service, validates the response contains data for this lock's
    entity ID, and returns the per-entity dict directly.
    """
    try:
        result = await lock.hass.services.async_call(
            MATTER_DOMAIN,
            service,
            service_data,
            blocking=True,
            return_response=True,
        )
    except (ServiceValidationError, HomeAssistantError) as err:
        raise LockDisconnected(
            f"Matter service {MATTER_DOMAIN}.{service} failed for "
            f"{lock.lock.entity_id}: {err}"
        ) from err
    if not isinstance(result, dict) or lock.lock.entity_id not in result:
        raise LockCodeManagerError(
            f"Matter service {MATTER_DOMAIN}.{service} returned no data for "
            f"{lock.lock.entity_id}"
        )
    return result[lock.lock.entity_id]


@dataclass(repr=False, eq=False)
class MatterLock(BaseLock):
    """Class to represent a Matter lock."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MATTER_DOMAIN

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=5)

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Validate the lock supports Matter user management."""
        lock_info = await _async_call_matter_service(
            self,
            "get_lock_info",
            {"entity_id": self.lock.entity_id},
        )
        if not lock_info.get("supports_user_management"):
            raise LockCodeManagerError(
                f"Matter lock {self.lock.entity_id} does not support user management"
            )

    async def async_is_integration_connected(self) -> bool:
        """Return whether the Matter integration is loaded."""
        if not self.lock_config_entry:
            return False
        return self.lock_config_entry.state == ConfigEntryState.LOADED

    async def async_is_device_available(self) -> bool:
        """Return whether the Matter lock device is available for commands."""
        try:
            await _async_call_matter_service(
                self,
                "get_lock_info",
                {"entity_id": self.lock.entity_id},
            )
        except (LockDisconnected, LockCodeManagerError):
            return False
        return True

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes.

        Matter PINs are write-only, so occupied slots return SlotCode.UNKNOWN.
        """
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }
        if not code_slots:
            return {}

        lock_data = await _async_call_matter_service(
            self,
            "get_lock_users",
            {"entity_id": self.lock.entity_id},
        )
        users: list[dict[str, Any]] = lock_data.get("users", [])

        # Build a set of credential indices that have PIN credentials
        occupied_slots: set[int] = set()
        for user in users:
            for credential in user.get("credentials", []):
                if credential.get("credential_type") == "pin":
                    occupied_slots.add(int(credential["credential_index"]))

        return {
            slot: SlotCode.UNKNOWN if slot in occupied_slots else SlotCode.EMPTY
            for slot in code_slots
        }

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot.

        Returns True unconditionally because Matter does not reveal whether
        the credential value actually changed.
        """
        await _async_call_matter_service(
            self,
            "set_lock_credential",
            {
                "entity_id": self.lock.entity_id,
                "credential_type": "pin",
                "credential_data": usercode,
                "credential_index": code_slot,
            },
        )
        if name is not None:
            await _async_call_matter_service(
                self,
                "set_lock_user",
                {
                    "entity_id": self.lock.entity_id,
                    "credential_index": code_slot,
                    "user_name": name,
                },
            )
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot.

        Returns True if a credential was cleared, False if the slot was already empty.
        """
        # Check if credential exists before clearing
        lock_data = await _async_call_matter_service(
            self,
            "get_lock_credential_status",
            {
                "entity_id": self.lock.entity_id,
                "credential_type": "pin",
                "credential_index": code_slot,
            },
        )
        if not lock_data.get("credential_exists"):
            return False

        await _async_call_matter_service(
            self,
            "clear_lock_credential",
            {
                "entity_id": self.lock.entity_id,
                "credential_type": "pin",
                "credential_index": code_slot,
            },
        )
        return True

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Perform hard refresh and return all codes.

        Matter has no cache to invalidate, so this is identical to async_get_usercodes.
        """
        return await self.async_get_usercodes()
