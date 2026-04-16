"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LOCKS, CONF_SLOTS, DOMAIN


def get_entry_data(config_entry: ConfigEntry, key: str, default: Any) -> Any:
    """
    Get data from config entry.

    Prefers options over data because during options flow updates, the new
    configuration is in options while data still contains the old configuration.
    """
    return config_entry.options.get(key, config_entry.data.get(key, default))


def get_slot_data(config_entry, slot_num: int) -> dict[str, Any]:
    """Get data for slot."""
    return get_entry_data(config_entry, CONF_SLOTS, {}).get(slot_num, {})


def get_managed_slots(hass: HomeAssistant, lock_entity_id: str) -> set[int]:
    """Return the set of slot numbers managed by any LCM config entry for a lock."""
    return {
        int(code_slot)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if lock_entity_id in get_entry_data(entry, CONF_LOCKS, [])
        for code_slot in get_entry_data(entry, CONF_SLOTS, {})
    }


def find_entry_for_lock_slot(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int
) -> ConfigEntry | None:
    """Find the config entry that manages a specific lock + slot combination.

    Returns None if no entry manages this lock/slot. There can be at most one
    due to the config entry uniqueness constraint.
    """
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if lock_entity_id in get_entry_data(entry, CONF_LOCKS, [])
            and code_slot in (int(s) for s in get_entry_data(entry, CONF_SLOTS, {}))
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class EntryConfigDiff:
    """Diff between two LCM entry configurations.

    Produced by :func:`compute_entry_config_diff`. Provides three views of
    the same diff so callers can ask the question that fits their need:

    - **By axis** (slot dict + lock list): used by the update listener,
      which adds/removes slot entities and lock providers along independent
      axes.
    - **By unchanged set**: ``slots_unchanged`` enumerates slot keys
      present in both configs, used by the listener to reconcile per-slot
      configuration changes.
    - **By cartesian pair**: ``pairs_added`` / ``pairs_removed`` give
      ``(lock, slot)`` tuples that are new or gone, which the options flow
      uses to detect existing-codes hazards on newly-added pairs (catches
      both "new slot on existing lock" and "new lock with existing slot").

    **Slot key types**: ``slots_added`` and ``slots_unchanged`` preserve
    the *new* mapping's key type. ``slots_removed`` preserves the *old*
    mapping's key type (its keys come from the old mapping). The
    cartesian pair sets always use ``int`` slot keys so they compare
    correctly even when ``old`` and ``new`` have different key types (a
    common situation: stored ``data`` is ``str``-keyed, fresh user input
    from voluptuous is ``int``-keyed).

    **Immutability**: the dataclass is frozen, and all containers are
    deeply immutable (``MappingProxyType`` for dicts, ``frozenset`` for
    sets, ``tuple`` for lists) so callers can use this safely as cached
    state without defensive copies.
    """

    slots_added: Mapping[Any, Any]
    slots_removed: Mapping[Any, Any]
    slots_unchanged: frozenset[Any]
    locks_added: tuple[str, ...]
    locks_removed: tuple[str, ...]
    pairs_added: frozenset[tuple[str, int]]
    pairs_removed: frozenset[tuple[str, int]]

    @property
    def has_changes(self) -> bool:
        """True if any slot or lock was added or removed."""
        return bool(
            self.slots_added
            or self.slots_removed
            or self.locks_added
            or self.locks_removed
        )


def compute_entry_config_diff(
    old: Mapping[str, Any], new: Mapping[str, Any]
) -> EntryConfigDiff:
    """Compute the diff between two LCM entry config mappings.

    Each input is a mapping with ``CONF_LOCKS`` (list[str]) and
    ``CONF_SLOTS`` (dict[int|str, dict]) keys. Slot keys are normalized
    to ``int`` *internally* for set comparisons (so ``str``-keyed stored
    data and ``int``-keyed voluptuous output compare correctly), but the
    slot-dict outputs preserve the source key type of ``new``. This
    matches the existing convention in the listener and entity layer
    where ``slot_num`` may be ``str`` or ``int`` depending on origin.
    """
    raw_old_slots = old.get(CONF_SLOTS, {})
    raw_new_slots = new.get(CONF_SLOTS, {})
    # int-normalized key sets for comparisons only
    old_int_keys = {int(k) for k in raw_old_slots}
    new_int_keys = {int(k) for k in raw_new_slots}
    unchanged_int_keys = old_int_keys & new_int_keys

    old_locks: list[str] = list(old.get(CONF_LOCKS, []))
    new_locks: list[str] = list(new.get(CONF_LOCKS, []))
    old_lock_set = set(old_locks)
    new_lock_set = set(new_locks)
    old_pairs: set[tuple[str, int]] = {
        (lock, slot) for lock in old_locks for slot in old_int_keys
    }
    new_pairs: set[tuple[str, int]] = {
        (lock, slot) for lock in new_locks for slot in new_int_keys
    }

    return EntryConfigDiff(
        slots_added=MappingProxyType(
            {k: v for k, v in raw_new_slots.items() if int(k) not in old_int_keys}
        ),
        slots_removed=MappingProxyType(
            {k: v for k, v in raw_old_slots.items() if int(k) not in new_int_keys}
        ),
        # Preserve the new mapping's key type — the listener's reconcile
        # loop indexes back into raw_new_slots / raw_old_slots and needs
        # the original key type to find the slot config dict.
        slots_unchanged=frozenset(
            k for k in raw_new_slots if int(k) in unchanged_int_keys
        ),
        locks_added=tuple(lock for lock in new_locks if lock not in old_lock_set),
        locks_removed=tuple(lock for lock in old_locks if lock not in new_lock_set),
        pairs_added=frozenset(new_pairs - old_pairs),
        pairs_removed=frozenset(old_pairs - new_pairs),
    )


def build_slot_unique_id(
    entry_id: str,
    slot_num: int,
    key: str,
    lock_entity_id: str | None = None,
) -> str:
    """Build the unique ID for a slot entity.

    Standard: {entry_id}|{slot_num}|{key}
    Per-lock:  {entry_id}|{slot_num}|{key}|{lock_entity_id}
    """
    uid = f"{entry_id}|{slot_num}|{key}"
    if lock_entity_id:
        uid = f"{uid}|{lock_entity_id}"
    return uid
