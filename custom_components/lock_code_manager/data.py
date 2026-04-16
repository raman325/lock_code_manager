"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LOCKS, CONF_SLOTS, DOMAIN

_EMPTY_SLOTS: Mapping[int, Mapping[str, Any]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class EntryConfig:
    """Typed, normalized view of an LCM entry's configuration.

    Single chokepoint for reading entry config: the on-disk representation
    has ``str`` slot keys (JSON storage) while voluptuous-validated user
    input has ``int`` keys. ``EntryConfig`` normalizes to ``int`` keys
    once at construction so every downstream consumer can treat slot
    numbers uniformly. The defensive ``slot if slot in d else str(slot)``
    patterns scattered across the codebase exist precisely because there
    was no such chokepoint before.

    ``slots`` is a deeply read-only mapping (``MappingProxyType`` at both
    levels) so callers can keep an ``EntryConfig`` as cached state without
    defensive copies.

    Lifecycle: an instance is cached on
    ``LockCodeManagerConfigEntryData.config`` and refreshed by the update
    listener whenever the entry is mutated. Most callers should access it
    via ``entry.runtime_data.config`` directly. Iteration helpers that
    walk ``hass.config_entries.async_entries(DOMAIN)`` use
    :func:`get_entry_config` to handle the unloaded-entry case.
    """

    locks: tuple[str, ...]
    slots: Mapping[int, Mapping[str, Any]]

    @classmethod
    def empty(cls) -> EntryConfig:
        """Return a config representing an entry with no locks or slots.

        Used as the initial value for ``runtime_data.config`` before the
        entry's data has been read.
        """
        return cls(locks=(), slots=_EMPTY_SLOTS)

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> EntryConfig:
        """Build EntryConfig from a config entry, options-preferred.

        Matches the precedence used by :func:`get_entry_data`: during
        options-flow updates the new config is in ``options`` while
        ``data`` still holds the old config.
        """
        return cls.from_mapping(
            {
                CONF_LOCKS: entry.options.get(
                    CONF_LOCKS, entry.data.get(CONF_LOCKS, [])
                ),
                CONF_SLOTS: entry.options.get(
                    CONF_SLOTS, entry.data.get(CONF_SLOTS, {})
                ),
            }
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> EntryConfig:
        """Build EntryConfig from a raw config mapping (data, options, or input).

        Slot keys are normalized to ``int`` regardless of source. Inner
        slot config dicts are wrapped in ``MappingProxyType`` so the
        whole structure is read-only.
        """
        raw_slots = mapping.get(CONF_SLOTS, {})
        raw_locks = mapping.get(CONF_LOCKS, [])
        return cls(
            locks=tuple(raw_locks),
            slots=MappingProxyType(
                {int(k): MappingProxyType(dict(v)) for k, v in raw_slots.items()}
            ),
        )

    def has_lock(self, lock_entity_id: str) -> bool:
        """Return True if this entry manages the given lock."""
        return lock_entity_id in self.locks

    def has_slot(self, slot_num: int | str) -> bool:
        """Return True if this entry manages the given slot number.

        Accepts ``int`` or ``str`` to absorb the slot-key type variance
        in the codebase (entities created during the listener may carry
        either type as ``self.slot_num``). Internal storage is always
        ``int``-keyed.
        """
        return int(slot_num) in self.slots

    def slot(self, slot_num: int | str) -> Mapping[str, Any]:
        """Return the slot config dict, or an empty mapping if absent.

        Like :meth:`has_slot`, accepts ``int`` or ``str`` so callers
        don't need to cast at every read site.
        """
        return self.slots.get(int(slot_num), {})


def get_entry_config(entry: ConfigEntry) -> EntryConfig:
    """Return the EntryConfig view of ``entry``.

    Prefers the cached instance on ``entry.runtime_data.config`` (set by
    the listener during setup and on every update) and falls back to
    constructing fresh from the raw entry data. The fallback covers
    iteration over ``hass.config_entries.async_entries(DOMAIN)`` which
    may yield entries that haven't been loaded yet (or are mid-teardown)
    and so don't have ``runtime_data`` populated.
    """
    cached = getattr(getattr(entry, "runtime_data", None), "config", None)
    if isinstance(cached, EntryConfig):
        return cached
    return EntryConfig.from_entry(entry)


def get_entry_data(config_entry: ConfigEntry, key: str, default: Any) -> Any:
    """
    Get data from config entry.

    Prefers options over data because during options flow updates, the new
    configuration is in options while data still contains the old configuration.
    """
    return config_entry.options.get(key, config_entry.data.get(key, default))


def get_slot_data(config_entry, slot_num: int | str) -> Mapping[str, Any]:
    """Get the slot config dict for ``slot_num`` (empty mapping if absent).

    Thin wrapper around :meth:`EntryConfig.slot` for callers that don't
    have an :class:`EntryConfig` in hand.
    """
    return get_entry_config(config_entry).slot(slot_num)


def get_managed_slots(hass: HomeAssistant, lock_entity_id: str) -> set[int]:
    """Return the set of slot numbers managed by any LCM config entry for a lock."""
    return {
        slot_num
        for entry in hass.config_entries.async_entries(DOMAIN)
        for config in [get_entry_config(entry)]
        if config.has_lock(lock_entity_id)
        for slot_num in config.slots
    }


def find_entry_for_lock_slot(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int | str
) -> ConfigEntry | None:
    """Find the config entry that manages a specific lock + slot combination.

    Returns None if no entry manages this lock/slot. There can be at most one
    due to the config entry uniqueness constraint.
    """
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            for config in [get_entry_config(entry)]
            if config.has_lock(lock_entity_id) and config.has_slot(code_slot)
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
