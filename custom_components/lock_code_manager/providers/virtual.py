"""Module for Virtual locks."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import TypedDict

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)


class CodeSlotData(TypedDict):
    """Type for code slot data."""

    code: int | str
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

    async def async_setup(self) -> None:
        """Set up lock."""
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

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return True

    async def async_hard_refresh_codes(self) -> None:
        """
        Perform hard refresh of all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock.
        """
        self._data = data if (data := await self._store.async_load()) else {}

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        self._data[str(code_slot)] = CodeSlotData(code=usercode, name=name)

    async def async_clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        if str(code_slot) not in self._data:
            raise HomeAssistantError(f"Code slot {code_slot} not found")
        self._data.pop(str(code_slot))

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        return {
            int(slot_num): code_slot["code"]
            for slot_num, code_slot in self._data.items()
        }
