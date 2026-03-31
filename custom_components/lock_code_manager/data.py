"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .callbacks import EntityCallbackRegistry
from .const import CONF_LOCKS, CONF_SLOTS, DOMAIN

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


@dataclass
class LockCodeManagerConfigEntryData:
    """Runtime data for a Lock Code Manager config entry."""

    locks: dict[str, BaseLock] = field(default_factory=dict)
    setup_tasks: dict[str | Platform, asyncio.Task[Any]] = field(default_factory=dict)
    callbacks: EntityCallbackRegistry = field(default_factory=EntityCallbackRegistry)


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryData]


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
