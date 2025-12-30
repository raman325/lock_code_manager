"""Binary sensor for lock_code_manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorEntity,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.const import (
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    TrackStates,
    async_call_later,
    async_track_state_change_event,
    async_track_state_change_filtered,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity, UpdateFailed

from .const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    ATTR_IN_SYNC,
    CONF_CALENDAR,
    CONF_NUMBER_OF_USES,
    DOMAIN,
    EVENT_PIN_USED,
)
from .coordinator import LockUsercodeUpdateCoordinator
from .data import LockCodeManagerConfigEntry, get_slot_data
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity, BaseLockCodeManagerEntity
from .exceptions import LockDisconnected
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)
RETRY_DELAY = timedelta(seconds=10)


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

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_{config_entry.entry_id}_add", add_pin_active_entity
        )
    )
    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add_lock_slot",
            add_code_slot_entities,
        )
    )
    return True


class LockCodeManagerActiveEntity(BaseLockCodeManagerEntity, BinarySensorEntity):
    """Active binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        for key, state in get_slot_data(self.config_entry, self.slot_num).items():
            if key in (EVENT_PIN_USED, CONF_NAME, CONF_PIN, ATTR_IN_SYNC):
                continue

            if key == CONF_CALENDAR and (hass_state := self.hass.states.get(state)):
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
        self._update_state()

    @callback
    def _handle_calendar_state_changes(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle calendar state changes."""
        if event.data["entity_id"] == self._calendar_entity_id:
            self._update_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

        self.async_on_remove(
            async_track_state_change_filtered(
                self.hass,
                TrackStates(False, set(), {Platform.CALENDAR}),
                self._handle_calendar_state_changes,
            ).async_remove
        )

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
        self._entity_id_map: dict[str, str] = {}
        self._active_unique_id = self._get_uid(ATTR_ACTIVE)
        self._name_text_unique_id = self._get_uid(CONF_NAME)
        self._pin_text_unique_id = self._get_uid(CONF_PIN)
        lock_entity_id = self.lock.lock.entity_id
        self._lock_slot_sensor_unique_id = (
            f"{self._get_uid(ATTR_CODE)}|{lock_entity_id}"
        )
        self._lock = asyncio.Lock()
        self._attr_is_on: bool | None = None  # None means not yet initialized
        self._tracked_entity_ids: set[str] = set()
        self._retry_unsub: Callable[[], None] | None = None
        self._retry_active = False

    @property
    def should_poll(self) -> bool:
        """Return whether entity should poll."""
        return True

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and (
            int(self.slot_num) in self.coordinator.data
        )

    async def async_update(self) -> None:
        """Update entity."""
        if self._attr_is_on is None:
            return

        if (
            self._lock.locked()
            or self.is_on
            or not (state := self.hass.states.get(self.lock.lock.entity_id))
            or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            or (not self.coordinator.last_update_success and not self._retry_active)
        ):
            return

        _LOGGER.debug(
            "Code slot %s on %s is out of sync, syncing now",
            self.slot_num,
            self.lock.lock.entity_id,
        )
        await self._async_update_state()

    def _get_entity_state(self, key: str) -> str | None:
        """Get entity state."""
        if (state := self.hass.states.get(self._entity_id_map[key])) is None:
            return None
        return state.state

    def _update_sync_state(self, is_on: bool) -> None:
        """Update sync state and write to Home Assistant."""
        self._attr_is_on = is_on
        self.async_write_ha_state()

    def _cancel_retry(self) -> None:
        """Cancel any scheduled retry callback."""
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
        self._retry_active = False

    def _schedule_retry(self) -> None:
        """Schedule a retry if one isn't already pending."""
        if self._retry_unsub:
            return

        _LOGGER.debug(
            "%s (%s): Scheduling retry for %s slot %s in %ss",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.lock.lock.entity_id,
            self.slot_num,
            RETRY_DELAY.total_seconds(),
        )

        self._retry_unsub = async_call_later(
            self.hass,
            RETRY_DELAY.total_seconds(),
            self._handle_retry_callback,
        )

    async def _handle_retry_callback(self, _now: datetime) -> None:
        """Handle retry callback."""
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None
        self._retry_active = True
        try:
            await self.async_update()
        finally:
            self._retry_active = False

    def _build_entity_id_map(self) -> bool:
        """Build and cache entity IDs for this slot."""
        missing = False
        for key, domain, unique_id in (
            (CONF_PIN, TEXT_DOMAIN, self._pin_text_unique_id),
            (CONF_NAME, TEXT_DOMAIN, self._name_text_unique_id),
            (ATTR_ACTIVE, BINARY_SENSOR_DOMAIN, self._active_unique_id),
            (ATTR_CODE, SENSOR_DOMAIN, self._lock_slot_sensor_unique_id),
        ):
            if key in self._entity_id_map:
                continue
            ent_id = self.ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
            if not ent_id:
                _LOGGER.debug(
                    "%s (%s): Missing %s entity for %s slot %s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    key,
                    self.lock.lock.entity_id,
                    self.slot_num,
                )
                missing = True
                continue
            self._entity_id_map[key] = ent_id
        self._tracked_entity_ids = set(self._entity_id_map.values())
        return not missing

    def _ensure_entities_ready(self) -> bool:
        """Ensure all dependent entities exist with valid states."""
        for key in (CONF_PIN, CONF_NAME, ATTR_ACTIVE, ATTR_CODE):
            if key not in self._entity_id_map:
                return False

            # Verify entity has a state
            state = self._get_entity_state(key)
            if state is None or state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                _LOGGER.debug(
                    "%s (%s): Waiting for %s state for %s slot %s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    key,
                    self.lock.lock.entity_id,
                    self.slot_num,
                )
                return False

        return True

    def _calculate_expected_sync(self, slot_state: _SlotState) -> bool:
        """Calculate whether slot should be in sync.

        Active: PIN should match code on lock
        Inactive: Code on lock should be empty
        """
        # Use coordinator data if available, otherwise fall back to sensor state
        lock_code = (
            str(slot_state.coordinator_code)
            if slot_state.coordinator_code is not None
            else slot_state.code_state
        )
        if slot_state.active_state == STATE_ON:
            return slot_state.pin_state == lock_code
        return lock_code == ""

    @dataclass(frozen=True)
    class _SlotState:
        active_state: str
        pin_state: str
        name_state: str | None
        code_state: str
        coordinator_code: int | str | None

    def _resolve_slot_state(
        self, event: Event[EventStateChangedData] | None
    ) -> _SlotState | None:
        """Resolve slot state while applying shared guards."""
        entity_id = event.data["entity_id"] if event else None
        to_state = event.data["new_state"] if event else None

        if not self.coordinator.last_update_success and not self._retry_active:
            return None

        if not self._build_entity_id_map():
            return None

        if self._attr_is_on is None and int(self.slot_num) not in self.coordinator.data:
            _LOGGER.debug(
                "%s (%s): Slot %s not yet in coordinator data, skipping",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.slot_num,
            )
            return None

        if entity_id is not None and entity_id not in self._tracked_entity_ids:
            return None

        if to_state and to_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None

        if not self._ensure_entities_ready():
            return None

        active_state = self._get_entity_state(ATTR_ACTIVE)
        pin_state = self._get_entity_state(CONF_PIN)
        name_state = self._get_entity_state(CONF_NAME)
        code_state = self._get_entity_state(ATTR_CODE)

        if active_state is None or pin_state is None or code_state is None:
            return None

        coordinator_code = self.coordinator.data.get(int(self.slot_num))
        return self._SlotState(
            active_state=active_state,
            pin_state=pin_state,
            name_state=name_state,
            code_state=code_state,
            coordinator_code=coordinator_code,
        )

    async def _perform_sync_operation(self, slot_state: _SlotState) -> bool:
        """Perform sync operation (set or clear usercode).

        Returns True if sync was performed, False if lock disconnected.
        """
        try:
            if slot_state.active_state == STATE_ON:
                await self.lock.async_internal_set_usercode(
                    int(self.slot_num), slot_state.pin_state, slot_state.name_state
                )
                _LOGGER.debug(
                    "%s (%s): Set usercode for %s slot %s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    self.lock.lock.entity_id,
                    self.slot_num,
                )
            else:  # active_state == STATE_OFF
                await self.lock.async_internal_clear_usercode(int(self.slot_num))
                _LOGGER.debug(
                    "%s (%s): Cleared usercode for %s slot %s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    self.lock.lock.entity_id,
                    self.slot_num,
                )
            self._cancel_retry()
            return True
        except LockDisconnected as err:
            _LOGGER.debug(
                "%s (%s): Unable to %s usercode for %s slot %s: %s",
                self.config_entry.entry_id,
                self.config_entry.title,
                "set" if slot_state.active_state == STATE_ON else "clear",
                self.lock.lock.entity_id,
                self.slot_num,
                err,
            )
            self._schedule_retry()
            return False

    async def _async_update_state(
        self, event: Event[EventStateChangedData] | None = None
    ) -> None:
        """Update binary sensor state by checking dependent entity states.

        On initial load (when _attr_is_on is None): Sets sync state without operations.
        On subsequent updates: Performs sync operations when out of sync.
        """
        async with self._lock:
            slot_state = self._resolve_slot_state(event)
            if slot_state is None:
                return

            # Calculate expected sync state
            expected_in_sync = self._calculate_expected_sync(slot_state)

            # Initial load: Set sync state without performing operations (prevents startup flapping)
            if self._attr_is_on is None:
                # Guard: Verify active state is valid
                if slot_state.active_state not in (STATE_ON, STATE_OFF):
                    _LOGGER.debug(
                        "%s (%s): Active entity for %s slot %s has invalid state '%s'",
                        self.config_entry.entry_id,
                        self.config_entry.title,
                        self.lock.lock.entity_id,
                        self.slot_num,
                        slot_state.active_state,
                    )
                    return

                self._update_sync_state(expected_in_sync)
                _LOGGER.debug(
                    "%s (%s): Initial state loaded for %s slot %s, in_sync=%s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    self.lock.lock.entity_id,
                    self.slot_num,
                    expected_in_sync,
                )
                return

            # Normal operation: Perform sync if needed
            if not expected_in_sync:
                self._update_sync_state(False)

                # Perform sync operation
                sync_performed = await self._perform_sync_operation(slot_state)

                # Refresh coordinator to verify operation completed
                # Rate limiting at provider level prevents excessive calls
                if sync_performed:
                    try:
                        await self.coordinator.async_refresh()
                    except UpdateFailed as err:
                        _LOGGER.debug(
                            "%s (%s): Coordinator refresh failed after sync for %s "
                            "slot %s, scheduling retry: %s",
                            self.config_entry.entry_id,
                            self.config_entry.title,
                            self.lock.lock.entity_id,
                            self.slot_num,
                            err,
                        )
                        self._schedule_retry()

            elif not self._attr_is_on:
                # Was out of sync, now in sync
                self._update_sync_state(True)
                self._cancel_retry()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)

        if self._build_entity_id_map():
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, self._tracked_entity_ids, self._async_update_state
                )
            )
        else:
            self.async_on_remove(
                async_track_state_change_filtered(
                    self.hass, TrackStates(True, set(), set()), self._async_update_state
                ).async_remove
            )
        await self._async_update_state()

    async def _async_remove(self) -> None:
        """Handle removal cleanup."""
        self._cancel_retry()
