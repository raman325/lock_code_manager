"""
Per-slot entity coordinator.

A SlotEntityCoordinator instance owns the per-slot state surface that
text, switch, and active-binary-sensor entities used to compute on their
own. Entities become read-only views over the coordinator: they register
write callbacks for state changes and dispatch user intent (set a PIN,
toggle enabled) through the coordinator. The coordinator updates the
canonical config entry, manages slot-level repair issues, and asks the
per-lock SlotSyncManagers to re-evaluate on the next tick.

There is one SlotEntityCoordinator per (config_entry, slot_num); the per-
lock SlotSyncManager remains one per (config_entry, slot_num, lock).
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    CONF_ENABLED,
    CONF_ENTITY_ID,
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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from ..const import ATTR_IN_SYNC, DOMAIN, EVENT_PIN_USED
from .config import EntryConfig
from .queries import get_entry_config

if TYPE_CHECKING:
    from .models import LockCodeManagerConfigEntry
    from .sync import SlotSyncManager


_LOGGER = logging.getLogger(__name__)


ActiveViewWriter = Callable[[bool | None, list[str]], None]


class SlotEntityCoordinator:
    """
    Coordinate per-slot entity state for one (config entry, slot) pair.

    Computes the "active" derived state from the slot config plus the
    optional condition entity, fans out updates to the active binary
    sensor, owns slot-level repair issues (``pin_required``), and provides
    a single intent-dispatch surface so text and switch entities do not
    have to mutate the config entry or call sibling-entity services
    directly.

    The coordinator does not own the per-lock SlotSyncManager state
    machine; it just keeps a reference to the managers for this slot so
    it can request a sync check after a user-visible state change.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
    ) -> None:
        """Initialize the coordinator."""
        self._hass = hass
        self._config_entry = config_entry
        self._slot_num = int(slot_num)
        self._log_prefix = (
            f"{config_entry.entry_id} ({config_entry.title}): slot {slot_num}"
        )

        self._active_view_writers: set[ActiveViewWriter] = set()
        self._state_subscribers: set[Callable[[], None]] = set()
        self._sync_managers: set[SlotSyncManager] = set()

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
        self._sync_managers.clear()

    # -- Read-only views (consumed by entities) ------------------------------

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
        return self._slot_config().get(CONF_ENTITY_ID)

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
    def register_sync_manager(self, manager: SlotSyncManager) -> Callable[[], None]:
        """Register a per-lock sync manager so the coordinator can poke it on changes."""
        self._sync_managers.add(manager)
        return lambda: self._sync_managers.discard(manager)

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
        """Apply a slot name write requested by the text entity."""
        self._write_config_fields({CONF_NAME: value})

    async def async_request_pin_update(self, value: str) -> None:
        """
        Apply a PIN write requested by the text entity.

        Normalizing whitespace and the empty-PIN side effect (disabling
        the slot on an active slot whose PIN was cleared) live here so
        entities do not have to coordinate sibling state themselves.
        """
        if not value.strip():
            value = ""

        updates: dict[str, Any] = {CONF_PIN: value}
        if not value and self.is_enabled:
            _LOGGER.debug(
                "%s: PIN cleared on enabled slot, auto-disabling",
                self._log_prefix,
            )
            updates[CONF_ENABLED] = False

        self._write_config_fields(updates)

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
        self._hass.config_entries.async_update_entry(
            self._config_entry, data=config.to_dict()
        )
        self._config_entry.runtime_data.config = EntryConfig.from_entry(
            self._config_entry
        )
        self._recompute_active()
        self._notify_state_subscribers()
        self._poke_sync_managers()

    @callback
    def _notify_state_subscribers(self) -> None:
        """Notify entity-side write-back subscribers that config changed."""
        for subscriber in list(self._state_subscribers):
            try:
                subscriber()
            except Exception:
                _LOGGER.exception("%s: State subscriber raised", self._log_prefix)

    @callback
    def _poke_sync_managers(self) -> None:
        """Ask each per-lock sync manager to re-evaluate against fresh state."""
        for manager in list(self._sync_managers):
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
        entity (``CONF_ENTITY_ID``) is truthy when its state is ``on``;
        ``off`` is False and any other state (unknown, unavailable,
        missing) is None and treated as inactive.
        """
        slot_config = self._slot_config()
        states: dict[str, bool | None] = {}
        for key, value in slot_config.items():
            if key in (EVENT_PIN_USED, CONF_NAME, CONF_PIN, ATTR_IN_SYNC):
                continue
            if key == CONF_ENTITY_ID:
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
