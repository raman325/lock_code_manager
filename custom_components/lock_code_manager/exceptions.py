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


class ProviderNotImplementedError(LockCodeManagerError, NotImplementedError):
    """Raised when a provider method is not implemented.

    This exception should be raised by BaseLock methods that must be overridden
    by provider subclasses. It combines LockCodeManagerError (so the coordinator
    can catch it uniformly) with NotImplementedError (for standard Python semantics).
    """

    def __init__(self, provider: BaseLock, method_name: str, guidance: str = ""):
        """Initialize the error.

        Args:
            provider: The provider instance (self).
            method_name: Name of the method that needs to be implemented.
            guidance: Optional guidance on how to implement the method.

        """
        message = f"{provider.__class__.__name__} does not implement {method_name}()."
        if guidance:
            message = f"{message} {guidance}"
        super().__init__(message)
