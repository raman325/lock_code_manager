"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .callbacks import EntityCallbackRegistry
from .const import CONF_SLOTS

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
    """Get data from config entry.

    Prefers options over data because during options flow updates, the new
    configuration is in options while data still contains the old configuration.
    """
    return config_entry.options.get(key, config_entry.data.get(key, default))


def get_slot_data(config_entry, slot_num: int) -> dict[str, Any]:
    """Get data for slot."""
    return get_entry_data(config_entry, CONF_SLOTS, {}).get(slot_num, {})
