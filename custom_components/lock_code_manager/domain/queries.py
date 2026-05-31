"""Config-only queries across LCM entries (no provider dependency)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

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


def get_slot_config(config_entry: ConfigEntry, slot_num: int) -> Mapping[str, Any]:
    """Get slot config, raising if not found."""
    config = get_entry_config(config_entry)
    if not config.has_slot(slot_num):
        raise ServiceValidationError(f"Slot {slot_num} not found in config entry")
    return config.slot(slot_num)


def get_loaded_config_entry(hass: HomeAssistant, config_entry_id: str) -> ConfigEntry:
    """Get a loaded config entry by ID, raising if not found or not loaded."""
    config_entry = hass.config_entries.async_get_entry(config_entry_id)
    if not config_entry or config_entry.domain != DOMAIN:
        raise ServiceValidationError(
            f"No lock code manager config entry with ID `{config_entry_id}` found"
        )
    if config_entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"Config entry {config_entry.entry_id} not loaded")
    return config_entry
