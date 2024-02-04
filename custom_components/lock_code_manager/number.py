"""Number for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNLOCKED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CODE_SLOT,
    ATTR_TO,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
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
    def add_number_entities(slot_num: int) -> None:
        """Add number entities for slot."""
        async_add_entities(
            [LockCodeManagerNumber(config_entry, locks, slot_num, CONF_NUMBER_OF_USES)],
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
        entry = self.config_entry
        return entry.data[CONF_SLOTS][self.slot_num].get(self.key)

    async def async_set_native_value(self, value: int) -> None:
        """Set value of number."""
        self._update_config_entry(value)

    def _zwave_js_event_filter(self, event: Event) -> bool:
        """Filter zwave_js events."""
        return (
            any(
                event.data[ATTR_ENTITY_ID] == lock.lock.entity_id for lock in self.locks
            )
            and event.data[ATTR_CODE_SLOT] == int(self.slot_num)
            and event.data[ATTR_TO] == STATE_UNLOCKED
        )

    async def _handle_lock_state_changed(self, event: Event):
        """Handle lock state changed."""
        await self.async_set_native_value(self.native_value - 1)

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await NumberEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_lock_state_changed,
                self._zwave_js_event_filter,
            )
        )
