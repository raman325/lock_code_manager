"""Text for lock_code_manager."""
from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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
    def add_standard_text_entities(slot_num: int) -> None:
        """Add standard text entities for slot."""
        async_add_entities(
            [
                LockCodeManagerText(config_entry, locks, slot_num, key)
                for key in (CONF_NAME, CONF_PIN)
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_{config_entry.entry_id}_add", add_standard_text_entities
        )
    )

    return True


class LockCodeManagerText(BaseLockCodeManagerEntity, TextEntity):
    """Text entity for lock code manager."""

    _attr_native_min = 1
    _attr_native_max = 9999

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        entry = self.config_entry
        return entry.data[CONF_SLOTS][self.slot_num].get(self.key)

    async def async_set_value(self, value: str) -> None:
        """Set value of text."""
        self._update_config_entry(value)

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await TextEntity.async_added_to_hass(self)
