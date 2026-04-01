"""Slot sync manager — owns desired vs actual reconciliation.

Compares entity states (active, PIN, name) against coordinator data (actual lock
code) and drives set/clear operations to reconcile. Uses a periodic tick for
retries and circuit-breaking (disable slot after max failures).

Extracted from binary_sensor.py to separate domain logic from entity state display.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

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
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    SYNC_ATTEMPT_WINDOW,
    TICK_INTERVAL,
)
from .exceptions import CodeRejectedError, LockDisconnected
from .util import async_disable_slot

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
    """Manage sync state for a single lock x slot combination.

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
        state_writer: Callable[[], None],
    ) -> None:
        """Initialize the sync manager."""
        self._hass = hass
        self._ent_reg = ent_reg
        self._config_entry = config_entry
        self._coordinator = coordinator
        self._lock = lock
        self._slot_num = slot_num
        self._state_writer = state_writer

        self._log_prefix = (
            f"{config_entry.entry_id} ({config_entry.title}): "
            f"{lock.lock.entity_id} slot {slot_num}"
        )

        # Unique ID components for entity discovery
        entry_id = config_entry.entry_id
        lock_entity_id = lock.lock.entity_id
        base_uid = f"{entry_id}|{slot_num}"
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
        self._dirty: bool = False

        # Circuit breaker
        self._sync_attempt_count: int = 0
        self._sync_attempt_first: datetime | None = None

        # State tracking
        self._state_tracking_unsub: Callable[[], None] | None = None
        self._coordinator_unsub: Callable[[], None] | None = None
        self._tick_unsub: Callable[[], None] | None = None
        self._tracking_all_states: bool = False
        self._started = False

    @property
    def in_sync(self) -> bool | None:
        """Return current sync state (None = not yet determined)."""
        return self._in_sync

    async def async_start(self) -> None:
        """Start the sync manager -- discover entities, subscribe, initial tick."""
        if self._started:
            return
        self._started = True
        self._dirty = True
        self._setup_state_tracking()
        self._setup_coordinator_listener()
        self._tick_unsub = async_track_time_interval(
            self._hass, self._async_tick, TICK_INTERVAL
        )
        await self._async_tick()

    def async_stop(self) -> None:
        """Stop the sync manager -- unsubscribe tick and listeners. Idempotent."""
        if not self._started:
            return
        self._started = False
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None
        self._cleanup_state_tracking()
        if self._coordinator_unsub:
            self._coordinator_unsub()
            self._coordinator_unsub = None
        self._reset_sync_tracker()

    # -- State resolution ----------------------------------------------------

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

    def _resolve_slot_state(self) -> SlotState | None:
        """Resolve slot state from current entity and coordinator data.

        All state reads are sync (hass.states.get) with no awaits between them,
        ensuring atomicity on Home Assistant's single-threaded event loop.
        """
        if not self._build_entity_id_map():
            return None

        # NOTE: We intentionally don't upgrade from catch-all to targeted tracking
        # here. Modifying subscriptions from within a state change callback causes
        # timing issues. The catch-all has early-return guards that skip irrelevant
        # entities, so the performance impact is minimal.

        if int(self._slot_num) not in self._coordinator.data:
            _LOGGER.debug(
                "%s: Slot not in coordinator data, skipping",
                self._log_prefix,
            )
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

    # -- Sync calculation ----------------------------------------------------

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

    # -- Sync execution ------------------------------------------------------

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
                # Only track set operations -- clears always succeed and
                # shouldn't count toward the sync failure limit
                self._record_sync_attempt()
                _LOGGER.debug("%s: Set usercode", self._log_prefix)
            else:
                await self._lock.async_internal_clear_usercode(
                    int(self._slot_num), source="sync"
                )
                _LOGGER.debug("%s: Cleared usercode", self._log_prefix)
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
            self._dirty = True
            return False
        except Exception:
            _LOGGER.exception(
                "%s: Unexpected error during %s usercode",
                self._log_prefix,
                "set" if slot_state.active_state == STATE_ON else "clear",
            )
            # Count toward circuit breaker so persistent errors eventually
            # disable the slot instead of retrying forever
            self._record_sync_attempt()
            self._dirty = True
            return False

    async def _disable_slot(self, reason: str, title: str) -> None:
        """Disable the slot and create a persistent notification."""
        await async_disable_slot(
            self._hass,
            self._ent_reg,
            self._config_entry.entry_id,
            self._slot_num,
            reason=reason,
            title=title,
            lock_name=self._lock.display_name,
            lock_entity_id=self._lock.lock.entity_id,
        )
        self._reset_sync_tracker()

    # -- Attempt tracking + circuit breaker ----------------------------------

    def _reset_sync_tracker(self) -> None:
        """Reset the sync attempt tracker."""
        self._sync_attempt_count = 0
        self._sync_attempt_first = None

    def _record_sync_attempt(self) -> None:
        """Record a sync attempt (successful or failed, counts toward circuit breaker)."""
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

    # -- Orchestration -------------------------------------------------------

    def _write_state(self) -> None:
        """Notify the entity to write its Home Assistant state."""
        self._state_writer()

    @callback
    def _mark_dirty(self, *_args: Any) -> None:
        """Mark slot as needing sync check on next tick."""
        self._dirty = True

    @callback
    def _mark_dirty_if_relevant(self, event: Event[EventStateChangedData]) -> None:
        """Mark dirty only if the state change is for a tracked entity.

        Used by the catch-all state tracking fallback to avoid setting dirty
        on every HA state change. Falls back to always-dirty if entity IDs
        haven't been discovered yet.
        """
        if not self._tracked_entity_ids or (
            event.data["entity_id"] in self._tracked_entity_ids
        ):
            self._dirty = True

    async def _async_tick(self, _now: datetime | None = None) -> None:
        """Periodic reconciliation tick."""
        if not self._started or not self._dirty:
            return
        self._dirty = False

        slot_state = self._resolve_slot_state()
        if slot_state is None:
            self._dirty = True  # retry next tick
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
                self._dirty = True  # retry next tick
                return

            self._in_sync = expected_in_sync
            _LOGGER.debug(
                "%s: Initial state loaded, in_sync=%s",
                self._log_prefix,
                expected_in_sync,
            )
            self._write_state()
            if not expected_in_sync:
                self._dirty = True  # schedule sync on next tick
            return

        # Out of sync: perform sync
        if not expected_in_sync:
            self._in_sync = False
            self._write_state()

            # Circuit breaker
            if slot_state.active_state == STATE_ON and self._sync_attempts_exceeded():
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

            # Refresh coordinator to verify sync completed. Skip for push
            # providers — they update coordinator optimistically via push_update()
            # and refreshing from cache could read stale data.
            if sync_performed and not self._lock.supports_push:
                try:
                    await self._coordinator.async_refresh()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug(
                        "%s: Coordinator refresh failed after sync, "
                        "will retry next tick",
                        self._log_prefix,
                        exc_info=True,
                    )
                    self._dirty = True
            return

        # Back in sync
        if not self._in_sync:
            self._in_sync = True
            self._write_state()
            self._reset_sync_tracker()

    # -- State tracking subscriptions ----------------------------------------

    def _setup_coordinator_listener(self) -> None:
        """Subscribe to coordinator updates."""
        self._coordinator_unsub = self._coordinator.async_add_listener(self._mark_dirty)

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
        Otherwise, tracks all state changes via a catch-all subscription that
        filters by tracked entity IDs in _mark_dirty_if_relevant. The catch-all
        is not upgraded to targeted tracking later (modifying subscriptions from
        within a callback causes timing issues).
        """
        self._cleanup_state_tracking()

        if self._build_entity_id_map():
            self._state_tracking_unsub = async_track_state_change_event(
                self._hass,
                self._tracked_entity_ids,
                self._mark_dirty,
            )
            self._tracking_all_states = False
        else:
            tracker = async_track_state_change_filtered(
                self._hass,
                TrackStates(True, set(), set()),
                self._mark_dirty_if_relevant,
            )
            self._state_tracking_unsub = tracker.async_remove
            self._tracking_all_states = True
            _LOGGER.debug(
                "%s: Waiting for dependent entities, tracking all state changes",
                self._log_prefix,
            )
