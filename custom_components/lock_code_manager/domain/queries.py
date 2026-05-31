"""Query helpers that read across LCM config entries."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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
