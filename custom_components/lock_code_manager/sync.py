"""Slot sync manager — owns desired vs actual reconciliation.

Compares entity states (active, PIN, name) against coordinator data (actual lock
code) and drives set/clear operations to reconcile. Uses a periodic tick for
retries and circuit-breaking (disable slot after max failures).

Tick interval: 5 seconds (TICK_INTERVAL)
Circuit breaker: 3 attempts within 5 minutes (MAX_SYNC_ATTEMPTS, SYNC_ATTEMPT_WINDOW)

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
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
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
from .exceptions import CodeRejectedError, LockDisconnected, LockOperationFailed
from .models import SlotCode
from .util import async_disable_slot

if TYPE_CHECKING:
    from .coordinator import LockUsercodeUpdateCoordinator
    from .models import LockCodeManagerConfigEntry
    from .providers import BaseLock


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotState:
    """Snapshot of entity states for a slot on a specific lock.

    Used by SlotSyncManager to compare desired (entity) vs actual
    (coordinator) state. Includes raw HA state strings (rather than
    parsed values) because sync logic needs to distinguish between
    "off" and "unavailable" — both look like the same parsed bool but
    mean different things for retry decisions.
    """

    active_state: str
    pin_state: str
    name_state: str | None
    code_state: str
    coordinator_code: str | SlotCode | None


class SlotSyncManager:
    """Manage sync state for a single lock x slot combination.

    Compares desired state (from entity states: active, PIN) against actual
    state (from coordinator data) and drives set/clear operations to reconcile.

    The manager discovers entity IDs from the entity registry, subscribes to
    their state changes and coordinator updates, and drives sync operations
    via periodic ticks. State changes mark the slot dirty; reconciliation
    happens on the next tick. Includes circuit breaker protection that
    disables the slot after repeated sync failures within a time window.

    The in-sync binary sensor entity reads manager.in_sync for display.

    Lifecycle methods (async_start, async_stop) are idempotent and re-entrant.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        coordinator: LockUsercodeUpdateCoordinator,
        lock: BaseLock,
        slot_num: int,
        state_writer: Callable[[bool | None], None],
    ) -> None:
        """Initialize the sync manager."""
        self._hass = hass
        self._ent_reg = ent_reg
        self._config_entry = config_entry
        self._coordinator = coordinator
        self._lock = lock
        self._slot_num = int(slot_num)
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

        # Track the last PIN we successfully set, so we can detect when the
        # configured PIN changes while the lock code is UNKNOWN (masked/write-only).
        # This is in-memory only — on restart, _last_set_pin is None, which means
        # UNKNOWN slots will be treated as out-of-sync and re-set. This is the
        # safest behavior: it guarantees the lock has the correct PIN even if the
        # config changed while HA was down, at the cost of one extra set per
        # masked/write-only slot on every restart.
        self._last_set_pin: str | None = None

        # Circuit breaker
        self._sync_attempt_count: int = 0
        self._sync_attempt_first: datetime | None = None

        # Invalid state tracking (for initial load)
        self._invalid_state_count: int = 0

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

        if self._slot_num not in self._coordinator.data:
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

        coordinator_code = self._coordinator.data.get(self._slot_num)
        return SlotState(
            active_state=active_state,
            pin_state=pin_state,
            name_state=name_state,
            code_state=code_state,
            coordinator_code=coordinator_code,
        )

    # -- Sync calculation ----------------------------------------------------

    def calculate_in_sync(self, slot_state: SlotState) -> bool:
        """Calculate whether slot should be in sync.

        Active (state=ON): PIN should match code on lock.
        Inactive (state=OFF): Code on lock should be empty.

        For UNKNOWN codes (masked/write-only): in sync only if the configured
        PIN matches what we last successfully set. This ensures that PIN
        changes trigger a re-set even when the lock code is unreadable, and
        that taking over a slot with an existing masked code triggers a set.
        """
        lock_code = (
            slot_state.coordinator_code
            if slot_state.coordinator_code is not None
            else slot_state.code_state
        )
        if slot_state.active_state == STATE_ON:
            if lock_code is SlotCode.UNREADABLE_CODE:
                return slot_state.pin_state == self._last_set_pin
            if lock_code is SlotCode.EMPTY:
                return False  # need to set
            return slot_state.pin_state == lock_code
        # active_state == STATE_OFF: slot should be cleared
        # The "" check covers the fallback path where coordinator_code is None
        # and lock_code comes from the code sensor entity state (which returns ""
        # for SlotCode.EMPTY).
        return lock_code is SlotCode.EMPTY or lock_code == ""

    # -- Sync execution ------------------------------------------------------

    async def _perform_sync(self, slot_state: SlotState) -> None:
        """Execute sync operation (set or clear usercode).

        Raises CodeRejectedError, LockDisconnected, or propagates any exception.
        Error handling is done by the caller (_async_tick).
        """
        if slot_state.active_state == STATE_ON:
            await self._lock.async_internal_set_usercode(
                self._slot_num,
                slot_state.pin_state,
                slot_state.name_state,
                source="sync",
            )
            self._last_set_pin = slot_state.pin_state
            # Track set operations toward circuit breaker. Clear operations
            # don't increment the counter (expected to always succeed).
            self._record_sync_attempt()
            _LOGGER.debug("%s: Set usercode", self._log_prefix)
        else:
            await self._lock.async_internal_clear_usercode(
                self._slot_num, source="sync"
            )
            self._last_set_pin = None
            _LOGGER.debug("%s: Cleared usercode", self._log_prefix)

    async def _disable_slot(self, reason: str) -> None:
        """Disable the slot and create a repair issue."""
        try:
            await async_disable_slot(
                self._hass,
                self._ent_reg,
                self._config_entry.entry_id,
                self._slot_num,
                reason=reason,
                lock_name=self._lock.display_name,
                lock_entity_id=self._lock.lock.entity_id,
            )
        except Exception:
            _LOGGER.exception(
                "%s: Failed to disable slot via service call",
                self._log_prefix,
            )
            # Fallback: create repair issue directly so the user is notified
            # even though the switch service call failed
            async_create_issue(
                self._hass,
                DOMAIN,
                f"slot_disabled_{self._config_entry.entry_id}_{self._slot_num}",
                is_fixable=True,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="slot_disabled",
                translation_placeholders={
                    "slot_num": str(self._slot_num),
                    "reason": reason,
                },
            )
        finally:
            self._reset_sync_tracker()

    # -- Attempt tracking + circuit breaker ----------------------------------

    def _reset_sync_tracker(self) -> None:
        """Reset the sync attempt tracker."""
        self._sync_attempt_count = 0
        self._sync_attempt_first = None
        self._invalid_state_count = 0

    def _record_sync_attempt(self) -> None:
        """Record a sync attempt toward circuit breaker counter.

        Called for set operations. Successful clear operations and transient
        LockDisconnected errors are not tracked since they represent
        transient lock communication issues, not persistent failures.
        """
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
        """Notify the entity to write Home Assistant state."""
        self._state_writer(self.in_sync)

    @callback
    def _mark_dirty(self, *_args: Any) -> None:
        """Mark slot as needing sync check on next tick.

        Also immediately updates the display state (in_sync) if it changed,
        so the UI reflects changes without waiting for the next tick.
        """
        self._dirty = True
        self._update_in_sync_display()

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
            self._update_in_sync_display()

    @callback
    def _update_in_sync_display(self) -> None:
        """Update the in_sync display state immediately if it changed.

        This is a read-only check — no sync operations are performed.
        Allows the UI to reflect state changes instantly while the tick
        handles actual sync operations.
        """
        if self._in_sync is None:
            return
        slot_state = self._resolve_slot_state()
        if slot_state is None:
            return
        expected = self.calculate_in_sync(slot_state)
        if expected != self._in_sync:
            self._in_sync = expected
            if expected:
                self._reset_sync_tracker()
            self._write_state()

    async def _async_tick(self, _now: datetime | None = None) -> None:
        """Periodic reconciliation tick."""
        if not self._started:
            return

        # Try upgrading before the dirty check — catch-all mode may prevent
        # dirty from being set for entities not yet in _tracked_entity_ids
        self._try_upgrade_state_tracking()

        if not self._dirty:
            return

        self._dirty = False
        await self._async_tick_impl()

    async def _async_tick_impl(self) -> None:
        """Core tick logic — called from _async_tick after dirty check."""
        slot_state = self._resolve_slot_state()
        if slot_state is None:
            # State resolution failed — retry on next tick. We always retry
            # (not just during initial load) because the coordinator may not
            # have data yet for a newly enabled slot.
            self._dirty = True
            return

        expected_in_sync = self.calculate_in_sync(slot_state)

        # Initial load: detect sync state without performing sync operations.
        # This prevents premature sync attempts during entity setup when dependent
        # entities (active, code sensor) may not yet be registered. If out of sync,
        # we schedule a sync for the next tick after entities are ready.
        if self._in_sync is None:
            if slot_state.active_state not in (STATE_ON, STATE_OFF):
                self._invalid_state_count += 1

                # Exact `== MAX+1` (not `>=`) is intentional: we log the
                # warning exactly once. Subsequent invalid-state ticks fall
                # through to neither branch and stay silent to avoid spam.
                if self._invalid_state_count == MAX_SYNC_ATTEMPTS + 1:
                    _LOGGER.warning(
                        "%s: Active entity has invalid state '%s' for %s consecutive checks. "
                        "Entity may be unavailable or misconfigured. Check that the active "
                        "binary_sensor entity exists and is in a valid state (ON/OFF).",
                        self._log_prefix,
                        slot_state.active_state,
                        self._invalid_state_count,
                    )
                elif self._invalid_state_count <= MAX_SYNC_ATTEMPTS:
                    _LOGGER.debug(
                        "%s: Active entity has invalid state '%s' (attempt %s), waiting...",
                        self._log_prefix,
                        slot_state.active_state,
                        self._invalid_state_count,
                    )
                self._dirty = True  # retry next tick
                return

            self._in_sync = expected_in_sync
            _LOGGER.debug(
                "%s: Initial state loaded, in_sync=%s",
                self._log_prefix,
                expected_in_sync,
            )
            if expected_in_sync and slot_state.active_state == STATE_ON:
                # Clear any persisted slot_disabled issue from before restart,
                # but only if the slot is enabled — a disabled slot can be
                # "in sync" (no code desired, no code on lock) without the
                # circuit breaker condition being resolved.
                async_delete_issue(
                    self._hass,
                    DOMAIN,
                    f"slot_disabled_{self._config_entry.entry_id}_{self._slot_num}",
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
                )
                return

            # Perform sync operation (may raise)
            try:
                await self._perform_sync(slot_state)
            except CodeRejectedError as err:
                _LOGGER.error("%s: Code rejected: %s", self._log_prefix, err)
                await self._disable_slot(
                    f"Lock **{err.lock_entity_id}**: slot **{err.code_slot}** "
                    f"has been disabled. {err}\n\n"
                    f"Fix the issue and re-enable the slot.",
                )
                return
            except (LockDisconnected, LockOperationFailed) as err:
                _LOGGER.info(
                    "%s: Lock disconnected during %s usercode: %s. Will retry on next tick.",
                    self._log_prefix,
                    "set" if slot_state.active_state == STATE_ON else "clear",
                    err,
                )
                self._dirty = True
                return
            except Exception as err:
                _LOGGER.exception(
                    "%s: Unexpected error during %s usercode - this may indicate a bug. "
                    "Slot will be disabled to prevent infinite retry loop. Error: %s: %s",
                    self._log_prefix,
                    "set" if slot_state.active_state == STATE_ON else "clear",
                    type(err).__name__,
                    err,
                )
                # For unexpected errors, disable the slot immediately rather than
                # retrying forever. This prevents infinite retries of programming errors.
                await self._disable_slot(
                    f"Lock **{self._lock.lock.entity_id}**: slot **{self._slot_num}** "
                    f"encountered an unexpected error during sync. This may indicate a bug "
                    f"in the lock code manager integration. Check logs for details and "
                    f"report this issue.\n\nError: {type(err).__name__}: {err}",
                )
                return
            else:
                # Sync succeeded — reset circuit breaker immediately so a
                # subsequent PIN change doesn't inherit stale attempt counts.
                self._reset_sync_tracker()
                # Refresh coordinator to verify. Skip for push providers —
                # they update coordinator optimistically via push_update()
                # and refreshing from cache could read stale data.
                if not self._lock.supports_push:
                    try:
                        await self._coordinator.async_refresh()
                    except Exception:  # noqa: BLE001
                        _LOGGER.exception(
                            "%s: Coordinator refresh failed after sync operation. "
                            "Sync may have succeeded but verification failed. "
                            "Will retry on next tick.",
                            self._log_prefix,
                        )
                        # Still mark dirty so we retry, but log at ERROR level
                        self._dirty = True

            return

        # Back in sync
        if not self._in_sync:
            self._in_sync = True
            self._write_state()
            self._reset_sync_tracker()
            # Only clear slot_disabled when the slot is enabled — a disabled
            # slot can be "in sync" without the issue being resolved
            if slot_state.active_state == STATE_ON:
                async_delete_issue(
                    self._hass,
                    DOMAIN,
                    f"slot_disabled_{self._config_entry.entry_id}_{self._slot_num}",
                )

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
    def _try_upgrade_state_tracking(self) -> None:
        """Upgrade catch-all state tracking to targeted if entities are now available.

        Called at the start of each tick (outside any callback), so it is safe
        to modify subscriptions here without timing issues.
        """
        if not self._tracking_all_states or not self._build_entity_id_map():
            return

        assert self._state_tracking_unsub is not None
        self._state_tracking_unsub()
        self._state_tracking_unsub = async_track_state_change_event(
            self._hass,
            self._tracked_entity_ids,
            self._mark_dirty,
        )
        self._tracking_all_states = False
        _LOGGER.debug(
            "%s: Upgraded from catch-all to targeted state tracking",
            self._log_prefix,
        )

    @callback
    def _setup_state_tracking(self) -> None:
        """Set up state change tracking for dependent entities.

        If all entity IDs are available, tracks only those specific entities.
        Otherwise, tracks all state changes via a catch-all subscription that
        filters by tracked entity IDs in _mark_dirty_if_relevant. The catch-all
        is upgraded to targeted tracking in _try_upgrade_state_tracking(), which
        runs at the start of each tick (safe because it's outside a callback).
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
