"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_LOCKS, CONF_SLOTS, DOMAIN

_EMPTY_SLOTS: Mapping[int, Mapping[str, Any]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class EntryConfig:
    """Typed, normalized view of an LCM entry's configuration.

    Slot keys are normalized to ``int`` at construction (the on-disk
    JSON representation uses ``str``; voluptuous-validated user input
    uses ``int``). ``slots`` is a deeply read-only mapping
    (``MappingProxyType`` at both levels) so instances can be cached
    without defensive copies.

    An instance is cached on ``LockCodeManagerConfigEntryData.config``
    and refreshed by the update listener. Most callers should access it
    via ``entry.runtime_data.config``. Iteration helpers that walk
    ``hass.config_entries.async_entries(DOMAIN)`` use
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

        Uses options-preferred precedence: during options-flow updates
        the new config is in ``options`` while ``data`` still holds the
        old config.
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

    def __sub__(self, other: EntryConfig) -> EntryConfigDiff:
        """Return the diff from ``self`` (old) to ``other`` (new).

        Sugar for ``EntryConfigDiff(old=self, new=other)``. Reads as
        ``old_config - new_config`` — note this is a *delta* (both adds
        and removes), not strict set subtraction. The result includes
        what changed in either direction.

        Returns ``NotImplemented`` for non-``EntryConfig`` operands so
        Python's operator protocol falls back to ``__rsub__`` and
        ultimately raises a clear ``TypeError`` rather than letting the
        misuse fail deep inside ``EntryConfigDiff.__post_init__``.
        """
        if not isinstance(other, EntryConfig):
            return NotImplemented
        return EntryConfigDiff(old=self, new=other)

    def with_slot_field_set(
        self, slot_num: int | str, key: str, value: Any
    ) -> EntryConfig:
        """Return a new EntryConfig with one slot's field set to ``value``.

        Creates the slot if it doesn't already exist. Used by writer
        paths (entity field updates, condition entity set service) to
        produce the new config to hand to ``async_update_entry``, paired
        with :meth:`to_dict`.
        """
        sn = int(slot_num)
        new_slots: dict[int, dict[str, Any]] = {
            k: dict(v) for k, v in self.slots.items()
        }
        new_slots.setdefault(sn, {})[key] = value
        return EntryConfig(
            locks=self.locks,
            slots=MappingProxyType(
                {k: MappingProxyType(v) for k, v in new_slots.items()}
            ),
        )

    def with_slot_field_removed(self, slot_num: int | str, key: str) -> EntryConfig:
        """Return a new EntryConfig with one slot's field removed.

        No-op (returns ``self``) if the slot or key is already absent.
        """
        sn = int(slot_num)
        if sn not in self.slots or key not in self.slots[sn]:
            return self
        new_slots: dict[int, dict[str, Any]] = {
            k: dict(v) for k, v in self.slots.items()
        }
        new_slots[sn].pop(key, None)
        return EntryConfig(
            locks=self.locks,
            slots=MappingProxyType(
                {k: MappingProxyType(v) for k, v in new_slots.items()}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a plain mutable dict suitable for ``async_update_entry``.

        Only includes the keys EntryConfig knows about (CONF_LOCKS and
        CONF_SLOTS). Inner slot dicts are plain ``dict`` (not
        ``MappingProxyType``) so HA's storage layer can serialize them.

        Callers preserving other top-level keys in ``entry.data`` /
        ``entry.options`` should merge: ``{**dict(entry.data),
        **new_config.to_dict()}``. In practice LCM entries only carry
        these two keys.
        """
        return {
            CONF_LOCKS: list(self.locks),
            CONF_SLOTS: {k: dict(v) for k, v in self.slots.items()},
        }


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


def get_managed_slots(hass: HomeAssistant, lock_entity_id: str) -> set[int]:
    """Return the set of slot numbers managed by any LCM config entry for a lock."""
    return {
        slot_num
        for entry in hass.config_entries.async_entries(DOMAIN)
        if (config := get_entry_config(entry)).has_lock(lock_entity_id)
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
            if (config := get_entry_config(entry)).has_lock(lock_entity_id)
            and config.has_slot(code_slot)
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class EntryConfigDiff:
    """Diff between two LCM entry configurations.

    Constructed directly from the two configs being compared:

    .. code-block:: python

        diff = EntryConfigDiff(old=current_config, new=proposed_config)
        # or via the operator sugar on EntryConfig:
        diff = current_config - proposed_config

    Either side may be omitted (defaults to :meth:`EntryConfig.empty`)
    for the "all added" / "all removed" cases — for example,
    ``EntryConfigDiff(new=cfg)`` reads as "diff from nothing to cfg".

    Provides three views of the same diff so callers can ask the
    question that fits their need:

    - **By axis** (slot dict + lock list): used by the update listener,
      which adds/removes slot entities and lock providers along independent
      axes.
    - **By unchanged set**: ``slots_unchanged`` enumerates slot numbers
      present in both configs, used by the listener to reconcile per-slot
      configuration changes.
    - **By cartesian pair**: ``pairs_added`` / ``pairs_removed`` give
      ``(lock, slot)`` tuples that are new or gone, which the options flow
      uses to detect existing-codes hazards on newly-added pairs (catches
      both "new slot on existing lock" and "new lock with existing slot").

    All slot keys are ``int``, inherited from :class:`EntryConfig`.
    """

    old: EntryConfig = field(default_factory=EntryConfig.empty)
    new: EntryConfig = field(default_factory=EntryConfig.empty)

    # Computed in __post_init__ from old/new
    slots_added: Mapping[int, Mapping[str, Any]] = field(init=False)
    slots_removed: Mapping[int, Mapping[str, Any]] = field(init=False)
    slots_unchanged: frozenset[int] = field(init=False)
    locks_added: tuple[str, ...] = field(init=False)
    locks_removed: tuple[str, ...] = field(init=False)
    pairs_added: frozenset[tuple[str, int]] = field(init=False)
    pairs_removed: frozenset[tuple[str, int]] = field(init=False)

    def __post_init__(self) -> None:
        """Compute and freeze the diff fields."""
        old_slots = self.old.slots
        new_slots = self.new.slots
        old_keys = old_slots.keys()
        new_keys = new_slots.keys()
        old_lock_set = set(self.old.locks)
        new_lock_set = set(self.new.locks)
        old_pairs: set[tuple[str, int]] = {
            (lock, slot) for lock in self.old.locks for slot in old_keys
        }
        new_pairs: set[tuple[str, int]] = {
            (lock, slot) for lock in self.new.locks for slot in new_keys
        }

        # dict(v) + MappingProxyType wrapping snapshots inner slot configs
        # so caller-side mutation can't leak into the diff view.
        set_field = object.__setattr__
        set_field(
            self,
            "slots_added",
            MappingProxyType(
                {
                    k: MappingProxyType(dict(v))
                    for k, v in new_slots.items()
                    if k not in old_slots
                }
            ),
        )
        set_field(
            self,
            "slots_removed",
            MappingProxyType(
                {
                    k: MappingProxyType(dict(v))
                    for k, v in old_slots.items()
                    if k not in new_slots
                }
            ),
        )
        set_field(self, "slots_unchanged", frozenset(old_keys & new_keys))
        set_field(
            self,
            "locks_added",
            tuple(lock for lock in self.new.locks if lock not in old_lock_set),
        )
        set_field(
            self,
            "locks_removed",
            tuple(lock for lock in self.old.locks if lock not in new_lock_set),
        )
        set_field(self, "pairs_added", frozenset(new_pairs - old_pairs))
        set_field(self, "pairs_removed", frozenset(old_pairs - new_pairs))

    @property
    def has_changes(self) -> bool:
        """True if any slot or lock was added or removed."""
        return bool(
            self.slots_added
            or self.slots_removed
            or self.locks_added
            or self.locks_removed
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
