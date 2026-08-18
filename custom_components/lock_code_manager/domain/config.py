"""Config entry data types for lock_code_manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME

from ..const import CONF_LOCKS, CONF_SLOTS, CONF_USERS
from .names import normalize_name
from .slot_assignment import CONF_SLOT_ASSIGNMENT, SlotAssignment, _identity

_EMPTY_USERS: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
_EMPTY_EXTRA: Mapping[str, Any] = MappingProxyType({})

# The keys EntryConfig models directly. Everything else in the entry is
# internal bookkeeping and rides along in `extra`.
_CONFIG_KEYS = frozenset({CONF_LOCKS, CONF_SLOTS, CONF_USERS, CONF_SLOT_ASSIGNMENT})


def _users_from_slot_shape(
    raw_slots: Mapping[Any, Mapping[str, Any]],
) -> tuple[Mapping[str, Mapping[str, Any]], SlotAssignment]:
    """
    Accept the pre-version-3 slot-keyed shape as INPUT.

    One-directional and temporary. Nothing writes this shape any more --
    :meth:`EntryConfig.to_dict` only ever emits the user-keyed one -- so there
    is a single storage format and a single internal model. This exists purely
    because the config flow still assembles slot-keyed input; it goes when
    that does, and its absence will be a loud KeyError rather than a silent
    reversion.

    An unnamed slot falls back to its generated name rather than being
    dropped, because a version 2 entry is allowed to have one.
    """
    users: dict[str, dict[str, Any]] = {}
    assignment: dict[str, int] = {}
    for raw_num, slot in sorted(raw_slots.items(), key=lambda kv: int(kv[0])):
        slot_num = int(raw_num)
        name = normalize_name(slot.get(CONF_NAME)) or f"User {slot_num}"
        users[name] = {k: v for k, v in slot.items() if k != CONF_NAME}
        assignment[name] = slot_num
    return MappingProxyType(users), SlotAssignment(slots=assignment)


@dataclass(frozen=True, slots=True)
class EntryConfig:
    """
    Typed, normalized view of an LCM entry's configuration.

    Keyed by USER. The name is the identity: it is what the configuration is
    stored under, what the dashboard shows, and what every layer above the
    provider boundary works in. The slot number survives as internal
    bookkeeping in :attr:`assignment`, and is looked up at exactly two places
    -- provider writes, where it IS the lock's credential index, and registry
    identifiers, which key on it so a rename moves nothing.

    Deeply read-only (``MappingProxyType`` throughout) so instances can be
    cached without defensive copies. An instance is cached on
    ``LockCodeManagerConfigEntryRuntimeData.config`` and refreshed by the
    update listener; most callers should reach it via
    ``entry.runtime_data.config``, or :func:`get_entry_config` for entries
    that may not be loaded.
    """

    locks: tuple[str, ...]
    users: Mapping[str, Mapping[str, Any]]
    assignment: SlotAssignment
    # Every other top-level key in the entry, carried through verbatim.
    #
    # Deliberately has NO default. to_dict() feeds async_update_entry
    # directly, so a field that could be forgotten at a construction site
    # would silently ERASE whatever it holds on the next write. That is not
    # hypothetical -- it erased the migrated shape on every listener pass and
    # sent setup into a loop. Requiring it makes mypy the check, not memory.
    extra: Mapping[str, Any]

    @classmethod
    def empty(cls) -> EntryConfig:
        """Return a config for an entry with no locks and no users."""
        return cls(
            locks=(),
            users=_EMPTY_USERS,
            assignment=SlotAssignment.empty(),
            extra=_EMPTY_EXTRA,
        )

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> EntryConfig:
        """
        Build EntryConfig from a config entry, options-preferred.

        During an options-flow update the new configuration is in ``options``
        while ``data`` still holds the old one. Bookkeeping is merged from
        both sides rather than taken options-preferred: setup moves everything
        from data into options and the listener moves it back, so which side
        holds it depends on where in that cycle we are.
        """
        merged: dict[str, Any] = {
            **{k: v for k, v in entry.data.items() if k not in _CONFIG_KEYS},
            **{k: v for k, v in entry.options.items() if k not in _CONFIG_KEYS},
            CONF_LOCKS: entry.options.get(CONF_LOCKS, entry.data.get(CONF_LOCKS, [])),
        }
        for key in (CONF_USERS, CONF_SLOT_ASSIGNMENT, CONF_SLOTS):
            # Only set when a side actually HAS it. Defaulting to an empty
            # mapping would make "absent" indistinguishable from "empty", and
            # from_mapping would read an entry with users as having none.
            value = entry.options.get(key, entry.data.get(key))
            if value is not None:
                merged[key] = value
        return cls.from_mapping(merged)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> EntryConfig:
        """
        Build EntryConfig from a raw config mapping.

        Accepts the pre-version-3 slot-keyed shape as input only; see
        :func:`_users_from_slot_shape`. Nothing writes that shape any more.
        """
        raw_users = mapping.get(CONF_USERS)
        if raw_users is None:
            users, assignment = _users_from_slot_shape(mapping.get(CONF_SLOTS) or {})
        else:
            users = MappingProxyType(
                {
                    normalize_name(name): MappingProxyType(dict(user))
                    for name, user in raw_users.items()
                }
            )
            assignment = SlotAssignment.from_mapping(mapping)
        return cls(
            locks=tuple(mapping.get(CONF_LOCKS, [])),
            users=MappingProxyType(
                {name: MappingProxyType(dict(user)) for name, user in users.items()}
            ),
            assignment=assignment,
            extra=MappingProxyType(
                {k: v for k, v in mapping.items() if k not in _CONFIG_KEYS}
            ),
        )

    @property
    def slots(self) -> Mapping[int, Mapping[str, Any]]:
        """
        Return the slot-keyed view.

        TEMPORARY, and derived rather than stored -- there is one model, and
        this is a projection of it. It exists so the twenty-eight remaining
        slot-keyed call sites can move one at a time instead of in a single
        change nobody could review. Delete it, and this docstring, once they
        have.

        A user holding no slot is omitted: inventing a number would put their
        code on a credential index somebody else may hold.
        """
        return MappingProxyType(
            {
                slot_num: MappingProxyType({CONF_NAME: name, **user})
                for name, user in self.users.items()
                if (slot_num := self.assignment.slot(name)) is not None
            }
        )

    def has_lock(self, lock_entity_id: str) -> bool:
        """Return True if this entry manages the given lock."""
        return lock_entity_id in self.locks

    def has_user(self, name: str) -> bool:
        """Return True if this entry configures a user with that name."""
        return _identity(name) in {_identity(known) for known in self.users}

    def user(self, name: str) -> Mapping[str, Any]:
        """Return a user's configuration, or an empty mapping if absent."""
        wanted = _identity(name)
        return next(
            (user for known, user in self.users.items() if _identity(known) == wanted),
            _EMPTY_EXTRA,
        )

    def slot_for(self, name: str) -> int | None:
        """Return the slot number a user occupies, or None if they hold none."""
        return self.assignment.slot(name)

    def name_for(self, slot_num: int | str) -> str | None:
        """Return the user occupying a slot, or None if it is unoccupied."""
        wanted = int(slot_num)
        return next(
            (name for name in self.users if self.assignment.slot(name) == wanted),
            None,
        )

    def has_slot(self, slot_num: int | str) -> bool:
        """Return True if a user occupies the given slot number."""
        return self.name_for(slot_num) is not None

    def slot(self, slot_num: int | str) -> Mapping[str, Any]:
        """Return the slot-shaped view of whoever occupies ``slot_num``."""
        name = self.name_for(slot_num)
        if name is None:
            return _EMPTY_EXTRA
        return MappingProxyType({CONF_NAME: name, **self.user(name)})

    def __sub__(self, other: EntryConfig) -> EntryConfigDiff:
        """
        Return the diff from ``self`` (old) to ``other`` (new).

        Sugar for ``EntryConfigDiff(old=self, new=other)``. Reads as
        ``old_config - new_config`` — note this is a *delta* (both adds
        and removes), not strict set subtraction.

        Returns ``NotImplemented`` for non-``EntryConfig`` operands so
        Python's operator protocol raises a clear ``TypeError`` rather than
        failing deep inside ``EntryConfigDiff.__post_init__``.
        """
        if not isinstance(other, EntryConfig):
            return NotImplemented
        return EntryConfigDiff(old=self, new=other)

    def with_user_field_set(self, name: str, key: str, value: Any) -> EntryConfig:
        """Return a copy with one user's field set, creating the user if new."""
        stored = next(
            (known for known in self.users if _identity(known) == _identity(name)),
            normalize_name(name),
        )
        new_users = {k: dict(v) for k, v in self.users.items()}
        new_users.setdefault(stored, {})[key] = value
        return EntryConfig(
            locks=self.locks,
            users=MappingProxyType(
                {k: MappingProxyType(v) for k, v in new_users.items()}
            ),
            assignment=self.assignment,
            extra=self.extra,
        )

    def with_user_field_removed(self, name: str, key: str) -> EntryConfig:
        """Return a copy with one user's field removed; a no-op if absent."""
        stored = next(
            (known for known in self.users if _identity(known) == _identity(name)), None
        )
        if stored is None or key not in self.users[stored]:
            return self
        new_users = {k: dict(v) for k, v in self.users.items()}
        new_users[stored].pop(key, None)
        return EntryConfig(
            locks=self.locks,
            users=MappingProxyType(
                {k: MappingProxyType(v) for k, v in new_users.items()}
            ),
            assignment=self.assignment,
            extra=self.extra,
        )

    def with_slot_field_set(
        self, slot_num: int | str, key: str, value: Any
    ) -> EntryConfig:
        """Set a field on whoever occupies ``slot_num``. TEMPORARY, as :attr:`slots`."""
        name = self.name_for(slot_num)
        if name is None:
            return self
        return self.with_user_field_set(name, key, value)

    def with_slot_field_removed(self, slot_num: int | str, key: str) -> EntryConfig:
        """Remove a field from whoever occupies ``slot_num``. TEMPORARY."""
        name = self.name_for(slot_num)
        if name is None:
            return self
        return self.with_user_field_removed(name, key)

    def to_dict(self) -> dict[str, Any]:
        """
        Return a plain mutable dict suitable for ``async_update_entry``.

        Emits the user-keyed shape only. There is one storage format, so a
        write can never revert the entry to a previous one.
        """
        return {
            **dict(self.extra),
            CONF_LOCKS: list(self.locks),
            CONF_USERS: {name: dict(user) for name, user in self.users.items()},
            CONF_SLOT_ASSIGNMENT: dict(self.assignment.slots),
        }


@dataclass(frozen=True, slots=True)
class EntryConfigDiff:
    """
    Diff between two LCM entry configurations.

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
    """
    Build the unique ID for a slot entity.

    Standard: {entry_id}|{slot_num}|{key}
    Per-lock:  {entry_id}|{slot_num}|{key}|{lock_entity_id}
    """
    uid = f"{entry_id}|{slot_num}|{key}"
    if lock_entity_id:
        uid = f"{uid}|{lock_entity_id}"
    return uid


def build_slot_device_identifier(entry_id: str, slot_num: int) -> str:
    """
    Build the device registry identifier for a slot's device.

    Format: {entry_id}|{slot_num}. Deliberately distinct from the entry's
    own device identifier, which is the bare entry_id.
    """
    return f"{entry_id}|{slot_num}"


def parse_slot_device_identifier(entry_id: str, identifier: str) -> int | None:
    """
    Recover the slot number from a slot device identifier, else ``None``.

    The inverse of :func:`build_slot_device_identifier`, used to tell a slot
    device apart from the entry's own device when sweeping the registry.
    ``None`` covers both the entry device (bare entry_id, no separator) and
    anything that does not belong to this entry.

    Accepts exactly what the builder emits, verified by round-tripping the
    parsed number back to a string. Anything looser breaks the pairing in one
    direction or the other: ``str.isdigit()`` rejects the negative slot the
    builder will happily encode (the slots YAML schema does not bound the key),
    while a bare ``int()`` accepts ``+1`` and ``1_0`` as aliases the builder
    would never produce. Either way the device becomes invisible to the sweep
    AND to the removal hook -- the exact stuck, undeletable device this pairing
    exists to clean up.
    """
    prefix = f"{entry_id}|"
    if not identifier.startswith(prefix):
        return None
    suffix = identifier.removeprefix(prefix)
    try:
        slot_num = int(suffix)
    except ValueError:
        return None
    return slot_num if str(slot_num) == suffix else None
