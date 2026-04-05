"""Module for Virtual locks."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..data import get_managed_slots
from ..models import SlotCode
from ._base import BaseLock

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

    async def async_is_integration_connected(self) -> bool:
        """Return whether the integration is connected."""
        return True

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """
        Perform hard refresh and return all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock. Returns codes in the same format as async_get_usercodes().
        """
        self._data = data if (data := await self._store.async_load()) else {}
        return await self.async_get_usercodes()

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this value.
        """
        slot_key = str(code_slot)
        new_data = CodeSlotData(code=usercode, name=name)
        if slot_key in self._data and self._data[slot_key] == new_data:
            return False
        self._data[slot_key] = new_data
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        """
        slot_key = str(code_slot)
        if slot_key not in self._data:
            return False
        self._data.pop(slot_key)
        return True

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes.

        Returns all slots with data plus managed empty slots. Unmanaged
        occupied slots are included so callers like the lock reset config
        flow step can detect codes not managed by Lock Code Manager.
        """
        managed_slots = get_managed_slots(self.hass, self.lock.entity_id)
        stored_slots = set()
        for k in self._data:
            try:
                stored_slots.add(int(k))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Virtual lock %s: skipping stored slot with invalid key %r",
                    self.lock.entity_id,
                    k,
                )
                continue
        all_slots = managed_slots | stored_slots
        data: dict[int, str | SlotCode] = {}
        for slot_num in all_slots:
            slot_key = str(slot_num)
            if slot_key in self._data:
                data[slot_num] = str(self._data[slot_key]["code"])
            else:
                data[slot_num] = SlotCode.EMPTY
        return data
