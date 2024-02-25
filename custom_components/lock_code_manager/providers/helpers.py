"""Helpers for lock_code_manager providers."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)


def get_entry_data(config_entry: ConfigEntry, key: str) -> dict[int | str, Any]:
    """Get data from config entry."""
    return config_entry.data.get(key, config_entry.options.get(key, {}))
