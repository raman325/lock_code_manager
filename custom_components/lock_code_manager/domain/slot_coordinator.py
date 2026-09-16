"""
Per-slot entity coordinator.

A SlotEntityCoordinator instance owns the per-slot state surface for the
text, switch, and active-binary-sensor entities. Entities are read-only
views over the coordinator: they register write callbacks for state
changes and dispatch user intent (set a PIN, toggle enabled) through the
coordinator. The coordinator updates the canonical config entry, manages
slot-level repair issues, and asks the per-lock SlotSyncManagers to
re-evaluate on the next tick.

There is one SlotEntityCoordinator per (config_entry, slot_num); it owns
one SlotSyncManager per lock and credential address for that slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Iterable
from functools import partial
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from ..const import ATTR_IN_SYNC, DOMAIN, EVENT_CREDENTIAL_USED
from .config import EntryConfig, async_write_entry_config
from .credentials import CredentialAddress, CredentialType, managed_addresses
from .names import name_error, normalize_name
from .queries import get_entry_config
from .sync import SlotSyncManager, fold_in_sync, fold_sync_status

if TYPE_CHECKING:
    from ..providers import BaseLock
    from .models import LockCodeManagerConfigEntry


_LOGGER = logging.getLogger(__name__)


ActiveViewWriter = Callable[[bool | None, list[str]], None]


async def _async_gather_managers(
    managers: list[SlotSyncManager],
    operation: Callable[[SlotSyncManager], Awaitable[None]],
    what: str,
) -> None:
    """
    Run ``operation`` on every manager together; one raising is logged, not fatal.

    A cancellation from outside is not a failure: ``gather`` hands a cancelled
    child back as a value, and dropping it would let the pass carry on as if
    nothing had happened. (A stop's own cancellation of a first tick never
    reaches here; the manager absorbs it.)
    """
    results = await asyncio.gather(
        *(operation(manager) for manager in managers), return_exceptions=True
    )
    for manager, result in zip(managers, results, strict=True):
        if isinstance(result, Exception):
            _LOGGER.warning(
                "%s: Sync manager %s: %s",
                manager.log_prefix,
                what,
                result,
                exc_info=result,
            )
    if cancelled := next(
        (result for result in results if isinstance(result, asyncio.CancelledError)),
        None,
    ):
        raise cancelled


class SlotEntityCoordinator:
    """
    Coordinate per-slot entity state for one (config entry, slot) pair.

    Computes the "active" derived state from the slot config plus the
    optional condition entity, fans out updates to the active binary
    sensor, owns slot-level repair issues (``pin_required``), and provides
    a single intent-dispatch surface so text and switch entities do not
    have to mutate the config entry or call sibling-entity services
    directly.

    Owns the slot's sync managers: one per lock and credential address,
    started when a lock is set up for the slot and stopped when the lock
    or the slot goes. The in-sync sensors are views over them, so
    disabling a sensor never stops a sync.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
    ) -> None:
        """Initialize the coordinator."""
        self._hass = hass
        self._ent_reg = er.async_get(hass)
        self._config_entry = config_entry
        self._slot_num = int(slot_num)
        self._log_prefix = (
            f"{config_entry.entry_id} ({config_entry.title}): slot {slot_num}"
        )

        self._active_view_writers: set[ActiveViewWriter] = set()
        self._state_subscribers: set[Callable[[], None]] = set()
        # Sync subscribers are per lock: a manager's change concerns the
        # sensors of its lock, not the slot's text and switch entities nor
        # the other locks' sensors.
        self._sync_subscribers: dict[str, set[Callable[[], None]]] = {}
        self._sync_managers: dict[tuple[str, CredentialAddress], SlotSyncManager] = {}

        # Condition-entity subscription state
        self._condition_unsub: Callable[[], None] | None = None
        self._subscribed_condition_entity_id: str | None = None

        self._started = False

        # Cached derived state, refreshed by _recompute_active(). Initial
        # values are "unknown" so an entity created before the first
        # recompute can show STATE_UNKNOWN rather than a stale guess.
        self._is_active: bool | None = None
        self._inactive_because_of: list[str] = []

    # -- Lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the coordinator -- subscribe to the condition entity."""
        if self._started:
            return
        self._started = True
        self._update_condition_subscription()
        self._recompute_active()

    @callback
    def async_stop(self) -> None:
        """Stop the coordinator -- unsubscribe and clear writers."""
        if not self._started:
            return
        self._started = False
        if self._condition_unsub:
            self._condition_unsub()
            self._condition_unsub = None
        self._subscribed_condition_entity_id = None
        self._active_view_writers.clear()
        # The managers are stopped by ``async_stop_sync``: stopping takes an
        # await this callback cannot make.

    # -- Sync managers -------------------------------------------------------

    @property
    def _is_current(self) -> bool:
        """
        Whether this is the entry's live coordinator for its slot.

        A pass that captured the coordinator before an unload, a slot removal,
        or a reload finished holds one the entry no longer runs; a manager it
        started would have no teardown left to stop it.
        """
        runtime_data = getattr(self._config_entry, "runtime_data", None)
        return (
            runtime_data is not None
            and runtime_data.slot_coordinators.get(self._slot_num) is self
        )

    async def async_start_sync(self, lock: BaseLock) -> None:
        """
        Start a manager for every credential of this slot on ``lock``.

        Nothing starts on a coordinator that has stopped or that the entry no
        longer runs: an update listener can reach here after the unload it is
        racing has already stopped everything.
        """
        if lock.coordinator is None or not self._started or not self._is_current:
            _LOGGER.debug(
                "%s: Not starting sync on %s (coordinator ready: %s, running: %s, current: %s)",
                self._log_prefix,
                lock.lock.entity_id,
                lock.coordinator is not None,
                self._started,
                self._is_current,
            )
            return
        lock_entity_id = lock.lock.entity_id
        # A manager already running for a credential stays; replacing it would
        # leave the old one ticking with nothing able to reach it.
        new = {
            key: SlotSyncManager(
                self._hass,
                self._ent_reg,
                self._config_entry,
                lock.coordinator,
                lock,
                address,
                on_change=partial(self._notify_sync_changed, lock_entity_id),
            )
            for address in managed_addresses(self._slot_num)
            if (key := (lock_entity_id, address)) not in self._sync_managers
        }
        self._sync_managers.update(new)
        # One manager failing to start must not take the rest of the setup
        # pass with it; it stays registered so the stop still reaches it.
        await _async_gather_managers(
            list(new.values()), lambda manager: manager.async_start(), "failed to start"
        )

    async def async_stop_sync(
        self, lock_entity_ids: Collection[str] | None = None
    ) -> None:
        """
        Stop and drop this slot's managers, for some locks or for all of them.

        Stopped together, so several locks that have stopped answering cost
        one stop grace rather than one each. A manager whose stop raises is
        logged and dropped like the rest; nothing stays registered that is
        not running.
        """
        wanted = None if lock_entity_ids is None else set(lock_entity_ids)
        managers = [
            self._sync_managers.pop(key)
            for key in list(self._sync_managers)
            if wanted is None or key[0] in wanted
        ]
        await _async_gather_managers(
            managers, lambda manager: manager.async_stop(), "stop raised"
        )

    @property
    def sync_managers(self) -> list[SlotSyncManager]:
        """Return every manager this slot is running."""
        return list(self._sync_managers.values())

    def sync_manager(
        self, lock_entity_id: str, address: CredentialAddress
    ) -> SlotSyncManager | None:
        """Return the manager for one credential on one lock, if it is running."""
        return self._sync_managers.get((lock_entity_id, address))

    @callback
    def sync_state_for(
        self, lock_entity_id: str, addresses: Iterable[CredentialAddress]
    ) -> tuple[bool | None, str | None]:
        """
        Fold the in-sync state of some credentials on one lock.

        On when every manager is; the worst of their statuses. A credential
        with no manager reads as unknown, so a sensor added before its
        manager, or for a lock without one, says so rather than guessing.
        """
        managers = [
            self._sync_managers.get((lock_entity_id, address)) for address in addresses
        ]
        return (
            fold_in_sync(manager.in_sync if manager else None for manager in managers),
            fold_sync_status(
                manager.sync_status if manager else None for manager in managers
            ),
        )

    # -- Read-only views (consumed by entities) ------------------------------

    @property
    def log_prefix(self) -> str:
        """Return the log prefix identifying this coordinator's entry and slot."""
        return self._log_prefix

    @property
    def slot_num(self) -> int:
        """Return the slot number."""
        return self._slot_num

    @property
    def is_active(self) -> bool | None:
        """Return the derived active state (None before first compute)."""
        return self._is_active

    @property
    def inactive_because_of(self) -> list[str]:
        """Return the list of keys keeping this slot inactive."""
        return list(self._inactive_because_of)

    @property
    def is_enabled(self) -> bool:
        """Return the slot's enabled flag from the cached config view."""
        return bool(self._slot_config().get(CONF_ENABLED))

    @property
    def pin_value(self) -> str | None:
        """Return the configured PIN, or None if not set."""
        return self._slot_config().get(CONF_PIN) or None

    @property
    def condition_entity_id(self) -> str | None:
        """Return the configured condition entity ID for this slot."""
        return self._slot_config().get(CONF_CONDITION)

    # -- Registration (entities, sync managers) ------------------------------

    @callback
    def register_active_view(self, writer: ActiveViewWriter) -> Callable[[], None]:
        """
        Register a writer the coordinator calls to update an active-view entity.

        Returns an unsubscribe function. The writer is called immediately
        with the current derived state so the entity can render before
        any subsequent state change. If that immediate call raises, the
        writer is not retained -- otherwise a half-added entity would
        keep receiving fan-outs without ever being attached.
        """
        try:
            writer(self._is_active, list(self._inactive_because_of))
        except Exception:
            _LOGGER.exception(
                "%s: Active-view writer raised on registration; discarding",
                self._log_prefix,
            )
            raise
        self._active_view_writers.add(writer)
        return lambda: self._active_view_writers.discard(writer)

    @callback
    def register_state_subscriber(
        self, callback_fn: Callable[[], None]
    ) -> Callable[[], None]:
        """
        Register a callback fired after the coordinator writes config fields.

        Used by text and switch entities so a coordinator-driven write of
        a sibling field (for example, auto-disable on PIN clear) causes
        the sibling entity to push its new state to Home Assistant.
        """
        self._state_subscribers.add(callback_fn)
        return lambda: self._state_subscribers.discard(callback_fn)

    @callback
    def register_sync_subscriber(
        self, lock_entity_id: str, callback_fn: Callable[[], None]
    ) -> Callable[[], None]:
        """Register a callback fired when a manager of ``lock_entity_id`` changes state."""
        subscribers = self._sync_subscribers.setdefault(lock_entity_id, set())
        subscribers.add(callback_fn)

        @callback
        def unsubscribe() -> None:
            subscribers.discard(callback_fn)
            if not subscribers:
                self._sync_subscribers.pop(lock_entity_id, None)

        return unsubscribe

    # -- Intent dispatch -----------------------------------------------------

    async def async_request_name_update(self, value: str) -> None:
        """
        Apply a slot name write requested by the text entity.

        The name is the identity the configuration is keyed by, so this path
        enforces the same rules the config flow does. Without it the ordinary
        way to rename a user in the frontend would be a hole straight through
        them: an empty name, or a duplicate of somebody else's, would land in
        the config entry unchallenged.
        """
        name = normalize_name(value)
        if error := name_error(name):
            raise InvalidNameError(error, self._slot_num)

        # Compared against every configured user, not only those holding a
        # slot: a user without one is still a name that cannot be taken, and
        # renaming onto them would be refused further down without an error
        # ever reaching the caller.
        config = get_entry_config(self._config_entry)
        mine = config.name_for(self._slot_num)
        conflict = next(
            (
                other
                for other in config.users
                if other != mine and other.casefold() == name.casefold()
            ),
            None,
        )
        if conflict is not None:
            raise InvalidNameError("name_not_unique", self._slot_num, conflict)

        self._write_config_fields({CONF_NAME: name})

    async def async_request_pin_update(self, value: str) -> None:
        """
        Apply a PIN write requested by the text entity.

        Normalizing whitespace and the empty-PIN side effect (disabling
        the slot on an active slot whose PIN was cleared) live here so
        entities do not have to coordinate sibling state themselves.
        Stripping applies to every PIN, not just the whitespace-only one:
        a submitted code is stripped before it is matched, so padding kept
        on the stored side makes a credential nothing typed can match.

        A non-empty PIN is validated against every bound lock's advertised
        length range before it is written; an empty PIN clears the slot and
        is exempt. This is the authoritative gate for BOTH ends: the text
        entity keeps ``native_min`` and ``native_max`` permissive so Home
        Assistant's ``text.set_value`` service neither rejects the empty clear
        nor pre-empts the per-lock error built here -- and so a lock
        advertising a limit tighter than it really accepts cannot silently
        stop the keystrokes with no message at all.
        """
        value = value.strip()

        if value:
            self._validate_credential_length(value, CredentialType.PIN)

        updates: dict[str, Any] = {CONF_PIN: value}
        if not value and self.is_enabled:
            _LOGGER.debug(
                "%s: PIN cleared on enabled slot, auto-disabling",
                self._log_prefix,
            )
            updates[CONF_ENABLED] = False

        self._write_config_fields(updates)

    def _validate_credential_length(
        self, value: str, credential_type: CredentialType
    ) -> None:
        """
        Reject ``value`` if it violates any bound lock's length range.

        Authoritative gate for credential length. Iterates every bound lock so
        the error names each offending lock with its required range. The lock
        set is the entry-wide ``runtime_data.locks`` -- the same set the text
        entity mirrors in ``self.locks`` to size its surfaced bounds, since LCM
        binds every lock to every slot; a future per-slot binding must update
        both sites together. Locks whose capabilities are not cached
        (disconnected or not yet probed) and locks that do not advertise
        ``credential_type`` are skipped -- the write proceeds rather than
        blocking on unknown limits, and the sync layer surfaces any later
        device rejection.
        """
        length = len(value)
        violations: list[str] = []
        for lock in self._config_entry.runtime_data.locks.values():
            caps = lock.cached_capabilities
            if caps is None:
                continue
            bounds = caps.length_bounds(credential_type)
            if bounds is None:
                continue
            lo, hi = bounds
            if length < lo or (hi is not None and length > hi):
                required = (
                    f"at least {lo} characters"
                    if hi is None
                    else f"{lo}-{hi} characters"
                )
                violations.append(f"{required} for {lock.display_name}")
        if violations:
            raise ServiceValidationError(
                f"{credential_type.value.upper()} length {length} is not accepted "
                f"by all locks: {'; '.join(violations)}"
            )

    async def async_request_active_toggle(self, enabled: bool) -> None:
        """
        Apply an enabled/disabled toggle requested by the switch entity.

        Disable is unconditional. Enable validates that a PIN exists and
        raises ``PinRequiredError`` if absent (the switch translates
        that into ``HomeAssistantError``). On a successful enable the
        ``pin_required`` repair issue is cleared; failures inside the
        issue registry are logged and do not unwind the write.
        """
        if not enabled:
            self._write_config_fields({CONF_ENABLED: False})
            return

        if not self.pin_value:
            self._safely_raise_pin_required_issue()
            raise PinRequiredError(
                f"Set a PIN code for slot {self._slot_num} before enabling it"
            )

        self._write_config_fields({CONF_ENABLED: True})
        try:
            async_delete_issue(
                self._hass,
                DOMAIN,
                f"pin_required_{self._config_entry.entry_id}_{self._slot_num}",
            )
        except Exception:
            _LOGGER.exception(
                "%s: Failed to delete pin_required repair issue after enable",
                self._log_prefix,
            )

    # -- Config change hook (called by async_update_listener) ----------------

    @callback
    def notify_config_changed(self) -> None:
        """
        React to a config entry change.

        Called by ``async_update_listener`` after it refreshes
        ``runtime_data.config``. Updates the condition-entity subscription
        if the condition entity moved, recomputes derived state, and
        fans the new state out to writers and sync managers.
        """
        if not self._started:
            return
        self._update_condition_subscription()
        self._recompute_active()
        self._notify_state_subscribers()

    # -- Internal helpers ----------------------------------------------------

    def _slot_config(self) -> dict[str, Any]:
        """Return the current slot config dict for this slot number."""
        return dict(get_entry_config(self._config_entry).slot(self._slot_num))

    @callback
    def _write_config_fields(self, fields: dict[str, Any]) -> None:
        """
        Write one or more slot fields to the config entry in a single update.

        Coalescing avoids the trap where the update listener has not yet
        refreshed ``runtime_data.config`` between two consecutive writes,
        leading the second write to drop the first.

        ``async_update_entry`` schedules the update listener as a task,
        so ``runtime_data.config`` is still stale at the synchronous
        notify below. Refresh it eagerly here so ``_recompute_active``,
        ``_notify_state_subscribers``, and ``_poke_sync_managers``
        observe the new values. The listener will refresh again when it
        runs -- writing the same value twice is harmless.
        """
        config = get_entry_config(self._config_entry)
        for key, value in fields.items():
            config = config.with_slot_field_set(self._slot_num, key, value)
        async_write_entry_config(self._hass, self._config_entry, config)
        self._config_entry.runtime_data.config = EntryConfig.from_entry(
            self._config_entry
        )
        self._recompute_active()
        self._notify_state_subscribers()
        self._poke_sync_managers()

    @callback
    def _notify_state_subscribers(self) -> None:
        """Notify entity-side write-back subscribers that config changed."""
        self._notify(self._state_subscribers, "State subscriber")

    @callback
    def _notify_sync_changed(self, lock_entity_id: str) -> None:
        """Notify the sensors of one lock that a manager of theirs changed state."""
        self._notify(self._sync_subscribers.get(lock_entity_id, ()), "Sync subscriber")

    def _notify(self, subscribers: Iterable[Callable[[], None]], what: str) -> None:
        """Call every subscriber; one raising is logged so the rest still hear."""
        for subscriber in list(subscribers):
            try:
                subscriber()
            except Exception:
                _LOGGER.exception("%s: %s raised", self._log_prefix, what)

    @callback
    def _poke_sync_managers(self) -> None:
        """Ask each per-lock sync manager to re-evaluate against fresh state."""
        for manager in self.sync_managers:
            try:
                manager.request_sync_check()
            except Exception:
                _LOGGER.exception(
                    "%s: Sync manager raised on request_sync_check",
                    self._log_prefix,
                )

    @callback
    def _update_condition_subscription(self) -> None:
        """(Re-)subscribe to the condition entity if it changed."""
        current = self.condition_entity_id
        if current == self._subscribed_condition_entity_id:
            return

        if self._condition_unsub:
            self._condition_unsub()
            self._condition_unsub = None
        if current:
            self._condition_unsub = async_track_state_change_event(
                self._hass,
                [current],
                self._handle_condition_state_change,
            )
        self._subscribed_condition_entity_id = current

    @callback
    def _handle_condition_state_change(
        self, _event: Event[EventStateChangedData]
    ) -> None:
        """Recompute active state when the condition entity changes."""
        if not self._started:
            return
        self._recompute_active()

    @callback
    def _recompute_active(self) -> None:
        """
        Compute the slot's active state from config + condition entity.

        Every relevant slot config key must be truthy. The condition
        entity (``CONF_CONDITION``) is truthy when its state is ``on``;
        ``off`` is False and any other state (unknown, unavailable,
        missing) is None and treated as inactive.
        """
        slot_config = self._slot_config()
        states: dict[str, bool | None] = {}
        for key, value in slot_config.items():
            if key in (EVENT_CREDENTIAL_USED, CONF_NAME, CONF_PIN, ATTR_IN_SYNC):
                continue
            if key == CONF_CONDITION:
                hass_state = self._hass.states.get(value)
                if hass_state is None:
                    states[key] = None
                elif hass_state.state == STATE_ON:
                    states[key] = True
                elif hass_state.state == STATE_OFF:
                    states[key] = False
                else:
                    states[key] = None
                continue
            states[key] = bool(value)

        inactive_because_of = [k for k, v in states.items() if not v]
        new_active = not inactive_because_of
        if (
            new_active == self._is_active
            and inactive_because_of == self._inactive_because_of
        ):
            return
        self._is_active = new_active
        self._inactive_because_of = inactive_because_of
        for writer in list(self._active_view_writers):
            writer(self._is_active, list(self._inactive_because_of))

    @callback
    def _safely_raise_pin_required_issue(self) -> None:
        """Create the ``pin_required`` repair issue, logging registry failures."""
        try:
            async_create_issue(
                self._hass,
                DOMAIN,
                f"pin_required_{self._config_entry.entry_id}_{self._slot_num}",
                is_fixable=True,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="pin_required",
                translation_placeholders={
                    "slot_num": str(self._slot_num),
                    "config_entry_title": self._config_entry.title,
                },
            )
        except Exception:
            _LOGGER.exception(
                "%s: Failed to create pin_required repair issue",
                self._log_prefix,
            )


class PinRequiredError(Exception):
    """Raised when a slot cannot be enabled because no PIN is configured."""


class InvalidNameError(HomeAssistantError):
    """
    Raised when a slot name write would break the name rules.

    Subclasses ``HomeAssistantError`` so a caller that forgets to translate
    it still surfaces as a validation refusal rather than an unknown-error
    500. Carries the translation key and placeholders so the entity renders
    the same localized message the config flow shows for the same rule,
    instead of inventing a second English-only wording.
    """

    def __init__(
        self, error_key: str, slot_num: int, conflicting_name: str | None = None
    ) -> None:
        """Store the error key, the slot written to, and any user in the way."""
        self.error_key = error_key
        self.slot_num = slot_num
        self.conflicting_name = conflicting_name
        self.placeholders = {
            "slot_num": str(slot_num),
            "conflicting_name": str(conflicting_name),
        }
        super().__init__(f"{error_key} (slot {slot_num})")
