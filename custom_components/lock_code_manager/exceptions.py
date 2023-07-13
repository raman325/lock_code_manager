"""Exceptions for keymaster."""
from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class ConfigEntryNotFoundError(HomeAssistantError):
    """Raise when a config entry for a given entity ID is not found."""


class LockDisconnected(HomeAssistantError):
    """Raised when lock can't be communicated with."""
