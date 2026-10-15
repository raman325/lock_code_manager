"""
Data model types for lock_code_manager.

Canonical home for dataclasses, type aliases, enums, and structured data types
used across the integration.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class SlotCode(StrEnum):
    """
    What the views are told of a slot when the lock gave no PIN for it.

    The websocket payloads and diagnostics carry these labels in place of a
    code. The dashboard cards compare a slot's code against them, and
    ``ts/types.ts`` declares each one as a ``SLOT_CODE_*`` constant.

    EMPTY: the lock says the slot is clear.

    UNREADABLE_CODE: the lock holds a code it will not show (Matter and
    masking locks). Distinct from Home Assistant's STATE_UNKNOWN ("unknown"),
    a sensor whose state is not yet known.

    UNANSWERED: the lock answered without saying anything about the slot. The
    slot may be clear or may hold a code.

    DISABLED: the lock holds a code for the slot that is not in force.

    CHECKING: a managed slot whose last reading was set aside to read it
    again, on a lock that has answered. Nothing is known of it until the read.

    NOT_REPORTED: the lock answered that it cannot tell what the slot holds
    (Zigbee user status Not Supported, #1581).
    """

    EMPTY = "empty"
    UNREADABLE_CODE = "unreadable_code"
    UNANSWERED = "unanswered"
    DISABLED = "disabled_on_lock"
    CHECKING = "checking"
    NOT_REPORTED = "not_reported"


@dataclass(frozen=True, slots=True)
class SlotCredential:
    """
    Credential state for one slot on one lock.

    Three constructors:
    - ``empty()`` -- slot is cleared on the lock
    - ``unreadable()`` -- slot holds a code whose value is write-only
    - ``known(pin)`` -- slot holds a code whose value the provider exposes

    Treat as opaque; consume via accessors not direct field access.
    """

    present: bool
    pin: str | None

    @classmethod
    def empty(cls) -> SlotCredential:
        """Return the shared "slot is cleared" credential."""
        return _EMPTY_CREDENTIAL

    @classmethod
    def unreadable(cls) -> SlotCredential:
        """Return the shared "slot holds a write-only code" credential."""
        return _UNREADABLE_CREDENTIAL

    @classmethod
    def known(cls, pin: str) -> SlotCredential:
        """Return a credential carrying a readable PIN."""
        return cls(present=True, pin=pin)

    @property
    def is_empty(self) -> bool:
        """Return True when the slot holds no code."""
        return not self.present

    @property
    def is_present(self) -> bool:
        """Return True when the slot holds a code."""
        return self.present

    @property
    def is_readable(self) -> bool:
        """Return True when the credential exposes a comparable PIN."""
        return self.present and self.pin is not None

    @property
    def readable_pin(self) -> str | None:
        """Return the PIN when readable, otherwise ``None``."""
        return self.pin if self.is_readable else None

    def matches(self, pin: str) -> bool:
        """Return True when this credential is readable and equals ``pin``."""
        return self.is_readable and self.pin == pin


_EMPTY_CREDENTIAL: Final = SlotCredential(present=False, pin=None)
_UNREADABLE_CREDENTIAL: Final = SlotCredential(present=True, pin=None)
