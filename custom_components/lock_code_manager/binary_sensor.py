"""Binary sensor entities for lock_code_manager."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import (
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
    EntityCategory,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    ATTR_SYNC_STATUS,
    CONF_NUMBER_OF_USES,
    EVENT_PIN_USED,
)
from .coordinator import LockUsercodeUpdateCoordinator
from .data import get_entry_config
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity, BaseLockCodeManagerEntity
from .models import LockCodeManagerConfigEntry
from .providers import BaseLock
from .sync import SlotSyncManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_pin_active_entity(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add active binary sensor entities for slot."""
        async_add_entities(
            [
                LockCodeManagerActiveEntity(
                    hass, ent_reg, config_entry, slot_num, ATTR_ACTIVE
                )
            ],
            True,
        )

    @callback
    def add_code_slot_entities(
        lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ):
        """Add code slot sensor entities for slot."""
        coordinator = lock.coordinator
        if coordinator is None:
            _LOGGER.warning(
                "%s (%s): Coordinator missing for lock %s when adding slot %s entities",
                config_entry.entry_id,
                config_entry.title,
                lock.lock.entity_id,
                slot_num,
            )
            return
        async_add_entities(
            [
                LockCodeManagerCodeSlotInSyncEntity(
                    hass, ent_reg, config_entry, coordinator, lock, slot_num
                )
            ],
            True,
        )

    callbacks = config_entry.runtime_data.callbacks
    config_entry.async_on_unload(
        callbacks.register_standard_adder(add_pin_active_entity)
    )
    config_entry.async_on_unload(
        callbacks.register_lock_slot_adder(add_code_slot_entities)
    )
    return True


class LockCodeManagerActiveEntity(BaseLockCodeManagerEntity, BinarySensorEntity):
    """Active binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _condition_entity_unsub: Callable[[], None] | None = None
    _subscribed_condition_entity_id: str | None = None

    @property
    def _condition_entity_id(self) -> str | None:
        """Return condition entity ID for this slot."""
        return (
            get_entry_config(self.config_entry).slot(self.slot_num).get(CONF_ENTITY_ID)
        )

    @callback
    def _cleanup_condition_subscription(self) -> None:
        """Clean up condition entity subscription if one exists."""
        if self._condition_entity_unsub:
            self._condition_entity_unsub()
            self._condition_entity_unsub = None

    @callback
    def _update_state(self, _: datetime | None = None) -> None:
        """Update binary sensor state by getting dependent states."""
        _LOGGER.debug(
            "%s (%s): Updating %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
        )

        states: dict[str, bool | None] = {}
        for key, state in (
            get_entry_config(self.config_entry).slot(self.slot_num).items()
        ):
            if key in (EVENT_PIN_USED, CONF_NAME, CONF_PIN, ATTR_IN_SYNC):
                continue

            # Handle condition entity - ON means access granted
            if key == CONF_ENTITY_ID and (hass_state := self.hass.states.get(state)):
                states[key] = (
                    hass_state.state == STATE_ON
                    if hass_state.state in (STATE_ON, STATE_OFF)
                    else None
                )
                continue

            if key == CONF_NUMBER_OF_USES:
                states[key] = bool(int(float(state)))
                continue
            states[key] = state

        # For the binary sensor to be on, all states must be 'on', or for the number
        # of uses, greater than 0
        inactive_because_of = [key for key, state in states.items() if not state]
        self._attr_is_on = bool(not inactive_because_of)
        if inactive_because_of:
            self._attr_extra_state_attributes["inactive_because_of"] = (
                inactive_because_of
            )
        else:
            self._attr_extra_state_attributes.pop("inactive_because_of", None)
        self.async_write_ha_state()

    async def _config_entry_update_listener(
        self, hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
    ) -> None:
        """Update listener."""
        if config_entry.options:
            return
        # Re-subscribe if condition entity changed
        self._update_condition_entity_subscription()
        self._update_state()

    @callback
    def _handle_condition_entity_state_change(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle condition entity state change."""
        self._update_state()

    @callback
    def _update_condition_entity_subscription(self) -> None:
        """Update subscription for condition entity if it changed."""
        current_entity_id = self._condition_entity_id
        old_entity_id = self._subscribed_condition_entity_id

        # No change needed if entity ID hasn't changed
        if current_entity_id == old_entity_id:
            return

        # Unsubscribe from old entity if we had one
        self._cleanup_condition_subscription()

        # Subscribe to new entity if we have one
        if current_entity_id:
            self._condition_entity_unsub = async_track_state_change_event(
                self.hass,
                [current_entity_id],
                self._handle_condition_entity_state_change,
            )

        self._subscribed_condition_entity_id = current_entity_id
        _LOGGER.debug(
            "%s (%s): Updated condition entity subscription for %s: %s -> %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
            old_entity_id,
            current_entity_id,
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

        # Register cleanup for condition entity subscription (called on entity removal)
        self.async_on_remove(self._cleanup_condition_subscription)

        # Track state changes for configured condition entity
        self._update_condition_entity_subscription()

        self.async_on_remove(
            self.config_entry.add_update_listener(self._config_entry_update_listener)
        )

        self._update_state()


class LockCodeManagerCodeSlotInSyncEntity(
    BaseLockCodeManagerCodeSlotPerLockEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
    BinarySensorEntity,
):
    """PIN synced binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        coordinator: LockUsercodeUpdateCoordinator,
        lock: BaseLock,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, ATTR_IN_SYNC
        )
        CoordinatorEntity.__init__(self, coordinator)

        self._attr_sync_status: str | None = None

        @callback
        def _sync_and_write_state(in_sync: bool | None) -> None:
            """Sync _attr_is_on from manager and write HA state."""
            self._attr_is_on = in_sync
            self._attr_sync_status = self._sync_manager.sync_status
            self.async_write_ha_state()

        self._sync_manager = SlotSyncManager(
            hass,
            ent_reg,
            config_entry,
            coordinator,
            lock,
            slot_num,
            state_writer=_sync_and_write_state,
        )

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and (
            int(self.slot_num) in self.coordinator.data
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Return extra state attributes."""
        return {ATTR_SYNC_STATUS: self._attr_sync_status}

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)

        self.async_on_remove(self._sync_manager.async_stop)
        await self._sync_manager.async_start()
