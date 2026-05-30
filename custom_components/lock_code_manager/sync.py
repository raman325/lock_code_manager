"""
Slot sync manager — owns desired vs actual reconciliation.

Compares entity states (active, PIN, name) against coordinator data (actual lock
code) and drives set/clear operations to reconcile. Uses a state machine
(SyncState enum) with periodic tick for retries and per-lock suspension on
repeated failures.

Tick interval: 2 seconds (TICK_INTERVAL)
Circuit breaker: 3 attempts within 5 minutes (MAX_SYNC_ATTEMPTS, SYNC_ATTEMPT_WINDOW)
"""

from __future__ import annotations

import asyncio
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

from .const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    SYNC_ATTEMPT_WINDOW,
    TICK_INTERVAL,
)
from .data import build_slot_unique_id
from .exceptions import CodeRejectedError, LockDisconnected, LockOperationFailed
from .models import SlotCode, SyncState
from .resilience import CircuitBreaker
from .util import async_disable_slot

if TYPE_CHECKING:
    from .coordinator import LockUsercodeUpdateCoordinator
    from .models import LockCodeManagerConfigEntry
    from .providers import BaseLock


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotState:
    """
    Snapshot of entity states for a slot on a specific lock.

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
    """
    Manage sync state for a single lock x slot combination.

    Compares desired state (from entity states: active, PIN) against actual
    state (from coordinator data) and drives set/clear operations to reconcile.

    Uses a state machine (SyncState) with five states: LOADING, IN_SYNC,
    OUT_OF_SYNC, SYNCING, SUSPENDED. State changes mark the slot for
    re-evaluation; reconciliation happens on the next tick. Includes circuit
    breaker protection that suspends the lock after repeated sync failures.

    The in-sync binary sensor entity reads manager.in_sync and
    manager.sync_status for display.

    State mutation rules:
        - ``_request_sync_check`` transitions IN_SYNC -> OUT_OF_SYNC or
          SUSPENDED -> OUT_OF_SYNC for immediate UI feedback.
        - ``_async_tick_impl`` is the single authoritative place for all
          other state transitions, circuit breaker, sync operations,
          and ``_last_set_pin`` changes.

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
        self._unique_ids: dict[str, tuple[str, str]] = {
            CONF_PIN: (TEXT_DOMAIN, build_slot_unique_id(entry_id, slot_num, CONF_PIN)),
            CONF_NAME: (
                TEXT_DOMAIN,
                build_slot_unique_id(entry_id, slot_num, CONF_NAME),
            ),
            ATTR_ACTIVE: (
                BINARY_SENSOR_DOMAIN,
                build_slot_unique_id(entry_id, slot_num, ATTR_ACTIVE),
            ),
            ATTR_CODE: (
                SENSOR_DOMAIN,
                build_slot_unique_id(entry_id, slot_num, ATTR_CODE, lock_entity_id),
            ),
        }

        # Sync state machine — single source of truth replacing _dirty,
        # _in_sync, and _tick_in_sync.
        self._state: SyncState = SyncState.LOADING
        self._entity_id_map: dict[str, str] = {}
        self._tracked_entity_ids: set[str] = set()

        # Track the last PIN we successfully set, so we can detect when the
        # configured PIN changes while the lock code is UNKNOWN (masked/write-only).
        # This is in-memory only — on restart, _last_set_pin is None, which means
        # UNKNOWN slots will be treated as out-of-sync and re-set. This is the
        # safest behavior: it guarantees the lock has the correct PIN even if the
        # config changed while HA was down, at the cost of one extra set per
        # masked/write-only slot on every restart.
        self._last_set_pin: str | None = None

        # Slot-level circuit breaker: trips when a code repeatedly fails to
        # converge within the window, suspending just this lock and slot.
        self._slot_breaker = CircuitBreaker(
            MAX_SYNC_ATTEMPTS, window=SYNC_ATTEMPT_WINDOW
        )

        # The desired target (active_state, pin_state) captured when the slot
        # is suspended for a non-converging code or an unexpected error. While
        # set, the slot stays suspended until that target changes (user edits
        # the PIN or toggles the slot) or it returns to sync -- it does NOT
        # resume on unrelated coordinator updates. None for a slot that is not
        # suspended, or that is suspended only because the lock is unreachable.
        self._code_suspend_target: tuple[str, str] | None = None

        # Invalid state tracking (for initial load)
        self._logged_invalid_state: bool = False

        # State tracking
        self._state_tracking_unsub: Callable[[], None] | None = None
        self._coordinator_unsub: Callable[[], None] | None = None
        self._tick_unsub: Callable[[], None] | None = None
        self._tracking_all_states: bool = False
        self._started = False

        # All currently-executing _async_tick tasks. A new tick can fire from
        # the interval timer while a prior tick is still awaiting
        # ``_perform_sync`` or ``coordinator.async_refresh``; tracking every
        # in-flight tick lets async_stop await them all before tearing down
        # state. Tasks self-register on entry and self-discard on exit.
        self._tick_tasks: set[asyncio.Task[None]] = set()

    @property
    def in_sync(self) -> bool | None:
        """Return current sync state (None = not yet determined)."""
        if self._state is SyncState.LOADING:
            return None
        return self._state is SyncState.IN_SYNC

    @property
    def sync_status(self) -> str | None:
        """Return granular sync status for dashboard display."""
        if self._state is SyncState.LOADING:
            return None
        return self._state.value

    async def async_start(self) -> None:
        """Start the sync manager -- discover entities, subscribe, initial tick."""
        if self._started:
            return
        self._started = True
        self._setup_state_tracking()
        self._setup_coordinator_listener()
        self._tick_unsub = async_track_time_interval(
            self._hass, self._async_tick, TICK_INTERVAL
        )
        await self._async_tick()

    async def async_stop(self) -> None:
        """
        Stop the sync manager, awaiting any in-flight ticks.

        Idempotent. Unsubscribes the timer and state listeners first so no
        new ticks can start, then awaits any in-flight ticks so they cannot
        continue to call ``_perform_sync``, ``coordinator.async_refresh``,
        or ``_write_state()`` after stop returns. We do not cancel them --
        a tick mid ``_perform_sync`` should be allowed to finish so the lock
        operation completes; ``_started=False`` keeps it from scheduling
        more work.
        """
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

        current = asyncio.current_task()
        pending = {
            task for task in self._tick_tasks if task is not current and not task.done()
        }
        if pending:
            tick_results = await asyncio.gather(*pending, return_exceptions=True)
            for result in tick_results:
                if isinstance(result, Exception) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    _LOGGER.warning(
                        "%s: In-flight tick raised during stop: %s",
                        self._log_prefix,
                        result,
                        exc_info=result,
                    )

        self._slot_breaker.reset()

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
        """
        Ensure all dependent entities exist with valid states.

        The name entity (CONF_NAME) is optional — STATE_UNKNOWN is its
        normal state when no name is configured.

        When the slot is inactive (active entity is OFF), PIN and code
        sensor entities are allowed to be STATE_UNKNOWN since the sync
        manager only needs to know the slot is off to proceed (clear or
        confirm already cleared). This prevents disabled slots from
        being stuck in LOADING forever.
        """
        # Collect all states in a single pass
        states: dict[str, str | None] = {}
        for key in (CONF_PIN, CONF_NAME, ATTR_ACTIVE, ATTR_CODE):
            if key not in self._entity_id_map:
                return False
            state = self._get_entity_state(key)
            if state is None or state == STATE_UNAVAILABLE:
                _LOGGER.debug("%s: Waiting for %s state", self._log_prefix, key)
                return False
            states[key] = state

        # Active entity must always have a definite state
        if states[ATTR_ACTIVE] == STATE_UNKNOWN:
            _LOGGER.debug("%s: Waiting for %s state", self._log_prefix, ATTR_ACTIVE)
            return False

        # Name is always optional (STATE_UNKNOWN = no name configured)
        # PIN and code sensor can be unknown when the slot is inactive
        slot_inactive = states[ATTR_ACTIVE] == STATE_OFF
        for key in (CONF_PIN, ATTR_CODE):
            if states[key] == STATE_UNKNOWN and not slot_inactive:
                _LOGGER.debug("%s: Waiting for %s state", self._log_prefix, key)
                return False

        return True

    def _resolve_slot_state(self) -> SlotState | None:
        """
        Resolve slot state from current entity and coordinator data.

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

        # _ensure_entities_ready guarantees these three are non-None (the name
        # entity is optional). No awaits run between that check and these reads,
        # so the states cannot change underneath us on the single-threaded loop.
        active_state = self._get_entity_state(ATTR_ACTIVE)
        pin_state = self._get_entity_state(CONF_PIN)
        name_state = self._get_entity_state(CONF_NAME)
        code_state = self._get_entity_state(ATTR_CODE)
        assert active_state is not None
        assert pin_state is not None
        assert code_state is not None

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
        """
        Calculate whether slot should be in sync.

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
                # If we recently set a PIN on this slot and it matches the
                # configured PIN, trust the set — the provider may not have
                # caught up yet (eventual consistency, e.g. Schlage cloud API).
                return (
                    self._last_set_pin is not None
                    and slot_state.pin_state == self._last_set_pin
                )
            return slot_state.pin_state == lock_code
        # active_state == STATE_OFF: slot should be cleared
        # The "" check covers the fallback path where coordinator_code is None
        # and lock_code comes from the code sensor entity state (which returns ""
        # for SlotCode.EMPTY).
        return lock_code is SlotCode.EMPTY or lock_code == ""

    # -- Sync execution ------------------------------------------------------

    async def _perform_sync(self, slot_state: SlotState) -> None:
        """
        Execute sync operation (set or clear usercode).

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
            # Track set operations toward the slot breaker. Clear operations
            # don't increment the counter (expected to always succeed).
            self._slot_breaker.record_failure()
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
            self._slot_breaker.reset()

    def _suspend_slot(self, slot_state: SlotState, reason: str) -> None:
        """
        Suspend this lock and slot and create a per-slot repair issue.

        Records the desired target so the slot stays suspended until that
        target changes or it returns to sync, rather than resuming on
        unrelated coordinator updates.
        """
        self._state = SyncState.SUSPENDED
        self._code_suspend_target = (slot_state.active_state, slot_state.pin_state)
        self._slot_breaker.reset()
        self._write_state()

        issue_id = (
            f"slot_suspended_{self._config_entry.entry_id}_"
            f"{self._lock.lock.entity_id}_{self._slot_num}"
        )
        async_create_issue(
            self._hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="slot_suspended",
            translation_placeholders={
                "lock_entity_id": self._lock.lock.entity_id,
                "lock_name": self._lock.display_name or self._lock.lock.entity_id,
                "slot_num": str(self._slot_num),
                "reason": reason,
            },
        )

    def _clear_resolved_issues(self, slot_state: SlotState) -> None:
        """
        Clear repair issues that no longer apply now that the slot is in sync.

        The per-slot ``slot_disabled`` issue is cleared only when the slot is
        active (an inactive slot was never meant to hold a code, so a lingering
        disabled issue there is unrelated). The per-lock ``slot_suspended``
        issue is cleared regardless of active state.
        """
        entry_id = self._config_entry.entry_id
        if slot_state.active_state == STATE_ON:
            async_delete_issue(
                self._hass,
                DOMAIN,
                f"slot_disabled_{entry_id}_{self._slot_num}",
            )
        async_delete_issue(
            self._hass,
            DOMAIN,
            f"slot_suspended_{entry_id}_{self._lock.lock.entity_id}_{self._slot_num}",
        )

    # -- Orchestration -------------------------------------------------------

    def _write_state(self) -> None:
        """Notify the entity to write Home Assistant state."""
        # Skip if stopped: a tick mid-await may still call _write_state after
        # async_stop has begun teardown of the owning entity.
        if not self._started:
            return
        self._state_writer(self.in_sync)

    @callback
    def _request_sync_check(self, *_args: Any) -> None:
        """
        Request a sync check on the next tick.

        Transitions IN_SYNC -> OUT_OF_SYNC if calculate_in_sync returns False
        (also resets circuit breaker since the sync target changed).
        Transitions SUSPENDED -> OUT_OF_SYNC if the coordinator is no longer suspended.
        No-op for LOADING, OUT_OF_SYNC, SYNCING.
        """
        if self._state is SyncState.IN_SYNC:
            slot_state = self._resolve_slot_state()
            if slot_state is not None and not self.calculate_in_sync(slot_state):
                self._state = SyncState.OUT_OF_SYNC
                self._slot_breaker.reset()
                self._write_state()
        elif self._state is SyncState.SUSPENDED:
            if self._code_suspend_target is not None:
                # Suspended for a non-converging code or an unexpected error.
                # Only retry once the desired target changes (e.g. the user
                # edits the PIN or toggles the slot) or the slot returns to
                # sync on its own -- otherwise stay suspended so we don't
                # hammer the lock with a code it keeps rejecting.
                slot_state = self._resolve_slot_state()
                if slot_state is not None and (
                    (slot_state.active_state, slot_state.pin_state)
                    != self._code_suspend_target
                    or self.calculate_in_sync(slot_state)
                ):
                    self._slot_breaker.reset()
                    self._code_suspend_target = None
                    self._state = SyncState.OUT_OF_SYNC
                    self._write_state()
            elif not self._coordinator.unreachable:
                # Suspended because the lock was unreachable; it is reachable
                # again, so resume.
                self._slot_breaker.reset()
                self._state = SyncState.OUT_OF_SYNC
                self._write_state()

    @callback
    def _request_sync_check_if_relevant(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """
        Request sync check only if the state change is for a tracked entity.

        Used by the catch-all state tracking fallback to avoid unnecessary
        checks on every HA state change. Falls back to always-checking if
        entity IDs haven't been discovered yet.
        """
        if not self._tracked_entity_ids or (
            event.data["entity_id"] in self._tracked_entity_ids
        ):
            self._request_sync_check()

    async def _async_tick(self, _now: datetime | None = None) -> None:
        """Periodic reconciliation tick."""
        if not self._started:
            return

        # Register before the first await so a concurrent ``async_stop``
        # sees this tick in ``_tick_tasks``.
        task = asyncio.current_task()
        if task is None:
            return
        self._tick_tasks.add(task)
        try:
            # Try upgrading before the state check — catch-all mode may prevent
            # _request_sync_check from firing for entities not yet tracked
            self._try_upgrade_state_tracking()

            if self._state in (
                SyncState.IN_SYNC,
                SyncState.SYNCING,
                SyncState.SUSPENDED,
            ):
                return

            await self._async_tick_impl()
        finally:
            self._tick_tasks.discard(task)

    async def _async_tick_impl(self) -> None:
        """
        Core tick logic — called from _async_tick for LOADING and OUT_OF_SYNC states.

        This is the single authoritative place for all sync state mutations:
        circuit breaker tracking, sync operations, and ``_last_set_pin``
        changes.
        """
        slot_state = self._resolve_slot_state()
        if slot_state is None:
            # State resolution failed — stay in current state and retry.
            return

        expected_in_sync = self.calculate_in_sync(slot_state)

        # -- LOADING: detect initial sync state without performing operations --
        if self._state is SyncState.LOADING:
            if slot_state.active_state not in (STATE_ON, STATE_OFF):
                if not self._logged_invalid_state:
                    _LOGGER.debug(
                        "%s: Active entity has invalid state '%s', waiting "
                        "for valid state (ON/OFF)",
                        self._log_prefix,
                        slot_state.active_state,
                    )
                    self._logged_invalid_state = True
                return

            if expected_in_sync:
                self._state = SyncState.IN_SYNC
                self._clear_resolved_issues(slot_state)
            else:
                self._state = SyncState.OUT_OF_SYNC

            _LOGGER.debug(
                "%s: Initial state loaded, state=%s",
                self._log_prefix,
                self._state.value,
            )
            self._write_state()
            return

        # -- OUT_OF_SYNC: check lock reachability, then attempt sync --
        if self._coordinator.unreachable:
            self._state = SyncState.SUSPENDED
            self._write_state()
            return

        if expected_in_sync:
            # Became in sync without us doing anything (external change)
            self._state = SyncState.IN_SYNC
            self._slot_breaker.reset()
            self._write_state()
            self._clear_resolved_issues(slot_state)
            return

        # Circuit breaker check: too many failed sync attempts (a set that
        # never converges, or repeated LockOperationFailed) suspends the slot.
        if self._slot_breaker.tripped:
            _LOGGER.error(
                "%s: Sync attempts exceeded (%s in %s window), suspending slot",
                self._log_prefix,
                self._slot_breaker.failure_count,
                SYNC_ATTEMPT_WINDOW,
            )
            self._suspend_slot(
                slot_state,
                f"Lock **{self._lock.lock.entity_id}**: slot "
                f"**{self._slot_num}** failed to sync after "
                f"{self._slot_breaker.failure_count} consecutive attempts. "
                f"The lock may be rejecting the code silently or "
                f"experiencing communication issues. "
                f"Sync has been suspended for this slot. It will resume "
                f"automatically once the lock accepts the code or you change "
                f"the PIN for this slot.",
            )
            return

        # Perform sync
        self._state = SyncState.SYNCING
        self._write_state()
        try:
            await self._perform_sync(slot_state)
        except CodeRejectedError as err:
            _LOGGER.error("%s: Code rejected: %s", self._log_prefix, err)
            await self._disable_slot(
                f"Lock **{err.lock_entity_id}**: slot **{err.code_slot}** "
                f"has been disabled. {err}\n\n"
                f"Fix the issue and re-enable the slot.",
            )
            # After disable, the slot active switch turns off. The next
            # _request_sync_check will see the slot as in-sync (no code
            # desired, no code on lock). Set to OUT_OF_SYNC so the next
            # tick resolves to IN_SYNC.
            self._state = SyncState.OUT_OF_SYNC
            return
        except LockDisconnected as err:
            _LOGGER.info(
                "%s: Lock unreachable during %s usercode: %s. Will retry on next tick.",
                self._log_prefix,
                "set" if slot_state.active_state == STATE_ON else "clear",
                err,
            )
            # Connectivity failure: feed the lock breaker so repeated failures
            # converge to "unreachable" alongside poll failures (recovers via a
            # successful poll/push).
            self._coordinator.note_connectivity_failure()
            self._state = SyncState.OUT_OF_SYNC
            return
        except LockOperationFailed as err:
            _LOGGER.info(
                "%s: Operation failed during %s usercode: %s. Will retry on next tick.",
                self._log_prefix,
                "set" if slot_state.active_state == STATE_ON else "clear",
                err,
            )
            # The lock is reachable but the operation failed. Count toward the
            # slot breaker so a persistently-failing slot suspends instead of
            # retrying forever -- NOT the lock breaker, whose read-probe
            # recovery can't validate a failing write.
            self._slot_breaker.record_failure()
            self._state = SyncState.OUT_OF_SYNC
            return
        except Exception as err:
            _LOGGER.exception(
                "%s: Unexpected error during %s usercode. "
                "Sync suspended for this lock to prevent infinite retry loop. "
                "Error: %s: %s",
                self._log_prefix,
                "set" if slot_state.active_state == STATE_ON else "clear",
                type(err).__name__,
                err,
            )
            self._suspend_slot(
                slot_state,
                f"Lock **{self._lock.lock.entity_id}**: slot **{self._slot_num}** "
                f"encountered an unexpected error during sync. This may indicate a bug "
                f"in the lock code manager integration. Check logs for details and "
                f"report this issue.\n\nError: {type(err).__name__}: {err}",
            )
            return
        else:
            # Sync succeeded — refresh coordinator to verify.
            # Skip for push providers — they update coordinator optimistically
            # via push_update() and refreshing from cache could read stale data.
            if not self._lock.supports_push:
                try:
                    await self._coordinator.async_refresh()
                except Exception:
                    _LOGGER.exception(
                        "%s: Coordinator refresh failed after sync operation. "
                        "Sync may have succeeded but verification failed. "
                        "Will retry on next tick.",
                        self._log_prefix,
                    )
                    self._state = SyncState.OUT_OF_SYNC
                    return

        # Check if sync actually worked
        slot_state = self._resolve_slot_state()
        if slot_state is not None and self.calculate_in_sync(slot_state):
            self._state = SyncState.IN_SYNC
            self._slot_breaker.reset()
            self._write_state()
            self._clear_resolved_issues(slot_state)
        else:
            self._state = SyncState.OUT_OF_SYNC
            self._write_state()

    # -- State tracking subscriptions ----------------------------------------

    def _setup_coordinator_listener(self) -> None:
        """Subscribe to coordinator updates."""
        self._coordinator_unsub = self._coordinator.async_add_listener(
            self._request_sync_check
        )

    @callback
    def _cleanup_state_tracking(self) -> None:
        """Clean up state tracking subscription if one exists."""
        if self._state_tracking_unsub:
            self._state_tracking_unsub()
            self._state_tracking_unsub = None
            self._tracking_all_states = False

    @callback
    def _try_upgrade_state_tracking(self) -> None:
        """
        Upgrade catch-all state tracking to targeted if entities are now available.

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
            self._request_sync_check,
        )
        self._tracking_all_states = False
        _LOGGER.debug(
            "%s: Upgraded from catch-all to targeted state tracking",
            self._log_prefix,
        )

    @callback
    def _setup_state_tracking(self) -> None:
        """
        Set up state change tracking for dependent entities.

        If all entity IDs are available, tracks only those specific entities.
        Otherwise, tracks all state changes via a catch-all subscription that
        filters by tracked entity IDs in _request_sync_check_if_relevant. The catch-all
        is upgraded to targeted tracking in _try_upgrade_state_tracking(), which
        runs at the start of each tick (safe because it's outside a callback).
        """
        self._cleanup_state_tracking()

        if self._build_entity_id_map():
            self._state_tracking_unsub = async_track_state_change_event(
                self._hass,
                self._tracked_entity_ids,
                self._request_sync_check,
            )
            self._tracking_all_states = False
        else:
            tracker = async_track_state_change_filtered(
                self._hass,
                TrackStates(True, set(), set()),
                self._request_sync_check_if_relevant,
            )
            self._state_tracking_unsub = tracker.async_remove
            self._tracking_all_states = True
            _LOGGER.debug(
                "%s: Waiting for dependent entities, tracking all state changes",
                self._log_prefix,
            )
