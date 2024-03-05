"""Exceptions for lock_code_manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from .providers import BaseLock


class LockCodeManagerError(HomeAssistantError):
    """Base class for lock_code_manager exceptions."""


class EntityNotFoundError(LockCodeManagerError):
    """Raise when en entity is not found."""

    def __init__(self, lock: BaseLock, slot_num: int, key: str):
        """Initialize the error."""
        self.lock = lock
        self.key = key
        self.slot_num = slot_num
        super().__init__(f"Entity not found for lock {lock} slot {slot_num} key {key}")


class LockDisconnected(LockCodeManagerError):
    """Raised when lock can't be communicated with."""
