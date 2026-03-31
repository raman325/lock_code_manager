"""Slot sync manager — owns desired vs actual reconciliation.

Compares entity states (active, PIN, name) against coordinator data (actual lock
code) and drives set/clear operations to reconcile. Manages retry scheduling,
attempt tracking, and circuit-breaking (disable slot after max failures).

Extracted from binary_sensor.py to separate domain logic from entity state display.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.const import (
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import (
    TrackStates,
    async_track_state_change_event,
    async_track_state_change_filtered,
)
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    RETRY_DELAY,
    SYNC_ATTEMPT_WINDOW,
)
from .exceptions import CodeRejectedError, LockDisconnected
from .util import OneShotRetry, async_disable_slot

if TYPE_CHECKING:
    from .coordinator import LockUsercodeUpdateCoordinator
    from .data import LockCodeManagerConfigEntry
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotState:
    """Snapshot of entity states for a slot on a specific lock."""

    active_state: str
    pin_state: str
    name_state: str | None
    code_state: str
    coordinator_code: str | None


class SlotSyncManager:
    """Manage sync state for a single lock × slot combination.

    Compares desired state (from entity states: active, PIN) against actual
    state (from coordinator data) and drives set/clear operations to reconcile.

    The manager discovers entity IDs from the entity registry, subscribes to
    their state changes and coordinator updates, and drives sync operations.
    The in-sync binary sensor entity reads manager.in_sync for display.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        coordinator: LockUsercodeUpdateCoordinator,
        lock: BaseLock,
        slot_num: int,
    ) -> None:
        """Initialize the sync manager."""
        self._hass = hass
        self._ent_reg = ent_reg
        self._config_entry = config_entry
        self._coordinator = coordinator
        self._lock = lock
        self._slot_num = slot_num

        self._log_prefix = (
            f"{config_entry.entry_id} ({config_entry.title}): "
            f"{lock.lock.entity_id} slot {slot_num}"
        )

        # Unique ID components for entity discovery
        base_uid = f"{config_entry.entry_id}|{slot_num}"
        lock_entity_id = lock.lock.entity_id
        self._unique_ids: dict[str, tuple[str, str]] = {
            CONF_PIN: (TEXT_DOMAIN, f"{base_uid}|{CONF_PIN}"),
            CONF_NAME: (TEXT_DOMAIN, f"{base_uid}|{CONF_NAME}"),
            ATTR_ACTIVE: (BINARY_SENSOR_DOMAIN, f"{base_uid}|{ATTR_ACTIVE}"),
            ATTR_CODE: (SENSOR_DOMAIN, f"{base_uid}|{ATTR_CODE}|{lock_entity_id}"),
        }

        # State
        self._in_sync: bool | None = None
        self._entity_id_map: dict[str, str] = {}
        self._tracked_entity_ids: set[str] = set()
        self._aio_lock = asyncio.Lock()
        self._state_writer: Callable[[], None] | None = None

        # Retry + circuit breaker
        self._retry = OneShotRetry(
            hass,
            RETRY_DELAY,
            self._async_poll_sync,
            f"{lock_entity_id} slot {slot_num}",
        )
        self._sync_attempt_count: int = 0
        self._sync_attempt_first: datetime | None = None

        # State tracking
        self._state_tracking_unsub: Callable[[], None] | None = None
        self._coordinator_unsub: Callable[[], None] | None = None
        self._tracking_all_states: bool = False
        self._started = False

    @property
    def in_sync(self) -> bool | None:
        """Return current sync state (None = not yet determined)."""
        return self._in_sync

    def set_state_writer(self, writer: Callable[[], None]) -> None:
        """Register the entity's async_write_ha_state callback."""
        self._state_writer = writer

    async def async_start(self) -> None:
        """Start the sync manager — discover entities, subscribe, initial sync."""
        if self._started:
            return
        self._started = True
        self._setup_state_tracking()
        self._setup_coordinator_listener()
        await self._async_check_and_sync()

    def async_stop(self) -> None:
        """Stop the sync manager — cancel retry, unsubscribe. Idempotent."""
        if not self._started:
            return
        self._started = False
        self._retry.cancel()
        self._cleanup_state_tracking()
        if self._coordinator_unsub:
            self._coordinator_unsub()
            self._coordinator_unsub = None
        self._reset_sync_tracker()

    # ── State resolution ────────────────────────────────────────────────

    def _get_entity_state(self, key: str) -> str | None:
        """Get entity state by role key."""
        entity_id = self._entity_id_map.get(key)
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        return state.state if state else None

    def _build_entity_id_map(self) -> bool:
        """Build and cache entity IDs for this slot from the entity registry."""
        missing = False
        for key, (domain, unique_id) in self._unique_ids.items():
            if key in self._entity_id_map:
                continue
            ent_id = self._ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
            if not ent_id:
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
            state = self._get_entity_state(key)
            if state is None or state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                _LOGGER.debug("%s: Waiting for %s state", self._log_prefix, key)
                return False
        return True

    def _resolve_slot_state(
        self, event: Event[EventStateChangedData] | None
    ) -> SlotState | None:
        """Resolve slot state while applying shared guards.

        All state reads are sync (hass.states.get) with no awaits between them,
        ensuring atomicity on HA's single-threaded event loop.
        """
        entity_id = event.data["entity_id"] if event else None
        to_state = event.data["new_state"] if event else None

        if not self._coordinator.last_update_success and not self._retry.active:
            return None

        if not self._build_entity_id_map():
            return None

        if self._in_sync is None and int(self._slot_num) not in self._coordinator.data:
            _LOGGER.debug(
                "%s: Slot not yet in coordinator data, skipping",
                self._log_prefix,
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

        coordinator_code = self._coordinator.data.get(int(self._slot_num))
        return SlotState(
            active_state=active_state,
            pin_state=pin_state,
            name_state=name_state,
            code_state=code_state,
            coordinator_code=coordinator_code,
        )

    # ── Sync calculation ────────────────────────────────────────────────

    @staticmethod
    def calculate_in_sync(slot_state: SlotState) -> bool:
        """Calculate whether slot should be in sync.

        Active (state=ON): PIN should match code on lock.
        Inactive (state=OFF): Code on lock should be empty.
        """
        lock_code = (
            slot_state.coordinator_code
            if slot_state.coordinator_code is not None
            else slot_state.code_state
        )
        if slot_state.active_state == STATE_ON:
            return slot_state.pin_state == lock_code
        return lock_code == ""

    # ── Sync execution ──────────────────────────────────────────────────

    async def _perform_sync(self, slot_state: SlotState) -> bool:
        """Execute sync operation (set or clear usercode).

        Returns True if sync was performed, False on disconnect or rejection.
        """
        try:
            if slot_state.active_state == STATE_ON:
                await self._lock.async_internal_set_usercode(
                    int(self._slot_num),
                    slot_state.pin_state,
                    slot_state.name_state,
                    source="sync",
                )
                # Only track set operations — clears always succeed and
                # shouldn't count toward the sync failure limit
                self._record_sync_attempt()
                _LOGGER.debug("%s: Set usercode", self._log_prefix)
            else:
                await self._lock.async_internal_clear_usercode(
                    int(self._slot_num), source="sync"
                )
                _LOGGER.debug("%s: Cleared usercode", self._log_prefix)
            self._retry.cancel()
            return True
        except CodeRejectedError as err:
            _LOGGER.error("%s: Code rejected: %s", self._log_prefix, err)
            await self._disable_slot(
                f"Lock **{err.lock_entity_id}**: slot **{err.code_slot}** "
                f"has been disabled. {err}\n\n"
                f"Fix the issue and re-enable the slot.",
                title="Lock Code Rejected",
            )
            return False
        except LockDisconnected as err:
            _LOGGER.debug(
                "%s: Unable to %s usercode: %s",
                self._log_prefix,
                "set" if slot_state.active_state == STATE_ON else "clear",
                err,
            )
            self._retry.schedule()
            return False

    async def _disable_slot(self, reason: str, title: str) -> None:
        """Disable the slot and create a persistent notification."""
        self._retry.cancel()
        await async_disable_slot(
            self._hass,
            self._ent_reg,
            self._config_entry.entry_id,
            self._slot_num,
            reason=reason,
            title=title,
            lock_name=self._lock.lock.name or self._lock.lock.original_name,
            lock_entity_id=self._lock.lock.entity_id,
        )
        self._reset_sync_tracker()

    # ── Attempt tracking + circuit breaker ──────────────────────────────

    def _reset_sync_tracker(self) -> None:
        """Reset the sync attempt tracker."""
        self._sync_attempt_count = 0
        self._sync_attempt_first = None

    def _record_sync_attempt(self) -> None:
        """Record a sync attempt (provider call succeeded)."""
        now = dt_util.utcnow()
        if (
            self._sync_attempt_first is not None
            and now - self._sync_attempt_first > SYNC_ATTEMPT_WINDOW
        ):
            self._sync_attempt_count = 0
            self._sync_attempt_first = None

        if self._sync_attempt_first is None:
            self._sync_attempt_first = now
        self._sync_attempt_count += 1

    def _sync_attempts_exceeded(self) -> bool:
        """Check if sync attempts exceeded the limit within the time window."""
        if self._sync_attempt_count < MAX_SYNC_ATTEMPTS:
            return False
        if self._sync_attempt_first is None:
            return False
        return dt_util.utcnow() - self._sync_attempt_first <= SYNC_ATTEMPT_WINDOW

    # ── Orchestration ───────────────────────────────────────────────────

    def _write_state(self) -> None:
        """Notify the entity to write its HA state."""
        if self._state_writer:
            self._state_writer()

    async def _async_check_and_sync(
        self, event: Event[EventStateChangedData] | None = None
    ) -> None:
        """Check sync state and perform operations if needed.

        Acquires asyncio.Lock — if already locked, discards (a sync is in
        progress and will read latest state when it runs).
        """
        if self._aio_lock.locked():
            return

        async with self._aio_lock:
            slot_state = self._resolve_slot_state(event)
            if slot_state is None:
                return

            expected_in_sync = self.calculate_in_sync(slot_state)

            # Initial load: set state without sync operations
            if self._in_sync is None:
                if slot_state.active_state not in (STATE_ON, STATE_OFF):
                    _LOGGER.debug(
                        "%s: Active entity has invalid state '%s'",
                        self._log_prefix,
                        slot_state.active_state,
                    )
                    return

                self._in_sync = expected_in_sync
                _LOGGER.debug(
                    "%s: Initial state loaded, in_sync=%s",
                    self._log_prefix,
                    expected_in_sync,
                )
                self._write_state()
                return

            # Out of sync: perform sync
            if not expected_in_sync:
                self._in_sync = False
                self._write_state()

                # Circuit breaker
                if (
                    slot_state.active_state == STATE_ON
                    and self._sync_attempts_exceeded()
                ):
                    _LOGGER.error(
                        "%s: Sync attempts exceeded (%s in %s window), disabling slot",
                        self._log_prefix,
                        self._sync_attempt_count,
                        SYNC_ATTEMPT_WINDOW,
                    )
                    await self._disable_slot(
                        f"Lock **{self._lock.lock.entity_id}**: slot "
                        f"**{self._slot_num}** failed to sync after "
                        f"{self._sync_attempt_count} consecutive attempts. "
                        f"The lock may be rejecting the code silently. "
                        f"Slot {self._slot_num} has been disabled. Check the "
                        f"code and re-enable the slot.",
                        title="Lock Code Sync Failed",
                    )
                    return

                sync_performed = await self._perform_sync(slot_state)

                if sync_performed:
                    try:
                        await self._coordinator.async_refresh()
                    except UpdateFailed as err:
                        _LOGGER.debug(
                            "%s: Coordinator refresh failed after sync, "
                            "scheduling retry: %s",
                            self._log_prefix,
                            err,
                        )
                        self._retry.schedule()

            # Re-check: slot may be back in sync after sync operation +
            # coordinator refresh, or from an external state change.
            # The coordinator listener can't run while we hold the lock,
            # so we re-read here.
            if not self._in_sync:
                post_state = self._resolve_slot_state(None)
                if post_state and self.calculate_in_sync(post_state):
                    self._in_sync = True
                    self._write_state()
                    self._retry.cancel()
                    self._reset_sync_tracker()

    async def _async_poll_sync(self) -> None:
        """Poll-driven sync check (called by async_update and OneShotRetry).

        Applies additional guards before delegating to _async_check_and_sync.
        """
        if self._in_sync is None:
            return

        if (
            self._aio_lock.locked()
            or self._in_sync
            or not (state := self._hass.states.get(self._lock.lock.entity_id))
            or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            or (not self._coordinator.last_update_success and not self._retry.active)
        ):
            return

        _LOGGER.debug(
            "Code slot %s on %s is out of sync, syncing now",
            self._slot_num,
            self._lock.lock.entity_id,
        )
        await self._async_check_and_sync()

    # ── State tracking subscriptions ────────────────────────────────────

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self._in_sync is not None and not self._aio_lock.locked():
            self._hass.async_create_task(
                self._async_check_and_sync(),
                name=(
                    f"lcm_sync_check_{self._lock.lock.entity_id}_slot_{self._slot_num}"
                ),
            )

    def _setup_coordinator_listener(self) -> None:
        """Subscribe to coordinator updates."""
        self._coordinator_unsub = self._coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    @callback
    def _cleanup_state_tracking(self) -> None:
        """Clean up state tracking subscription if one exists."""
        if self._state_tracking_unsub:
            self._state_tracking_unsub()
            self._state_tracking_unsub = None
            self._tracking_all_states = False

    @callback
    def _setup_state_tracking(self) -> None:
        """Set up state change tracking for dependent entities.

        If all entity IDs are available, tracks only those specific entities.
        Otherwise, tracks all state changes until entities become available.
        """
        self._cleanup_state_tracking()

        if self._build_entity_id_map():
            self._state_tracking_unsub = async_track_state_change_event(
                self._hass,
                self._tracked_entity_ids,
                self._async_check_and_sync,
            )
            self._tracking_all_states = False
        else:
            tracker = async_track_state_change_filtered(
                self._hass,
                TrackStates(True, set(), set()),
                self._async_check_and_sync,
            )
            self._state_tracking_unsub = tracker.async_remove
            self._tracking_all_states = True
            _LOGGER.debug(
                "%s: Waiting for dependent entities, tracking all state changes",
                self._log_prefix,
            )
