"""
Data model types for lock_code_manager.

Canonical home for dataclasses, type aliases, enums, and structured data types
used across the integration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .callbacks import EntityCallbackRegistry
from .data import EntryConfig

if TYPE_CHECKING:
    from .providers import BaseLock
    from .sync import SlotSyncManager


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
    Sentinel values for slot codes in coordinator data.

    Used alongside str values: a readable code is a plain string,
    while EMPTY and UNREADABLE_CODE represent non-string slot states.

    UNREADABLE_CODE means a code exists on the lock but its value cannot be
    read back (for example, write-only locks like Matter). This is distinct
    from Home Assistant's STATE_UNKNOWN ("unknown"), which represents a
    sensor whose state is not yet known.
    """

    EMPTY = "empty"
    UNREADABLE_CODE = "unreadable_code"


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
    # True once the options update listener has been registered for this
    # entry. Guards against stacking when _setup_entry_after_start runs more
    # than once (for example, a reload racing with EVENT_HOMEASSISTANT_STARTED).
    update_listener_registered: bool = False


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryRuntimeData]
