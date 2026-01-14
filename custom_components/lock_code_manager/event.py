"""Event entity for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import EVENT_LOCK_STATE_CHANGED, EVENT_PIN_USED
from .data import LockCodeManagerConfigEntry
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
                    hass, ent_reg, config_entry, slot_num, EVENT_PIN_USED
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
    """Code slot event entity for lock code manager.

    The event_types are the lock entity IDs that support code slot events.
    When a PIN is used, the event type is the lock entity ID where it was used.
    Locks that don't support code slot events are listed in unsupported_locks attribute.
    """

    _attr_entity_category = None
    _attr_translation_key = EVENT_PIN_USED

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
        self._attr_name = None
        # Track the last event type to preserve it in event_types even if lock removed
        self._last_event_type: str | None = None

    def _get_supported_locks(self) -> list[BaseLock]:
        """Get locks that support code slot events."""
        return [lock for lock in self.locks if lock.supports_code_slot_events]

    def _get_unsupported_locks(self) -> list[BaseLock]:
        """Get locks that don't support code slot events."""
        return [lock for lock in self.locks if not lock.supports_code_slot_events]

    def _compute_event_types(self) -> list[str]:
        """Compute current event_types from supported locks.

        Includes supported lock entity IDs plus the last event type if it's
        from a lock that was removed (to preserve history until next event).
        """
        supported_lock_ids = {
            lock.lock.entity_id for lock in self._get_supported_locks()
        }

        # Include last event type if it exists and isn't in current locks
        # This preserves history even if a lock was removed
        if self._last_event_type and self._last_event_type not in supported_lock_ids:
            return list(supported_lock_ids | {self._last_event_type})

        return list(supported_lock_ids) if supported_lock_ids else [EVENT_PIN_USED]

    @property
    def event_types(self) -> list[str]:
        """Return supported event types (lock entity IDs)."""
        return self._compute_event_types()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes.

        Includes unsupported_locks list for locks that can't fire code slot events.
        """
        attrs: dict[str, Any] = {}
        unsupported = self._get_unsupported_locks()
        if unsupported:
            attrs[ATTR_UNSUPPORTED_LOCKS] = [
                lock.lock.entity_id for lock in unsupported
            ]
        return attrs

    @callback
    def _handle_event(self, event: Event) -> None:
        """Handle event.

        The event type is the lock entity ID where the PIN was used.
        """
        lock_entity_id = event.data.get(ATTR_ENTITY_ID)
        if lock_entity_id:
            self._last_event_type = lock_entity_id
            self._trigger_event(lock_entity_id, event.data)
        else:
            # Fallback to generic event type if no lock entity ID
            self._trigger_event(EVENT_PIN_USED, event.data)
        self.async_write_ha_state()

    @callback
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """Handle lock entities being added."""
        super()._handle_add_locks(locks)
        # Update state to reflect new event_types
        self.async_write_ha_state()

    @callback
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """Handle lock entity being removed."""
        super()._handle_remove_lock(lock_entity_id)
        # Update state to reflect new event_types
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

        # Restore last event type from state if available
        if (last_state := await self.async_get_last_state()) and last_state.attributes:
            # The last event type is stored in the state's event_type attribute
            if event_type := last_state.attributes.get("event_type"):
                self._last_event_type = event_type

        # Listen for lock state changed events
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_event,
                self._event_filter,
            )
        )
