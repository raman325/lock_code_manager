"""Number for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_NUMBER_OF_USES, DOMAIN, EVENT_LOCK_STATE_CHANGED
from .entity import BaseLockCodeManagerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_number_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add number entities for slot."""
        async_add_entities(
            [
                LockCodeManagerNumber(
                    hass, ent_reg, config_entry, slot_num, CONF_NUMBER_OF_USES
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add_{CONF_NUMBER_OF_USES}",
            add_number_entities,
        )
    )
    return True


class LockCodeManagerNumber(BaseLockCodeManagerEntity, NumberEntity):
    """Number entity for lock code manager."""

    _attr_native_min_value = 0
    _attr_native_max_value = 999999999
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> int | None:
        """Return native value."""
        return self._state

    async def async_set_native_value(self, value: int) -> None:
        """Set value of number."""
        self._update_config_entry(value)

    async def _handle_lock_state_changed(self, event: Event):
        """Handle lock state changed."""
        if not (val := self.native_value):
            val = 1
        await self.async_set_native_value(val - 1)

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await NumberEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_lock_state_changed,
                self._event_filter,
            )
        )
