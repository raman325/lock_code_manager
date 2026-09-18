"""
Data model types for lock_code_manager.

Canonical home for dataclasses, type aliases, enums, and structured data types
used across the integration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from homeassistant.config_entries import ConfigEntry

from .callbacks import EntityCallbackRegistry
from .config import EntryConfig

if TYPE_CHECKING:
    from ..providers import BaseLock
    from .slot_coordinator import SlotEntityCoordinator


class SyncState(StrEnum):
    """
    State machine for slot sync reconciliation.

    LOADING: initial state, waiting for entity states to resolve.
    IN_SYNC: desired state matches actual state on the lock.
    OUT_OF_SYNC: mismatch detected, pending sync on next tick.
    SYNCING: sync operation in progress.
    PENDING_CONFIRMATION: an optimistic (ambiguous-but-treated-as-completed)
        write was issued and we are waiting for the lock to confirm it (a push
        event or hard-refresh read). The tick does not re-write while waiting;
        confirmation -> IN_SYNC, timeout -> re-sync.
    UNCONFIRMED: the lock's stack accepted the last write or clear but could
        not verify it, and nothing has shown it since. Retried on a growing
        backoff; never counted toward suspension.
    SUSPENDED: circuit breaker tripped or unexpected error; awaiting
        coordinator recovery (suspended flag cleared).
    """

    LOADING = "loading"
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    SYNCING = "syncing"
    PENDING_CONFIRMATION = "pending_confirmation"
    UNCONFIRMED = "unconfirmed"
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
        """Return True when the slot holds no code."""
        return not self.present

    @property
    def is_present(self) -> bool:
        """Return True when the slot holds a code."""
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
    callbacks: EntityCallbackRegistry = field(default_factory=EntityCallbackRegistry)
    # Cached typed view of the entry's current config; refreshed by the
    # update listener on every change. Readers should prefer this over
    # parsing config_entry.data/options directly. See data.EntryConfig.
    config: EntryConfig = field(default_factory=EntryConfig.empty)
    # Per-slot coordinators; each owns that slot's sync managers.
    slot_coordinators: dict[int, SlotEntityCoordinator] = field(default_factory=dict)
    # True once the options update listener has been registered for this
    # entry. Guards against stacking when _setup_entry_after_start runs more
    # than once (for example, a reload racing with EVENT_HOMEASSISTANT_STARTED).
    post_start_setup_done: bool = False
    # Set whenever the update listener finishes a pass. Home Assistant runs
    # update listeners as a task rather than awaiting them, so a caller that
    # writes to the entry returns before the entry has reacted -- before the
    # entities for a user it just added exist. Anything that must not return
    # early clears this, writes, and waits for it.
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    # How many listener passes are currently in flight. Writing one user is
    # several entry writes -- a subentry each, then the entry -- and Home
    # Assistant schedules a listener task per write. Only the first finds a
    # diff to act on; the rest see the config it already cached and return
    # straight away. Without this count the first of those to finish sets
    # ``settled`` while the pass that is building the entities is still
    # awaiting, which is precisely what waiting was supposed to prevent.
    passes_in_flight: int = 0
    # Update passes run one at a time under this lock, and an unload takes it
    # too, so a pass never sees a half-torn entry and an unload never
    # overlaps a pass. Once ``unloading`` is set, a pass that gets the lock
    # returns without touching anything.
    pass_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    unloading: bool = False
    # (lock, slot) pairs whose credential is to be left on the lock when the
    # slot leaves the configuration, set by the delete-user service and drained
    # by the update listener. A hand-off cannot be expressed in the new
    # configuration, because what it concerns is exactly what that
    # configuration no longer has.
    retained_pairs: set[tuple[str, int]] = field(default_factory=set)


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryRuntimeData]
