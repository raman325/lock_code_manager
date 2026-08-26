"""Event entity for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.const import ATTR_ENTITY_ID, ATTR_NAME
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_SOURCE,
    ATTR_TARGET,
    BUS_EVENT_CREDENTIAL_USED,
    EVENT_CREDENTIAL_USED,
    EVENT_LOCK_STATE_CHANGED,
)
from .domain.models import LockCodeManagerConfigEntry
from .domain.queries import get_entry_config
from .entity import BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

ATTR_UNSUPPORTED_LOCKS = "unsupported_locks"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_code_slot_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add code slot event entities for slot."""
        async_add_entities(
            [
                LockCodeManagerCodeSlotEventEntity(
                    hass, ent_reg, config_entry, slot_num, EVENT_CREDENTIAL_USED
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_standard_adder(
            add_code_slot_entities
        )
    )
    return True


class LockCodeManagerCodeSlotEventEntity(BaseLockCodeManagerEntity, EventEntity):
    """
    Code slot event entity for lock code manager.

    The event_types are the lock entity IDs that support code slot events.
    When a PIN is used, the event type is the lock entity ID where it was used.
    Locks that don't support code slot events are listed in unsupported_locks attribute.
    """

    _attr_entity_category = None
    _attr_translation_key = EVENT_CREDENTIAL_USED

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, key
        )

    def _get_supported_locks(self) -> list[BaseLock]:
        """Get locks that support code slot events."""
        return [lock for lock in self.locks if lock.supports_code_slot_events]

    @property
    def event_types(self) -> list[str]:
        """Return supported event types (lock entity IDs)."""
        return [lock.lock.entity_id for lock in self._get_supported_locks()]

    @property
    def available(self) -> bool:
        """
        Return True if entity is available.

        The event entity is unavailable if no locks support code slot events.
        """
        if not self._get_supported_locks():
            return False
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """
        Return extra state attributes.

        Includes unsupported_locks list for locks that can't fire code slot events.
        Computed dynamically to reflect any changes in lock capabilities.

        Starts from what the base class publishes: a property shadows
        ``_attr_extra_state_attributes`` outright, so building a fresh dict
        here dropped this entity's slot identity and left it the one entity a
        template could not find by slot.
        """
        attrs: dict[str, Any] = dict(self._attr_extra_state_attributes)
        unsupported = [
            lock.lock.entity_id
            for lock in self.locks
            if not lock.supports_code_slot_events
        ]
        if unsupported:
            attrs[ATTR_UNSUPPORTED_LOCKS] = unsupported
        return attrs

    @callback
    def _handle_event(self, event: Event) -> None:
        """
        Handle event.

        The event type is the lock entity ID where the PIN was used.
        _trigger_event stores the event type internally in EventEntity.
        """
        lock_entity_id = event.data.get(ATTR_ENTITY_ID)
        if not lock_entity_id:
            _LOGGER.warning("Received event without lock entity ID: %s", event.data)
            return
        self._trigger_event(lock_entity_id, event.data)
        self.async_write_ha_state()

    @callback
    def _credential_used_filter(self, event_data: dict[str, Any]) -> bool:
        """
        Keep the unified events this slot's entity is the one to record.

        The unified event names the person rather than the slot number they
        occupy, so working out whose entity this is means asking the
        configuration who holds this slot right now.

        A use a lock observed itself arrives here as well as on the
        deprecated lock-state event, which carries the same use with the
        richer payload this entity publishes as its attributes. Recording
        both would fire the entity twice and leave it showing the thinner of
        the two. ``source`` and ``target`` being the same entity is what
        marks that case, and this guard retires with the deprecated event.
        """
        return (
            event_data[ATTR_CONFIG_ENTRY_ID] == self.entry_id
            and event_data[ATTR_SOURCE] != event_data[ATTR_TARGET]
            and event_data[ATTR_NAME]
            == get_entry_config(self.config_entry).name_for(self.slot_num)
        )

    @callback
    def _handle_credential_used(self, event: Event) -> None:
        """
        Record a use reported against one of this entry's locks.

        The target is the event type, the same as it is for a use a lock
        observed. Anything else -- a cover, an alarm panel, a lock in the
        entry that cannot fire code slot events -- is not one of this
        entity's event types, and ``EventEntity`` raises on an event type it
        was not told about. That is the ordinary case rather than a mistake:
        the action's target is whatever the caller says the credential acted
        on, and only some of those are things this entity can name.
        """
        target = event.data[ATTR_TARGET]
        if target not in self.event_types:
            return
        self._trigger_event(target, event.data)
        self.async_write_ha_state()

    @callback
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """Handle lock entities being added."""
        super()._handle_add_locks(locks)
        self.async_write_ha_state()

    @callback
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """Handle lock entity being removed."""
        super()._handle_remove_lock(lock_entity_id)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        # EventEntity.async_added_to_hass restores __last_event_type from stored data
        await EventEntity.async_added_to_hass(self)

        # Listen for lock state changed events
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_event,
                self._event_filter,
            )
        )

        # And for uses reported from outside, which reach this entity only as
        # the unified event: the action that reports them fires nothing
        # lock-shaped, because no lock observed anything.
        self.async_on_remove(
            self.hass.bus.async_listen(
                BUS_EVENT_CREDENTIAL_USED,
                self._handle_credential_used,
                self._credential_used_filter,
            )
        )
