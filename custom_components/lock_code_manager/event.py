"""Sensor for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EVENT_LOCK_STATE_CHANGED, EVENT_PIN_USED
from .entity import BaseLockCodeManagerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_code_slot_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add code slot event entities for slot."""
        async_add_entities(
            [
                LockCodeManagerCodeSlotEventEntity(
                    hass, ent_reg, config_entry, slot_num, EVENT_PIN_USED
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_{config_entry.entry_id}_add", add_code_slot_entities
        )
    )
    return True


class LockCodeManagerCodeSlotEventEntity(BaseLockCodeManagerEntity, EventEntity):
    """Code slot event entity for lock code manager."""

    _attr_entity_category = None
    _attr_event_types = [EVENT_PIN_USED]
    _attr_translation_key = EVENT_PIN_USED

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: ConfigEntry,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, key
        )
        self._attr_name = None

    @callback
    def _handle_event(self, event: Event) -> None:
        """Handle event."""
        self._trigger_event(EVENT_PIN_USED, event.data)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_event,
                self._event_filter,
            )
        )
