"""Config entry data types for lock_code_manager."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigSubentry,
    ConfigSubentryData,
)
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback

from ..const import CONF_LOCKS, CONF_SLOT, CONF_SLOTS, CONF_USERS, SUBENTRY_TYPE_USER
from .names import normalize_name
from .slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
    SlotAssignment,
    identity,
    users_from_slots,
)

_LOGGER = logging.getLogger(__name__)

_EMPTY_USERS: Mapping[str, Mapping[str, Any]] = MappingProxyType({})
_EMPTY_EXTRA: Mapping[str, Any] = MappingProxyType({})

# The keys EntryConfig models directly. Everything else in the entry is
# internal bookkeeping and rides along in `extra`.
_CONFIG_KEYS = frozenset({CONF_LOCKS, CONF_SLOTS, CONF_USERS, CONF_SLOT_ASSIGNMENT})


def _normalized_user(user: Mapping[str, Any]) -> dict[str, Any]:
    """
    Return a user's stored fields with the PIN stripped.

    Stripping only where a PIN is written makes the invariant true going
    forward, which leaves it dependent on when a value happened to be
    written: config hand-edited in ``.storage``, restored from a backup, or
    written by a version that predates the write-path strip all still carry
    padding. Stripping on read makes it true regardless of provenance, and
    every read of a stored PIN -- validation, the sync's desired credential,
    the dashboard -- resolves through here.

    A whitespace-only PIN collapses to empty, which is already how "this
    user has no PIN" is spelled, rather than to a truthy value nobody can
    type. A non-string PIN is left alone: coercing it would hide a
    malformed entry, and raising would take entry setup down with it.
    """
    normalized = dict(user)
    if isinstance(pin := normalized.get(CONF_PIN), str):
        normalized[CONF_PIN] = pin.strip()
    return normalized


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
    # Every other top-level key in the entry, carried through verbatim. No
    # default: to_dict() feeds async_update_entry directly, so a field that
    # could be omitted at a construction site would erase what it holds.
    extra: Mapping[str, Any]
    # Derived once at construction: the instance is immutable and cached, and
    # the slot view is read per entry per lock on some paths.
    _by_slot: Mapping[int, Mapping[str, Any]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the slot-keyed projection."""
        by_slot: dict[int, Mapping[str, Any]] = {}
        for name, user in self.users.items():
            slot_num = self.assignment.slot(name)
            if slot_num is None:
                # No number: inventing one would put this user's code on a
                # credential index somebody else may hold.
                continue
            # Key LAST so it wins: a `name` surviving inside a user dict must
            # not shadow the identity this user is stored under.
            by_slot[slot_num] = MappingProxyType({**user, CONF_NAME: name})
        object.__setattr__(self, "_by_slot", MappingProxyType(by_slot))

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
        Build EntryConfig from a config entry and its user subentries.

        Users live in subentries; the locks and everything else live on the
        entry. Each user's credential position is carried in their own
        subentry rather than in a side table, so the two can no longer
        disagree about who holds which number.

        The entry side is still read options-first, because an options-flow
        update leaves the new configuration in ``options`` while ``data``
        still holds the old one.

        An entry from before users had subentries is read the way it was
        written. Migration only runs when an entry is set up, so a disabled
        entry keeps its old shape for as long as it stays disabled -- and it
        is still consulted: ``get_managed_slots`` asks every entry which
        numbers it holds on a lock, and an answer of "none" from an entry
        that holds five lets a new entry be issued the same five, and the
        unmanaged-code sweep raise a repair to delete each one.

        Told apart by shape, not by ``entry.version``. Shape is what this
        reads; the version is a proxy for it, and one the test fixtures do
        not keep -- they build subentries onto entries left at version 1.
        The shape is unambiguous because migration strips the legacy keys
        as it moves users out, so an entry with none of them has nothing
        to fall back to and an entry with them has not been migrated.
        """
        user_subentries = [
            subentry
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_USER
        ]
        if not user_subentries and any(
            key in side
            for side in (entry.options, entry.data)
            for key in (CONF_USERS, CONF_SLOTS)
        ):
            return cls.from_mapping({**entry.data, **entry.options})

        users: dict[str, dict[str, Any]] = {}
        numbers: dict[str, int] = {}
        for subentry in user_subentries:
            name = normalize_name(subentry.title)
            fields = {
                key: value for key, value in subentry.data.items() if key != CONF_SLOT
            }
            users[name] = _normalized_user(fields)
            if (slot := subentry.data.get(CONF_SLOT)) is not None:
                numbers[name] = slot

        return cls(
            locks=tuple(entry.options.get(CONF_LOCKS, entry.data.get(CONF_LOCKS, []))),
            users=MappingProxyType(
                {name: MappingProxyType(fields) for name, fields in users.items()}
            ),
            assignment=SlotAssignment(numbers),
            extra=MappingProxyType(
                {
                    key: value
                    for side in (entry.data, entry.options)
                    for key, value in side.items()
                    if key not in _CONFIG_KEYS
                }
            ),
        )

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> EntryConfig:
        """
        Build EntryConfig from a raw config mapping.

        Accepts the slot-keyed shape as INPUT, converted on the way in. The
        YAML and options flows still submit it; nothing stores it. That
        conversion goes when those flows stop producing it.
        """
        raw_users = mapping.get(CONF_USERS)
        if raw_users is None:
            # Converted by the migration's own function so the two cannot
            # disagree about what a slot-keyed entry means.
            converted, assignment, _, _ = users_from_slots(
                mapping.get(CONF_SLOTS) or {}
            )
            users = dict(converted)
        else:
            # Two keys reducing to one identity keep the FIRST. Both
            # surviving would put them on one slot while the name lookups
            # disagreed about which holds it, so a display would show one user
            # and a write would land on the other.
            users = {}
            seen: set[str] = set()
            for name, user in raw_users.items():
                normalized = normalize_name(name)
                if (key := identity(normalized)) in seen:
                    continue
                seen.add(key)
                users[normalized] = dict(user)
            assignment = SlotAssignment.from_mapping(mapping)
        return cls(
            locks=tuple(mapping.get(CONF_LOCKS, [])),
            users=MappingProxyType(
                {
                    name: MappingProxyType(_normalized_user(user))
                    for name, user in users.items()
                }
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

        A projection of the one model, not a second model. It exists for the
        consumers that are slot-shaped by nature rather than by habit: the
        entity lifecycle diff, which decides what to create and remove by
        slot because entity identifiers key on it; the YAML editor, which
        edits a slot-keyed document; and diagnostics. Everything that only
        wanted to know WHICH numbers exist uses :attr:`slot_numbers`.
        """
        return self._by_slot

    @property
    def slot_numbers(self) -> frozenset[int]:
        """
        Return the slot numbers this entry's users occupy.

        The answer most callers of the slot-shaped view actually wanted: the
        registries key on the number, so plenty of code needs to know which
        numbers exist without caring who holds them.
        """
        return frozenset(self._by_slot)

    def has_lock(self, lock_entity_id: str) -> bool:
        """Return True if this entry manages the given lock."""
        return lock_entity_id in self.locks

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
        return self._by_slot.get(int(slot_num), _EMPTY_EXTRA)

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

    def with_user_renamed(self, old: str, new: str) -> EntryConfig:
        """
        Return a copy with a user re-keyed, keeping their slot.

        Re-keys the configuration and the assignment together, so the slot
        follows the person. Neither registry moves: identifiers key on the
        slot number.
        """
        stored = next(
            (known for known in self.users if identity(known) == identity(old)), None
        )
        if stored is None:
            return self
        renamed = normalize_name(new)
        # Renaming onto somebody else would collapse two users into one key,
        # deleting one and freeing their slot.
        if any(
            identity(known) == identity(renamed) and known != stored
            for known in self.users
        ):
            return self
        new_users = {
            (renamed if k == stored else k): dict(v) for k, v in self.users.items()
        }
        # Re-keyed directly rather than through reconcile, which allocates: a
        # rename moves a number, it never issues one.
        moved = {
            (identity(renamed) if name == identity(stored) else name): slot
            for name, slot in self.assignment.slots.items()
        }
        return EntryConfig(
            locks=self.locks,
            users=MappingProxyType(
                {k: MappingProxyType(v) for k, v in new_users.items()}
            ),
            assignment=SlotAssignment(slots=moved),
            extra=self.extra,
        )

    def with_user_field_set(self, name: str, key: str, value: Any) -> EntryConfig:
        """
        Return a copy with one user's field set.

        Setting the NAME re-keys the user rather than storing a field: the
        name is the identity, and it is what both the configuration and the
        assignment are keyed by.

        A user who does not exist is not created. Creating one also has to
        allocate a slot, which this has no view to do.
        """
        if key == CONF_NAME:
            return self.with_user_renamed(name, value)
        stored = next(
            (known for known in self.users if identity(known) == identity(name)), None
        )
        if stored is None:
            return self
        new_users = {k: dict(v) for k, v in self.users.items()}
        new_users[stored][key] = value
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
            (known for known in self.users if identity(known) == identity(name)), None
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
        """Set a field on whoever occupies ``slot_num``."""
        name = self.name_for(slot_num)
        if name is None:
            return self
        return self.with_user_field_set(name, key, value)

    def with_slot_field_removed(self, slot_num: int | str, key: str) -> EntryConfig:
        """Remove a field from whoever occupies ``slot_num``."""
        name = self.name_for(slot_num)
        if name is None:
            return self
        return self.with_user_field_removed(name, key)

    def to_dict(self) -> dict[str, Any]:
        """
        Return the ENTRY's own data, suitable for ``async_update_entry``.

        Users are not in here: they live in subentries, and
        :func:`async_write_entry_config` is what puts them there. This carries
        only what belongs to the entry itself, so a caller cannot write a user
        into entry data by accident and give the entry two answers about who
        holds a slot.
        """
        return {
            **dict(self.extra),
            CONF_LOCKS: list(self.locks),
        }

    def subentry_data(self, name: str) -> dict[str, Any]:
        """Return the subentry ``data`` for one user, number included."""
        data = dict(self.users[name])
        if (slot_num := self.assignment.slot(name)) is not None:
            data[CONF_SLOT] = slot_num
        return data

    def subentries(self) -> tuple[ConfigSubentryData, ...]:
        """
        Return this config's users as the subentries an entry holds them in.

        For a flow creating an entry, which hands Home Assistant the whole
        entry at once and so cannot go through
        :func:`async_write_entry_config`.
        """
        return tuple(
            ConfigSubentryData(
                data=self.subentry_data(name),
                subentry_type=SUBENTRY_TYPE_USER,
                title=name,
                unique_id=None,
            )
            for name in self.users
        )


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
    - **By cartesian pair**: ``pairs_removed`` gives the ``(lock, slot)``
      tuples that are gone, which the update listener uses to release the
      lock-side state a slot owned (catches both "slot dropped from an
      existing lock" and "lock dropped while its slots remain").

    All slot keys are ``int``, inherited from :class:`EntryConfig`.
    """

    old: EntryConfig = field(default_factory=EntryConfig.empty)
    new: EntryConfig = field(default_factory=EntryConfig.empty)

    # Computed in __post_init__ from old/new
    slots_added: Mapping[int, Mapping[str, Any]] = field(init=False)
    slots_removed: Mapping[int, Mapping[str, Any]] = field(init=False)
    locks_added: tuple[str, ...] = field(init=False)
    locks_removed: tuple[str, ...] = field(init=False)
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


def parse_slot_unique_id(entry_id: str, unique_id: str) -> int | None:
    """
    Recover the slot number from a slot entity's unique ID, else ``None``.

    Reads what :func:`build_slot_unique_id` writes, in both its shapes: the
    key and any trailing lock entity ID are ignored, so a per-lock entity
    resolves to the same slot as the shared entities beside it.

    Applies the same round-trip check as
    :func:`parse_slot_device_identifier`, for the same reason -- a looser
    parse claims slot numbers the builder would never emit, and this decides
    which device an entity is moved to.
    """
    prefix = f"{entry_id}|"
    if not unique_id.startswith(prefix):
        return None
    suffix = unique_id.removeprefix(prefix).split("|", 1)[0]
    try:
        slot_num = int(suffix)
    except ValueError:
        return None
    return slot_num if str(slot_num) == suffix else None


@callback
def async_write_entry_config(
    hass: HomeAssistant, entry: ConfigEntry, config: EntryConfig
) -> bool:
    """
    Reconcile an entry and its user subentries to ``config``.

    The single write path. Callers build the configuration they want and hand
    it over; deciding what belongs on the entry and what belongs in a subentry
    happens here once, rather than at each of the places that write.

    Returns whether anything actually changed, so a caller waiting for the
    entry to react knows whether to expect it to.

    Users are matched to existing subentries by the NUMBER they hold, falling
    back to identity for anyone who has not been given one. Matching on the
    name would make a rename look like a departure and an arrival: the old
    subentry would be removed, taking that person's entities, their settings
    and their history with it, and a stranger holding the same credential
    would appear in their place.
    """
    changed = False
    subentries = [
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_USER
    ]
    existing = {
        subentry.data.get(CONF_SLOT, identity(subentry.title)): subentry
        for subentry in subentries
    }

    for name in config.users:
        data = config.subentry_data(name)
        subentry = existing.pop(data.get(CONF_SLOT, identity(name)), None)
        if subentry is None:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=SUBENTRY_TYPE_USER,
                    title=name,
                    unique_id=None,
                ),
            )
            changed = True
        elif dict(subentry.data) != data or subentry.title != name:
            hass.config_entries.async_update_subentry(
                entry, subentry, data=MappingProxyType(data), title=name
            )
            changed = True

    # Whoever is left held a subentry the new configuration does not.
    for subentry in existing.values():
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        changed = True

    # The entry's own half last: it is what the update listener keys on, so
    # letting it land after the subentries means the listener sees them.
    #
    # Options are cleared in the same write. `from_entry` reads the entry side
    # options-first, because the options flow stages its submission there, so a
    # write that left them standing would be invisible -- and, showing no diff,
    # would not wake the listener that clears them either.
    if hass.config_entries.async_update_entry(entry, data=config.to_dict(), options={}):
        changed = True
    return changed
