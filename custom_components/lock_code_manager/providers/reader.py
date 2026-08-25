"""Module for credential reader "locks" (keypads and similar devices)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event

from ..domain.validation import validate_across_entries
from .virtual import VirtualLock


@dataclass(repr=False, eq=False)
class ReaderLock(VirtualLock):
    """
    A credential reader anchored on a state-bearing entity.

    The anchor entity's state is the last-submitted credential; each
    non-empty value is validated against the entry's users. Slot storage
    reuses the virtual provider's Store so the sync machinery treats the
    reader like any slot-only lock.
    """

    # A Home Assistant state listener tied to setup/unload, which the base
    # push-subscription registry explicitly does not manage.
    _anchor_state_unsub: Callable[[], None] | None = field(
        default=None, init=False, repr=False
    )

    @property
    def domain(self) -> str:
        """Return the anchor entity's integration domain."""
        return self.lock.platform

    @property
    def supports_code_slot_events(self) -> bool:
        """
        Readers exist to generate code-slot usage events.

        The False inherited from VirtualLock covers storage-only virtual
        locks; a reader's validations must reach the credential_used event
        entity, and its availability counts only supporting locks.
        """
        return True

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up slot storage and start watching the anchor entity."""
        await super().async_setup(config_entry)
        # Idempotent across provider-integration reconnects: drop any
        # previous subscription before re-subscribing.
        if self._anchor_state_unsub is not None:
            self._anchor_state_unsub()
        self._anchor_state_unsub = async_track_state_change_event(
            self.hass, self.lock.entity_id, self._async_anchor_state_changed
        )

    async def async_unload(self, remove_permanently: bool) -> None:
        """Stop watching the anchor entity, then unload slot storage."""
        if self._anchor_state_unsub is not None:
            self._anchor_state_unsub()
            self._anchor_state_unsub = None
        await super().async_unload(remove_permanently)

    @callback
    def _async_anchor_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Validate the code a new anchor state carries, skipping blank states."""
        new_state = event.data["new_state"]
        if new_state is None or new_state.state in (
            "",
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return
        # Provider instances are shared across Lock Code Manager entries, so
        # the entry that ran setup is not the only one this anchor answers
        # to: validate against every entry that contains it, or credentials
        # belonging to any other entry read as unknown.
        #
        # A synchronous call inside the state-change callback: submissions
        # cannot interleave with each other or straddle an unload, and the
        # validation core's signature guarantees it.
        validate_across_entries(self.hass, self.lock.entity_id, new_state.state)
