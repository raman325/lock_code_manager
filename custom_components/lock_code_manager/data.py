"""Config entry data helpers for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import CONF_SLOTS

_LOGGER = logging.getLogger(__name__)


def get_entry_data(config_entry: ConfigEntry, key: str, default: Any = {}) -> Any:
    """Get data from config entry."""
    return config_entry.data.get(key, config_entry.options.get(key, default))


def get_slot_data(config_entry, slot_num: int) -> dict[str, Any]:
    """Get data for slot."""
    return get_entry_data(config_entry, CONF_SLOTS, {}).get(slot_num, {})
