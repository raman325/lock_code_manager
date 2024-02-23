"""Sensor for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNLOCKED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CODE_SLOT,
    ATTR_TO,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
    EVENT_PIN_USED,
)
from .entity import BaseLockCodeManagerCodeSlotEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_code_slot_entities(lock: BaseLock, slot_num: int) -> None:
        """Add code slot event entities for slot."""
        async_add_entities(
            [
                LockCodeManagerCodeSlotEventEntity(
                    hass, config_entry, lock, slot_num, EVENT_PIN_USED
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add_lock_slot",
            add_code_slot_entities,
        )
    )
    return True


class LockCodeManagerCodeSlotEventEntity(
    BaseLockCodeManagerCodeSlotEntity, EventEntity
):
    """Code slot event entity for lock code manager."""

    _attr_entity_category = None
    _attr_event_types = [EVENT_PIN_USED]
    _attr_icon = "mdi:gesture-tap"
    _attr_translation_key = EVENT_PIN_USED

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        lock: BaseLock,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotEntity.__init__(
            self, hass, config_entry, lock, slot_num, key
        )
        self._attr_name = f"Code slot {slot_num}"

    @callback
    def _event_filter(self, event: Event) -> bool:
        """Filter events."""
        return (
            event.data[ATTR_ENTITY_ID] == self.lock.lock.entity_id
            and event.data[ATTR_CODE_SLOT] == int(self.slot_num)
            and event.data[ATTR_TO] == STATE_UNLOCKED
        )

    @callback
    def _handle_event(self, event: Event) -> None:
        """Handle event."""
        self._trigger_event(EVENT_PIN_USED, event.data)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerCodeSlotEntity.async_added_to_hass(self)
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_event,
                self._event_filter,
            )
        )
