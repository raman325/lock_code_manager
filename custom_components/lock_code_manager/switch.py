"""Switch for lock_code_manager."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import ToggleEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from .entity import BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Setup config entry."""
    locks: list[BaseLock] = list(
        hass.data[DOMAIN][config_entry.entry_id][CONF_LOCKS].values()
    )

    @callback
    def add_switch_entities(slot_num: int) -> None:
        """Add switch entities for slot."""
        async_add_entities(
            [LockCodeManagerSwitch(config_entry, locks, slot_num, CONF_ENABLED)], True
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add",
            add_switch_entities,
        )
    )
    return True


class LockCodeManagerSwitch(BaseLockCodeManagerEntity, ToggleEntity):
    """Switch entity for lock code manager."""

    @property
    def is_on(self) -> bool:
        """Return native value."""
        entry = self.config_entry
        return entry.data[CONF_SLOTS][self.slot_num].get(self.key)

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on switch."""
        self._update_config_entry(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off switch."""
        self._update_config_entry(False)

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await ToggleEntity.async_added_to_hass(self)
