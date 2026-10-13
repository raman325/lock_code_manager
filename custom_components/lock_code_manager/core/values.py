"""
The values the core reasons with.

Addresses, what a lock said, what is wanted, and how sure a returned write
is. A PIN never appears in a ``repr``: these values end up in logs, and a PIN in a
log is a PIN on disk (#917).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib

PIN = "pin"


@dataclass(frozen=True, slots=True)
class Address:
    """Where a credential lives on a lock: its kind and its index."""

    kind: str
    index: int

    def __repr__(self) -> str:
        """Return a short form, ``pin:3``."""
        return f"{self.kind}:{self.index}"


def pin_at(index: int) -> Address:
    """Return the PIN address at ``index``."""
    return Address(PIN, index)


# What one exchange with a lock said about one address. "Never read" is not
# a reading: it is the absence of an observation.


class Reading:
    """Base for what a lock said about an address."""

    __slots__ = ()

    @property
    def informative(self) -> bool:
        """Return whether this says anything about what the address holds."""
        return True


@dataclass(frozen=True, slots=True)
class Empty(Reading):
    """The address holds nothing."""


@dataclass(frozen=True, slots=True)
class Holds(Reading):
    """The address holds ``pin``, and the lock said which."""

    pin: str = field(repr=False)

    def __repr__(self) -> str:
        """Return the reading with its PIN masked."""
        return "Holds(****)"


@dataclass(frozen=True, slots=True)
class HoldsMasked(Reading):
    """The address holds a code, and the lock will not say which."""


@dataclass(frozen=True, slots=True)
class Disabled(Reading):
    """The address holds a code that is not in force."""


@dataclass(frozen=True, slots=True)
class Nothing(Reading):
    """The lock answered without saying anything about the address."""

    @property
    def informative(self) -> bool:
        """Return False: silence says nothing."""
        return False


EMPTY = Empty()
HOLDS_MASKED = HoldsMasked()
DISABLED = Disabled()
NOTHING = Nothing()


@dataclass(frozen=True, slots=True)
class Observation:
    """A reading, and when the lock gave it (monotonic seconds)."""

    reading: Reading
    at: float


# What is wanted at an address: the slot's active state, decided by the
# integration from its configuration. The core never reads entities.


class Desired:
    """Base for what is wanted at an address."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class WantPin(Desired):
    """The address should hold ``pin``."""

    pin: str = field(repr=False)

    def __repr__(self) -> str:
        """Return the want with its PIN masked."""
        return "WantPin(****)"


@dataclass(frozen=True, slots=True)
class WantEmpty(Desired):
    """The address should hold nothing."""


WANT_EMPTY = WantEmpty()


class Strength(StrEnum):
    """What a write that returned promises: display and diagnostics only."""

    # The lock reported the change applied.
    APPLIED = "applied"
    # The lock, or its controller, received the write.
    RECEIVED = "received"
    # The write left Home Assistant; nothing came back.
    SENT = "sent"
    # A cloud service accepted the write for the lock.
    SERVICE_ACCEPTED = "service_accepted"


def fingerprint(pin: str, salt: bytes) -> str:
    """Return a salted fingerprint of ``pin``, safe to keep and to persist."""
    return hashlib.blake2b(pin.encode(), key=salt, digest_size=16).hexdigest()
