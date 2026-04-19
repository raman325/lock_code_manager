"""Exceptions for lock_code_manager."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from .providers import BaseLock


class LockCodeManagerError(HomeAssistantError):
    """Base class for lock_code_manager exceptions."""


class LockCodeManagerProviderError(LockCodeManagerError):
    """Base class for exceptions raised by lock providers.

    Subclasses cover real provider-side failures: communication problems
    (``LockDisconnected``), the lock rejecting a code (``CodeRejectedError``,
    ``DuplicateCodeError``), or the provider declining to implement an
    operation (``ProviderNotImplementedError``).

    Catching this class lets callers ask "did this error come from the
    lock provider?" without enumerating every provider error type.
    """


class EntityNotFoundError(LockCodeManagerError):
    """Raise when en entity is not found."""

    def __init__(self, lock: BaseLock, slot_num: int, key: str):
        """Initialize the error."""
        self.lock = lock
        self.key = key
        self.slot_num = slot_num
        super().__init__(f"Entity not found for lock {lock} slot {slot_num} key {key}")


class CodeRejectedError(LockCodeManagerProviderError):
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
        lock_entity_id: str,
        conflicting_slot: int | None = None,
        conflicting_slot_managed: bool = False,
    ):
        """Initialize the error."""
        self.conflicting_slot = conflicting_slot
        self.conflicting_slot_managed = conflicting_slot_managed
        if conflicting_slot is not None:
            managed_str = "managed" if conflicting_slot_managed else "unmanaged"
            reason = f"PIN duplicates {managed_str} slot {conflicting_slot}"
        else:
            reason = "duplicate detected by lock firmware"
        super().__init__(
            code_slot,
            lock_entity_id,
            reason,
        )


class LockDisconnected(LockCodeManagerProviderError):
    """Raised when lock can't be communicated with."""


class LockOperationFailed(LockCodeManagerProviderError):
    """Raised when the lock is reachable but the operation failed.

    This covers cases like the lock not supporting a requested operation
    or the provider rejecting the command for a lock-side reason. Unlike
    ``LockDisconnected``, the lock is online — the specific operation
    just could not be completed.
    """


class ProviderNotImplementedError(LockCodeManagerProviderError, NotImplementedError):
    """Raised when a provider method that subclasses must override is called."""

    def __init__(self, provider: BaseLock, method_name: str, guidance: str = ""):
        """Initialize the error."""
        message = f"{provider.__class__.__name__} does not implement {method_name}()."
        if guidance:
            message = f"{message} {guidance}"
        super().__init__(message)
