"""
The config entry's runtime data and the typed ``ConfigEntry`` alias.

Lives in the HA layer because the runtime data holds the entry's live
objects: provider instances, the entity callback registry and the slot
coordinators.
"""

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

from ..model.config import EntryConfig
from .callbacks import EntityCallbackRegistry

if TYPE_CHECKING:
    from ..providers import BaseLock
    from .slot_coordinator import SlotEntityCoordinator


@dataclass
class LockCodeManagerConfigEntryRuntimeData:
    """Runtime data for a Lock Code Manager config entry."""

    locks: dict[str, BaseLock] = field(default_factory=dict)
    callbacks: EntityCallbackRegistry = field(default_factory=EntityCallbackRegistry)
    # Cached typed view of the entry's current config; refreshed by the
    # update listener on every change. Readers should prefer this over
    # parsing config_entry.data/options directly. See data.EntryConfig.
    config: EntryConfig = field(default_factory=EntryConfig.empty)
    # Per-slot coordinators; each tells the locks what its slot wants.
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
