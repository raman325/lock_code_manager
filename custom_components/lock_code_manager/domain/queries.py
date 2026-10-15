"""
Queries across Lock Code Manager entries (no provider dependency).

"Which entry manages this lock or slot" has more than one honest answer,
because callers need different sets of entries counted. Each set has one
named question here, and each caller asks the one it means.

Every entry listing the lock, in any load state, disabled and ignored
included -- a number held by an entry that is switched off is still that
entry's:

- ``entries_configuring``: the entries themselves, each with the configuration
  it answers with. The slot entities a lock's view shows, and the config
  flow's check for slots already configured.
- ``slots_configured_on``: the slot numbers their users hold. Read scope,
  operation budgets, confirmation scope, the websocket's ``managed`` flag,
  the unmanaged-code sweep, the numbers allocation treats as claimed by
  other entries, and the Akuvox and Schlage tag passes -- which write: they
  assign each untagged user on the device to one of these numbers that no
  tagged user holds.
- ``configured_owner``: the entry holding one slot, first match wins.
  Duplicate-code attribution, and crediting a credential use to an entry.

Enabled entries in a load state that keeps their slots, the only ones whose
slots are wanted on the lock:

- ``syncing_owner``: the entry holding one slot. The credential Lock Code
  Manager wants there (``desired_credential``).

Entries that are not ignored, in any load state, disabled ones included:

- ``another_entry_configures``: whether one other than the one asking lists
  the lock. Keeping per-lock repair issues, the provider's stored state and
  what the core remembers when a lock leaves an entry.
- ``every_configured_lock``: every lock one lists. What the core remembers
  is kept for these alone.

Who holds a live lock instance is a different question, about runtime
objects rather than configuration:

- ``entries_holding``: every entry holding one lock instance, in any state.
  Release (``_lock_instance_held_by_another_entry`` in ``__init__``), and
  whether the lock's coordinator honours "Disable polling". Acquisition asks
  a narrower question (``_find_shared_lock_instance`` in ``__init__``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import slugify

from ..const import CONF_SLOT, DOMAIN, SUBENTRY_TYPE_USER
from .config import EntryConfig

if TYPE_CHECKING:
    from ..providers import BaseLock

# The states in which an entry's ``runtime_data`` is the one its last setup
# completed with. Home Assistant only starts unloading a loaded entry.
_CACHED_CONFIG_IS_CURRENT_STATES = frozenset(
    {ConfigEntryState.LOADED, ConfigEntryState.UNLOAD_IN_PROGRESS}
)


def get_entry_config(entry: ConfigEntry) -> EntryConfig:
    """
    Return the EntryConfig view of ``entry``.

    A loaded entry answers from ``entry.runtime_data.config``, the view its
    update listener last applied. Any other entry is read from its stored
    data, because Home Assistant keeps ``runtime_data`` on an entry whose
    setup failed after assigning it: that cache is the failed run's, and
    predates anything saved since. A setup in progress is read from storage
    too. Until it assigns its own ``runtime_data`` the one there may be a
    failed run's, and once it has, that cache was built from the same data.
    """
    cached = getattr(getattr(entry, "runtime_data", None), "config", None)
    if entry.state in _CACHED_CONFIG_IS_CURRENT_STATES and isinstance(
        cached, EntryConfig
    ):
        return cached
    return EntryConfig.from_entry(entry)


def subentry_id_for_slot(entry: ConfigEntry, slot_num: int) -> str | None:
    """
    Return the id of the subentry holding the user at this slot.

    What binds a slot's entities and its device to the user they belong to,
    so Home Assistant renders them under that user on the entry's page rather
    than in one undifferentiated list.

    ``None`` when nobody holds the number, which is not an error: a lock read
    can name a slot the configuration does not.
    """
    return next(
        (
            subentry.subentry_id
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_USER
            and subentry.data.get(CONF_SLOT) == slot_num
        ),
        None,
    )


# Load states in which an enabled entry keeps its slots: loaded, or on its way
# there. A reload passes through UNLOAD_IN_PROGRESS and NOT_LOADED without
# anybody releasing the slots, and an entry syncs them before its setup
# finishes. An entry whose setup, migration or unload failed syncs nothing
# until the user acts, so its slots are not wanted on the lock.
_KEEPS_ITS_SLOTS_STATES = frozenset(
    {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
    }
)


def _every_entry(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every entry, in any load state, disabled and ignored ones included."""
    return hass.config_entries.async_entries(DOMAIN)


def _enabled_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every entry that is neither disabled nor ignored, in any load state."""
    return hass.config_entries.async_entries(
        DOMAIN, include_disabled=False, include_ignore=False
    )


def _listing_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every entry that is not ignored, in any load state, disabled ones included."""
    return hass.config_entries.async_entries(DOMAIN, include_ignore=False)


def _syncing_entries(hass: HomeAssistant) -> Iterator[ConfigEntry]:
    """Every enabled entry in a load state that keeps its slots."""
    return (
        entry
        for entry in _enabled_entries(hass)
        if entry.state in _KEEPS_ITS_SLOTS_STATES
    )


def _configuring(
    entries: Iterable[ConfigEntry], lock_entity_id: str
) -> Iterator[tuple[ConfigEntry, EntryConfig]]:
    """Yield each of ``entries`` that lists the lock, with the view it answers with."""
    return (
        (entry, config)
        for entry in entries
        if (config := get_entry_config(entry)).has_lock(lock_entity_id)
    )


def _owner(
    entries: Iterable[ConfigEntry], lock_entity_id: str, code_slot: int | str
) -> ConfigEntry | None:
    """Return the first of ``entries`` with a user at the slot on the lock."""
    return next(
        (
            entry
            for entry, config in _configuring(entries, lock_entity_id)
            if config.has_slot(code_slot)
        ),
        None,
    )


def entries_configuring(
    hass: HomeAssistant, lock_entity_id: str
) -> Iterator[tuple[ConfigEntry, EntryConfig]]:
    """
    Yield every entry listing the lock, in any state, disabled included.

    Each comes with the configuration it answers with, so a caller reading
    its slots does not build that view a second time.
    """
    return _configuring(_every_entry(hass), lock_entity_id)


def slots_configured_on(
    hass: HomeAssistant,
    lock_entity_id: str,
    *,
    excluding: ConfigEntry | None = None,
) -> set[int]:
    """
    Return the slot numbers any entry's users hold on a lock.

    Every entry counts, in any state, disabled and ignored ones included:
    a number held by an entry that is switched off is still that entry's,
    and treating it as free would hand it to somebody else.

    ``excluding`` leaves one entry out, for a caller deciding what THAT
    entry may use. Its own numbers are not a constraint on itself: the ones
    it keeps are held by tenure, and the ones it is releasing are free. Left
    in, a submission that swaps one user for another would be told its own
    outgoing number was taken, and every edit would push numbers upward.
    """
    return {
        slot_num
        for _entry, config in _configuring(
            (entry for entry in _every_entry(hass) if entry is not excluding),
            lock_entity_id,
        )
        for slot_num in config.slot_numbers
    }


def configured_owner(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int | str
) -> ConfigEntry | None:
    """
    Return the entry whose user holds the slot on the lock, in any state.

    Disabled and ignored entries count. There should be at most one, by the
    config flow's uniqueness check; if storage holds more, the first wins.
    """
    return _owner(_every_entry(hass), lock_entity_id, code_slot)


def syncing_owner(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int | str
) -> ConfigEntry | None:
    """
    Return the entry whose user holds the slot on the lock, if it syncs it.

    Only an enabled entry in a state that keeps its slots
    (``_KEEPS_ITS_SLOTS_STATES``) counts: it is the only kind whose slots are
    wanted on the lock.
    """
    return _owner(_syncing_entries(hass), lock_entity_id, code_slot)


def another_entry_configures(
    hass: HomeAssistant, config_entry: ConfigEntry, lock_entity_id: str
) -> bool:
    """
    Return whether an entry other than ``config_entry`` lists the lock.

    Counts every entry that is not ignored, in any load state, disabled ones
    included: a lock a disabled entry lists has not left Lock Code Manager,
    and enabling the entry again finds what was kept for it -- the per-lock
    repairs, the provider's stored state, what was last written. Anything
    that tears down live objects asks ``_lock_instance_held_by_another_entry``
    in ``__init__`` instead.
    """
    return any(
        entry.entry_id != config_entry.entry_id
        for entry, _config in _configuring(_listing_entries(hass), lock_entity_id)
    )


def every_configured_lock(hass: HomeAssistant) -> set[str]:
    """Return every lock an entry that is not ignored lists, disabled ones included."""
    return {
        lock
        for entry in _listing_entries(hass)
        for lock in get_entry_config(entry).locks
    }


def entries_holding(
    hass: HomeAssistant, lock_entity_id: str, lock: BaseLock
) -> Iterator[ConfigEntry]:
    """
    Yield every entry holding this very lock instance, in any state.

    An entry holds an instance while it sits in the entry's
    ``runtime_data.locks``: put there when the entry builds or shares it,
    taken out when the entry lets go. Asked by identity, not configuration:
    an entry can list a lock it does not hold, and an entry setting up while
    another unloads can hold a second instance for the same lock.
    """
    return (
        entry
        for entry in _every_entry(hass)
        if (runtime_data := getattr(entry, "runtime_data", None)) is not None
        and runtime_data.locks.get(lock_entity_id) is lock
    )


def iter_loaded_lcm_entries(hass: HomeAssistant) -> Iterator[ConfigEntry]:
    """
    Yield loaded Lock Code Manager config entries.

    A lock may be shared by multiple LCM entries (same physical lock
    managed from multiple Lock Code Manager configurations); callers
    should treat the iteration as authoritative for "which locks does
    Lock Code Manager manage right now".
    """
    return (
        entry
        for entry in hass.config_entries.async_entries(
            DOMAIN, include_disabled=False, include_ignore=False
        )
        if entry.state is ConfigEntryState.LOADED
    )


def find_config_entry_by_title(hass: HomeAssistant, title: str) -> ConfigEntry | None:
    """
    Find an LCM config entry by title, comparing slugified.

    Normally exactly one entry can match: setup reserves ``slugify(title)``
    as the entry's unique ID and aborts on a second one. Renaming an entry
    afterwards does not update its unique ID, so that is the one way two
    entries can come to share a slugified title -- whereupon this returns
    the first, as it always has.
    """
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if slugify(entry.title) == slugify(title)
        ),
        None,
    )


def get_loaded_config_entry(
    hass: HomeAssistant,
    config_entry_id: str | None = None,
    config_entry_title: str | None = None,
) -> ConfigEntry:
    """
    Get a loaded LCM config entry by ID or by title.

    Either identifier resolves the same entry, so an action and a card can
    be pointed at one the same way. Title wins when both arrive, matching
    the websocket API the cards call.
    """
    if config_entry_title:
        config_entry = find_config_entry_by_title(hass, config_entry_title)
        if not config_entry:
            raise ServiceValidationError(
                f"No lock code manager config entry with title "
                f"`{config_entry_title}` found"
            )
    elif config_entry_id:
        config_entry = hass.config_entries.async_get_entry(config_entry_id)
        if not config_entry or config_entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"No lock code manager config entry with ID `{config_entry_id}` found"
            )
    else:
        raise ServiceValidationError(
            "Neither config_entry_title nor config_entry_id provided"
        )
    if config_entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"Config entry {config_entry.entry_id} not loaded")
    return config_entry
