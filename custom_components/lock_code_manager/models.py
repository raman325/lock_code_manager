"""
Data model types for lock_code_manager.

Canonical home for dataclasses, type aliases, enums, and structured data types
used across the integration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .callbacks import EntityCallbackRegistry
from .domain.config import EntryConfig

if TYPE_CHECKING:
    from .domain.slot_coordinator import SlotEntityCoordinator
    from .domain.sync import SlotSyncManager
    from .providers import BaseLock


class SyncState(StrEnum):
    """
    State machine for slot sync reconciliation.

    LOADING: initial state, waiting for entity states to resolve.
    IN_SYNC: desired state matches actual state on the lock.
    OUT_OF_SYNC: mismatch detected, pending sync on next tick.
    SYNCING: sync operation in progress.
    SUSPENDED: circuit breaker tripped or unexpected error; awaiting
        coordinator recovery (suspended flag cleared).
    """

    LOADING = "loading"
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    SYNCING = "syncing"
    SUSPENDED = "suspended"


class SlotCode(StrEnum):
    """
    Serialization labels for non-string credential states.

    Returned by ``SlotCredential.as_label()`` for diagnostics and websocket
    payloads so external consumers see stable string values ("empty" /
    "unreadable_code") rather than a structured credential object.

    UNREADABLE_CODE means a code exists on the lock but its value cannot be
    read back (for example, write-only locks like Matter). This is distinct
    from Home Assistant's STATE_UNKNOWN ("unknown"), which represents a
    sensor whose state is not yet known.
    """

    EMPTY = "empty"
    UNREADABLE_CODE = "unreadable_code"


@dataclass(frozen=True, slots=True)
class SlotCredential:
    """
    Credential state for one slot on one lock.

    Three constructors:
    - ``empty()`` -- slot is cleared on the lock
    - ``unreadable()`` -- slot holds a code whose value is write-only
    - ``known(pin)`` -- slot holds a code whose value the provider exposes

    Treat as opaque; consume via accessors not direct field access.
    """

    present: bool
    pin: str | None

    @classmethod
    def empty(cls) -> SlotCredential:
        """Return the shared "slot is cleared" credential."""
        return _EMPTY_CREDENTIAL

    @classmethod
    def unreadable(cls) -> SlotCredential:
        """Return the shared "slot holds a write-only code" credential."""
        return _UNREADABLE_CREDENTIAL

    @classmethod
    def known(cls, pin: str) -> SlotCredential:
        """Return a credential carrying a readable PIN."""
        return cls(present=True, pin=pin)

    @property
    def is_empty(self) -> bool:
        """Return True when no code is present on the lock for this slot."""
        return not self.present

    @property
    def is_present(self) -> bool:
        """Return True when a code is present on the lock for this slot."""
        return self.present

    @property
    def is_readable(self) -> bool:
        """Return True when the credential exposes a comparable PIN."""
        return self.present and self.pin is not None

    @property
    def readable_pin(self) -> str | None:
        """Return the PIN when readable, otherwise ``None``."""
        return self.pin if self.is_readable else None

    def matches(self, pin: str) -> bool:
        """Return True when this credential is readable and equals ``pin``."""
        return self.is_readable and self.pin == pin

    def as_label(self) -> str | SlotCode:
        """Return stable serialization for diagnostics/websocket consumers."""
        if not self.present:
            return SlotCode.EMPTY
        if self.pin is None:
            return SlotCode.UNREADABLE_CODE
        return self.pin


_EMPTY_CREDENTIAL: Final = SlotCredential(present=False, pin=None)
_UNREADABLE_CREDENTIAL: Final = SlotCredential(present=True, pin=None)


@dataclass
class LockCodeManagerConfigEntryRuntimeData:
    """Runtime data for a Lock Code Manager config entry."""

    locks: dict[str, BaseLock] = field(default_factory=dict)
    setup_tasks: dict[str | Platform, asyncio.Task[Any]] = field(default_factory=dict)
    callbacks: EntityCallbackRegistry = field(default_factory=EntityCallbackRegistry)
    # Cached typed view of the entry's current config; refreshed by the
    # update listener on every change. Readers should prefer this over
    # parsing config_entry.data/options directly. See data.EntryConfig.
    config: EntryConfig = field(default_factory=EntryConfig.empty)
    # Active per-slot sync managers, registered by the in-sync binary sensor
    # on add and discarded on remove. Tracked so async_unload_entry can stop
    # them up front -- before lock-removed callbacks fire and before platforms
    # unload -- so an in-flight tick cannot keep running against torn-down
    # state.
    sync_managers: set[SlotSyncManager] = field(default_factory=set)
    # Per-slot entity coordinators. Created when a slot is added and torn
    # down when it is removed. Owns the derived "active" state and the
    # intent-dispatch surface used by text/switch/active entities so they
    # do not have to mutate the config entry or call sibling-entity
    # services directly.
    slot_coordinators: dict[int, SlotEntityCoordinator] = field(default_factory=dict)
    # True once the options update listener has been registered for this
    # entry. Guards against stacking when _setup_entry_after_start runs more
    # than once (for example, a reload racing with EVENT_HOMEASSISTANT_STARTED).
    update_listener_registered: bool = False


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryRuntimeData]
