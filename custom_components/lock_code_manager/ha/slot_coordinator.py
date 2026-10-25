"""
Per-slot entity coordinator.

A SlotEntityCoordinator instance owns the per-slot state surface for the
text, switch, and active-binary-sensor entities. Entities are read-only
views over the coordinator: they register write callbacks for state
changes and dispatch user intent (set a PIN, toggle enabled) through the
coordinator. The coordinator updates the canonical config entry, manages
slot-level repair issues, and tells each of the entry's locks what the slot
wants: its credentials while it is active, nothing otherwise.

There is one SlotEntityCoordinator per (config_entry, slot_num).
"""

from collections.abc import Callable, Iterable
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PASSWORD,
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

from ..const import ATTR_IN_SYNC, CREDENTIAL_LABELS, DOMAIN, EVENT_CREDENTIAL_USED
from ..model.config import (
    EntryConfig,
    async_write_entry_config,
    credential_holder,
    credential_values,
    has_credential,
)
from ..model.credentials import (
    MANAGED_CREDENTIAL_TYPES,
    CredentialType,
    credential_value_error,
)
from ..model.names import name_error, normalize_name
from ..model.queries import get_entry_config
from .runtime import async_want

if TYPE_CHECKING:
    from .entry import LockCodeManagerConfigEntry


_LOGGER = logging.getLogger(__name__)


ActiveViewWriter = Callable[[bool | None, list[str]], None]


class SlotEntityCoordinator:
    """
    Coordinate per-slot entity state for one (config entry, slot) pair.

    Computes the "active" derived state from the slot config plus the
    optional condition entity, fans out updates to the active binary
    sensor, owns slot-level repair issues (``credential_required``), and provides
    a single intent-dispatch surface so text and switch entities do not
    have to mutate the config entry or call sibling-entity services
    directly.

    Tells each of the entry's locks what the slot wants
    (``async_push_want`` -> ``ha/runtime.async_want``); the lock's worker
    does the syncing. The in-sync sensors are views over the workers, so
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

        # Condition-entity subscription state
        self._condition_unsub: Callable[[], None] | None = None
        self._subscribed_condition_entity_id: str | None = None

        self._started = False

        # Cached derived state, refreshed by _recompute_active(). Initial
        # values are "unknown" so an entity created before the first
        # recompute can show STATE_UNKNOWN rather than a stale guess.
        self._is_active: bool | None = None
        self._inactive_because_of: list[str] = []
        # The enabled flag the last compute saw; None until the first, so a
        # coordinator starting up is no transition.
        self._was_enabled: bool | None = None

    # -- Lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Start the coordinator -- subscribe to the condition entity."""
        if self._started:
            return
        self._started = True
        self._was_enabled = None
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

    # -- What the slot wants on each lock ------------------------------------

    @property
    def wants(self) -> dict[CredentialType, str | None]:
        """Return what the slot should hold, by credential type: its credentials while active."""
        values = credential_values(self._slot_config())
        return {
            credential_type: values.get(credential_type) if self._is_active else None
            for credential_type in sorted(MANAGED_CREDENTIAL_TYPES)
        }

    @callback
    def async_push_want(self, lock_entity_ids: Iterable[str] | None = None) -> None:
        """Tell the entry's locks (or just ``lock_entity_ids``) what the slot wants."""
        if not self._started:
            return
        if self._slot_num not in get_entry_config(self._config_entry).slots:
            # Gone from the config: the update pass gives it up, as a release.
            return
        locks = (
            list(self._config_entry.runtime_data.locks)
            if lock_entity_ids is None
            else list(lock_entity_ids)
        )
        for lock_entity_id in locks:
            async_want(
                self._hass,
                self._config_entry,
                lock_entity_id,
                self._slot_num,
                self.wants,
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

    # -- Registration (entities) ---------------------------------------------

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

    async def async_request_credential_update(
        self, credential_type: CredentialType, value: str
    ) -> None:
        """
        Apply a credential write requested by a text entity or an action.

        A PIN is stripped: a submitted code is stripped before it is matched,
        so padding kept on the stored side makes a credential nothing typed
        can match. A password is kept as typed (spec decision 7).

        A non-empty value must pass the type's character rule and every bound
        lock's length range; an empty one clears the credential and is
        exempt. This is the authoritative gate: the text entity keeps
        ``native_min`` and ``native_max`` permissive so Home Assistant's
        ``text.set_value`` neither rejects the empty clear nor pre-empts the
        per-lock error built here -- whose errors would quote the value.

        Clearing the slot's last credential turns an enabled slot off: a user
        with nothing to present would read as active (spec decision 4).
        """
        if credential_type is CredentialType.PIN:
            value = value.strip()
        if value:
            if error := credential_value_error(credential_type, value):
                raise ServiceValidationError(error)
            self._validate_credential_length(value, credential_type)
        held = credential_values(self._slot_config())
        if value and value != held.get(credential_type):
            self._refuse_duplicate(credential_type, value)

        updates: dict[str, Any] = {credential_type.value: value}
        if (
            not value
            and not any(other is not credential_type for other in held)
            and self.is_enabled
        ):
            _LOGGER.debug(
                "%s: last credential cleared on enabled slot, auto-disabling",
                self._log_prefix,
            )
            updates[CONF_ENABLED] = False

        self._write_config_fields(updates)

    def _refuse_duplicate(self, credential_type: CredentialType, value: str) -> None:
        """
        Refuse ``value`` if another user in the entry holds it as the same type.

        Two users sharing a credential: whoever presents it is recorded as
        either, and turning one off leaves it working (spec decision 12).
        Every user counts, one with no slot number too. Names the other user,
        never the value.
        """
        config = get_entry_config(self._config_entry)
        own = config.name_for(self._slot_num)
        others = {name: user for name, user in config.users.items() if name != own}
        if (other := credential_holder(others, credential_type, value)) is not None:
            label = CREDENTIAL_LABELS[credential_type.value]
            raise ServiceValidationError(f"{other} already has this {label}")

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
                f"{CREDENTIAL_LABELS[credential_type.value]} length {length} is not accepted "
                f"by all locks: {'; '.join(violations)}"
            )

    async def async_request_active_toggle(self, enabled: bool) -> None:
        """
        Apply an enabled/disabled toggle requested by the switch entity.

        Disable is unconditional. Enable needs a credential of any type and
        raises ``CredentialRequiredError`` without one (the switch translates
        that into ``HomeAssistantError``). The repairs an enable answers are
        deleted by ``_recompute_active``, which sees every route back on.
        """
        if not enabled:
            self._write_config_fields({CONF_ENABLED: False})
            return

        if not has_credential(self._slot_config()):
            self._safely_raise_credential_required_issue()
            raise CredentialRequiredError(
                f"Set a PIN or password for slot {self._slot_num} before enabling it"
            )

        self._write_config_fields({CONF_ENABLED: True})

    # -- Config change hook (called by async_update_listener) ----------------

    @callback
    def notify_config_changed(self) -> None:
        """
        React to a config entry change.

        Called by ``async_update_listener`` after it refreshes
        ``runtime_data.config``. Updates the condition-entity subscription
        if the condition entity moved, recomputes derived state, and
        fans the new state out to writers and to the entry's locks.
        """
        if not self._started:
            return
        self._update_condition_subscription()
        self._recompute_active()
        self._notify_state_subscribers()
        self.async_push_want()

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
        ``_notify_state_subscribers``, and ``async_push_want``
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
        self.async_push_want()

    @callback
    def _notify_state_subscribers(self) -> None:
        """Notify entity-side write-back subscribers that config changed."""
        self._notify(self._state_subscribers, "State subscriber")

    def _notify(self, subscribers: Iterable[Callable[[], None]], what: str) -> None:
        """Call every subscriber; one raising is logged so the rest still hear."""
        for subscriber in list(subscribers):
            try:
                subscriber()
            except Exception:
                _LOGGER.exception("%s: %s raised", self._log_prefix, what)

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
        self.async_push_want()

    @callback
    def _recompute_active(self) -> None:
        """
        Compute the slot's active state from config + condition entity.

        Every relevant slot config key must be truthy. The condition
        entity (``CONF_CONDITION``) is truthy when its state is ``on``;
        ``off`` is False and any other state (unknown, unavailable,
        missing) is None and treated as inactive.

        Every change of the slot's configuration passes through here, so it
        is where the slot being turned back on is seen, by whatever route.
        """
        slot_config = self._slot_config()
        enabled = bool(slot_config.get(CONF_ENABLED))
        if self._was_enabled is False and enabled:
            self._answer_enable_repairs()
        self._was_enabled = enabled
        states: dict[str, bool | None] = {}
        for key, value in slot_config.items():
            if key in (
                EVENT_CREDENTIAL_USED,
                CONF_NAME,
                CONF_PIN,
                CONF_PASSWORD,
                ATTR_IN_SYNC,
            ):
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
    def _answer_enable_repairs(self) -> None:
        """
        Delete the repairs that ask the user to turn the slot back on.

        ``credential_required`` and a refusal's ``slot_disabled``: turning the
        slot on is what each asks for, and a value refused again raises its
        repair afresh. A registry failure is logged and does not unwind the
        enable.
        """
        for issue in ("credential_required", "slot_disabled"):
            try:
                async_delete_issue(
                    self._hass,
                    DOMAIN,
                    f"{issue}_{self._config_entry.entry_id}_{self._slot_num}",
                )
            except Exception:
                _LOGGER.exception(
                    "%s: Failed to delete %s repair issue after enable",
                    self._log_prefix,
                    issue,
                )

    @callback
    def _safely_raise_credential_required_issue(self) -> None:
        """Create the ``credential_required`` repair issue, logging registry failures."""
        try:
            async_create_issue(
                self._hass,
                DOMAIN,
                f"credential_required_{self._config_entry.entry_id}_{self._slot_num}",
                is_fixable=True,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="credential_required",
                translation_placeholders={
                    "slot_num": str(self._slot_num),
                    "config_entry_title": self._config_entry.title,
                },
            )
        except Exception:
            _LOGGER.exception(
                "%s: Failed to create credential_required repair issue",
                self._log_prefix,
            )


class CredentialRequiredError(Exception):
    """Raised when a slot cannot be enabled because it holds no credential."""


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
