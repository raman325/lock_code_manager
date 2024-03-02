"""Exceptions for lock_code_manager."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class LockCodeManagerError(HomeAssistantError):
    """Base class for lock_code_manager exceptions."""


class EntityNotFoundError(LockCodeManagerError):
    """Raise when en entity is not found."""

    def __init__(self, entity_id: str):
        """Initialize the error."""
        self.entity_id = entity_id
        super().__init__(f"Entity not found: {entity_id}")


class LockDisconnected(LockCodeManagerError):
    """Raised when lock can't be communicated with."""
