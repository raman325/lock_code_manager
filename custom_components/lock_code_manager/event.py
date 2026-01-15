"""Event entity for lock_code_manager."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
from typing import Any, Self

from homeassistant.components.event import ATTR_EVENT_TYPE, EventEntity
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData

from .const import EVENT_LOCK_STATE_CHANGED, EVENT_PIN_USED
from .data import LockCodeManagerConfigEntry
from .entity import BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

ATTR_UNSUPPORTED_LOCKS = "unsupported_locks"


@dataclass
class LockCodeManagerEventExtraStoredData(ExtraStoredData):
    """Extra stored data for lock code manager event entity."""

    unsupported_locks: list[str]

    def as_dict(self) -> dict[str, Any]:
        """Return a dict representation of the data."""
        return asdict(self)

    @classmethod
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        """Initialize from a dict."""
        try:
            return cls(restored["unsupported_locks"])
        except KeyError:
            return None


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
        # Track unsupported locks (restored from extra stored data on startup)
        self._unsupported_locks: list[str] = []

    def _get_supported_locks(self) -> list[BaseLock]:
        """Get locks that support code slot events."""
        return [lock for lock in self.locks if lock.supports_code_slot_events]

    def _get_unsupported_locks(self) -> list[BaseLock]:
        """Get locks that don't support code slot events."""
        return [lock for lock in self.locks if not lock.supports_code_slot_events]

    def _update_unsupported_locks(self) -> None:
        """Update the cached list of unsupported lock entity IDs."""
        self._unsupported_locks = [
            lock.lock.entity_id for lock in self._get_unsupported_locks()
        ]

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
        # state_attributes contains {ATTR_EVENT_TYPE: last_event_type} from EventEntity
        # Defensive check: state_attributes may be None during early initialization
        attrs = self.state_attributes or {}
        last_event_type = attrs.get(ATTR_EVENT_TYPE)
        if last_event_type and last_event_type not in supported_lock_ids:
            return list(supported_lock_ids | {last_event_type})

        return list(supported_lock_ids)

    @property
    def event_types(self) -> list[str]:
        """Return supported event types (lock entity IDs)."""
        return self._compute_event_types()

    @property
    def available(self) -> bool:
        """Return True if entity is available.

        The event entity is unavailable if no locks support code slot events.
        """
        if not self._get_supported_locks():
            return False
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes.

        Includes unsupported_locks list for locks that can't fire code slot events.
        """
        attrs: dict[str, Any] = {}
        if self._unsupported_locks:
            attrs[ATTR_UNSUPPORTED_LOCKS] = self._unsupported_locks
        return attrs

    @property
    def extra_restore_state_data(self) -> LockCodeManagerEventExtraStoredData:
        """Return extra data to be stored for restoration."""
        return LockCodeManagerEventExtraStoredData(self._unsupported_locks)

    async def _async_get_last_extra_data(
        self,
    ) -> LockCodeManagerEventExtraStoredData | None:
        """Get last extra stored data."""
        if (restored := await self.async_get_last_extra_data()) is None:
            return None
        return LockCodeManagerEventExtraStoredData.from_dict(restored.as_dict())

    @callback
    def _handle_event(self, event: Event) -> None:
        """Handle event.

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
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """Handle lock entities being added."""
        super()._handle_add_locks(locks)
        # Update cached unsupported locks and state to reflect new event_types
        self._update_unsupported_locks()
        self.async_write_ha_state()

    @callback
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """Handle lock entity being removed."""
        super()._handle_remove_lock(lock_entity_id)
        # Update cached unsupported locks and state to reflect new event_types
        self._update_unsupported_locks()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        # EventEntity.async_added_to_hass restores __last_event_type from stored data
        await EventEntity.async_added_to_hass(self)

        # Restore unsupported_locks from extra stored data
        if restored := await self._async_get_last_extra_data():
            self._unsupported_locks = restored.unsupported_locks

        # Update unsupported locks from current locks if locks are available
        # (may override restored state if locks have changed since last run)
        if self.locks:
            self._update_unsupported_locks()

        # Listen for lock state changed events
        self.async_on_remove(
            self.hass.bus.async_listen(
                EVENT_LOCK_STATE_CHANGED,
                self._handle_event,
                self._event_filter,
            )
        )
