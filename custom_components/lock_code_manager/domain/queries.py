"""Config-only queries across LCM entries (no provider dependency)."""

from __future__ import annotations

from collections.abc import Iterator

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import slugify

from ..const import DOMAIN
from .config import EntryConfig


def get_entry_config(entry: ConfigEntry) -> EntryConfig:
    """
    Return the EntryConfig view of ``entry``.

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


def get_managed_slots(
    hass: HomeAssistant,
    lock_entity_id: str,
    *,
    excluding: ConfigEntry | None = None,
) -> set[int]:
    """
    Return the slot numbers any config entry manages on a lock.

    ``excluding`` leaves one entry out, for a caller deciding what THAT
    entry may use. Its own numbers are not a constraint on itself: the ones
    it keeps are held by tenure, and the ones it is releasing are free. Left
    in, a submission that swaps one user for another would be told its own
    outgoing number was taken, and every edit would push numbers upward.
    """
    return {
        slot_num
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry is not excluding
        and (config := get_entry_config(entry)).has_lock(lock_entity_id)
        for slot_num in config.slot_numbers
    }


def find_entry_for_lock_slot(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int | str
) -> ConfigEntry | None:
    """
    Find the config entry that manages a specific lock + slot combination.

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
