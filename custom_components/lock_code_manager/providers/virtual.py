"""Module for Virtual locks."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Literal, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..domain.credentials import Credential, CredentialRef, User, user_from_slot
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import parse_slot_num

_LOGGER = logging.getLogger(__name__)


class CodeSlotData(TypedDict):
    """Type for code slot data."""

    code: str
    name: str | None


@dataclass(repr=False, eq=False)
class VirtualLock(BaseLock):
    """Class to represent Virtual lock."""

    _store: Store[dict[str, CodeSlotData]] = field(init=False, repr=False)
    _data: dict[str, CodeSlotData] = field(default_factory=dict, init=False, repr=False)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "virtual"

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False

    async def async_is_integration_connected(self) -> bool:
        """Virtual locks are always connected."""
        return True

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock by provider."""
        self._store = Store(
            self.hass, 1, f"{self.domain}_{DOMAIN}_{self.lock.entity_id}"
        )
        await self.async_hard_refresh_codes()

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        if remove_permanently:
            await self._store.async_remove()
        else:
            await self._store.async_save(self._data)

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """Reload from store and return all codes."""
        self._data = data if (data := await self._store.async_load()) else {}
        return await self.async_get_usercodes()

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """
        Set a Personal Identification Number credential on a code slot.

        Returns True if the value was changed, False if already set to this value.
        Ignores ``user_id``; slot-only providers address the credential by slot.
        """
        slot_key = str(credential.slot)
        new_data = CodeSlotData(code=credential.readable_pin or "", name=name)
        if slot_key in self._data and self._data[slot_key] == new_data:
            return False
        self._data[slot_key] = new_data
        return True

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ``ref``; return whether it changed.

        Returns True if a code was removed, False if the slot was already empty.
        """
        slot_key = str(ref.slot)
        if slot_key not in self._data:
            return False
        self._data.pop(slot_key)
        return True

    async def async_get_users(self) -> list[User]:
        """
        Return users by reading all stored and managed slots.

        Returns occupied slots as known-Personal-Identification-Number users
        and managed empty slots as empty users so the base projection can
        surface them. Unmanaged occupied slots are also included so callers
        like the lock-reset config flow step can detect codes not managed
        by Lock Code Manager.
        """
        managed_slots = self.managed_slots
        stored_slots = set()
        for k in self._data:
            slot_num = parse_slot_num(k)
            if slot_num is None:
                _LOGGER.warning(
                    "Virtual lock %s: skipping stored slot with invalid key %r",
                    self.lock.entity_id,
                    k,
                )
                continue
            stored_slots.add(slot_num)
        all_slots = managed_slots | stored_slots
        slot_states: dict[int, SlotCredential] = {}
        for slot_num in all_slots:
            slot_key = str(slot_num)
            if slot_key in self._data:
                slot_states[slot_num] = SlotCredential.known(
                    str(self._data[slot_key]["code"])
                )
            else:
                slot_states[slot_num] = SlotCredential.empty()
        return [user_from_slot(slot, state) for slot, state in slot_states.items()]
