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


class CodeRejectedError(LockCodeManagerError):
    """Raised when the lock will not accept a PIN on a slot."""

    def __init__(self, code_slot: int, lock_entity_id: str, reason: str | None = None):
        """Initialize the error."""
        self.code_slot = code_slot
        self.lock_entity_id = lock_entity_id
        reason = (
            reason
            or "the call to the lock succeeded but the lock appears to reject the code"
        )
        super().__init__(
            f"Lock {lock_entity_id}: cannot set code on slot {code_slot} — {reason}"
        )


class DuplicateCodeError(CodeRejectedError):
    """Raised when a PIN duplicates a code in another slot on the lock."""

    def __init__(
        self,
        code_slot: int,
        conflicting_slot: int,
        conflicting_slot_managed: bool,
        lock_entity_id: str,
    ):
        """Initialize the error."""
        self.conflicting_slot = conflicting_slot
        self.conflicting_slot_managed = conflicting_slot_managed
        managed_str = "managed" if conflicting_slot_managed else "unmanaged"
        super().__init__(
            code_slot,
            lock_entity_id,
            f"PIN duplicates {managed_str} slot {conflicting_slot}",
        )


class LockDisconnected(LockCodeManagerError):
    """Raised when lock can't be communicated with."""


class ProviderNotImplementedError(LockCodeManagerError, NotImplementedError):
    """
    Raised when a provider method is not implemented.

    This exception should be raised by BaseLock methods that must be overridden
    by provider subclasses. It combines LockCodeManagerError (so the coordinator
    can catch it uniformly) with NotImplementedError (for standard Python semantics).
    """

    def __init__(self, provider: BaseLock, method_name: str, guidance: str = ""):
        """
        Initialize the error.

        Args:
            provider: The provider instance (self).
            method_name: Name of the method that needs to be implemented.
            guidance: Optional guidance on how to implement the method.

        """
        message = f"{provider.__class__.__name__} does not implement {method_name}()."
        if guidance:
            message = f"{message} {guidance}"
        super().__init__(message)
