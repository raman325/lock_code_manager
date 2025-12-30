"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .const import CONF_SLOTS

if TYPE_CHECKING:
    from .coordinator import LockUsercodeUpdateCoordinator
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


@dataclass
class LockCodeManagerConfigEntryData:
    """Runtime data for a Lock Code Manager config entry."""

    locks: dict[str, BaseLock] = field(default_factory=dict)
    coordinators: dict[str, LockUsercodeUpdateCoordinator] = field(default_factory=dict)
    setup_tasks: dict[str | Platform, asyncio.Task[Any]] = field(default_factory=dict)


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryData]


def get_entry_data(config_entry: ConfigEntry, key: str, default: Any = {}) -> Any:
    """Get data from config entry."""
    return config_entry.data.get(key, config_entry.options.get(key, default))


def get_slot_data(config_entry, slot_num: int) -> dict[str, Any]:
    """Get data for slot."""
    return get_entry_data(config_entry, CONF_SLOTS, {}).get(slot_num, {})
