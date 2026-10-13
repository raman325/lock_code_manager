"""
One slot on one lock: what is wanted, what was written, what the lock said.

Pure: no Home Assistant, no asyncio; the worker passes the time in. The
rules are the credential-model decisions as the first-principles design
states them (docs: 2026-10-13-core-first-principles.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
import hashlib

# What the lock said about a slot.


class Seen(Enum):
    """What the lock said about a slot, apart from which PIN it holds."""

    EMPTY = "empty"
    # Holds a code it will not show.
    MASKED = "masked"
    # Holds a code that is not in force.
    DISABLED = "disabled"
    # Answered without saying anything about the slot.
    SILENT = "silent"


@dataclass(frozen=True, slots=True)
class Holds:
    """The slot holds this PIN, and the lock said so."""

    pin: str = field(repr=False)

    def __repr__(self) -> str:
        """Keep the PIN out of logs (#917)."""
        return "Holds(****)"


Reading = Seen | Holds

# A wanted PIN, or None for an empty slot.
Want = str | None


class Repair(StrEnum):
    """Why a slot is held, and what the user is told."""

    # The lock refused the PIN; the slot is turned off.
    SLOT_DISABLED = "slot_disabled"
    # The PIN would not go on; nothing is written until the target changes.
    CANT_SET = "cant_set"
    # A clear would not take: the code may still open the door.
    CODE_MAY_REMAIN = "code_may_remain"


class Status(StrEnum):
    """A slot's sync status, as the integration shows it."""

    UNKNOWN = "unknown"
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    SUSPENDED = "suspended"


CLEARED = "cleared"
WINDOW = 300.0
LIMIT = 3
RELEASE_LIMIT = 2


def fingerprint(pin: str, salt: bytes) -> str:
    """Return a salted fingerprint of ``pin``: kept and persisted instead of it."""
    return hashlib.blake2b(pin.encode(), key=salt, digest_size=16).hexdigest()


@dataclass
class Slot:
    """
    One slot on one lock.

    ``wrote`` is what was last written and returned without error: a PIN's
    fingerprint, or ``CLEARED``. ``release`` marks a slot no entry manages any
    more, being cleared on its way out.
    """

    want: Want
    wrote: str | None = None
    seen: Reading | None = None
    failures: list[float] = field(default_factory=list)
    hold: Repair | None = None
    release: bool = False

    def aim(self, want: Want, *, release: bool = False) -> None:
        """Set what is wanted; a change of target clears failures and any hold."""
        if want != self.want or release != self.release:
            self.failures.clear()
            self.hold = None
        self.want = want
        self.release = release

    def agrees(self, salt: bytes) -> bool:
        """Return whether what the lock said agrees with what is wanted."""
        seen = self.seen
        if self.want is None:
            if seen is Seen.EMPTY:
                return True
            return seen is Seen.SILENT and self.wrote == CLEARED
        written = self.wrote == fingerprint(self.want, salt)
        if isinstance(seen, Holds):
            return seen.pin == self.want
        # A lock that will not show the code, or says nothing: trust the write.
        return seen in (Seen.MASKED, Seen.SILENT) and written

    def status(self, salt: bytes) -> Status:
        """Return the slot's sync status."""
        if self.hold is not None:
            return Status.SUSPENDED
        if self.seen is None:
            return Status.UNKNOWN
        return Status.IN_SYNC if self.agrees(salt) else Status.OUT_OF_SYNC

    def observe(self, reading: Reading, salt: bytes, now: float) -> None:
        """
        Take what the lock said about the slot.

        A reading that disagrees with a target Lock Code Manager wrote is one
        failure -- a write the lock did not keep, or a keypad taking the code
        back -- and that write is no longer trusted, so the same contradiction
        read again counts nothing more.
        """
        trusted = self.wrote is not None and self.wrote == self._target(salt)
        self.seen = reading
        if trusted and not self.agrees(salt):
            self.wrote = None
            self.failed(now)

    def wrote_want(self, salt: bytes) -> None:
        """Record that a write of what is wanted returned without error."""
        self.wrote = self._target(salt)

    def _target(self, salt: bytes) -> str:
        """Return what ``wrote`` holds once what is wanted has been written."""
        return CLEARED if self.want is None else fingerprint(self.want, salt)

    def failed(self, now: float) -> None:
        """
        Count a failure: a write raised, or the lock contradicted one.

        ``LIMIT`` failures inside ``WINDOW`` seconds (``RELEASE_LIMIT`` for a
        release clear) hold the slot. Agreement never clears them, so a keypad
        fighting a code back still trips (#776); they age out of the window.
        """
        self.failures = [t for t in self.failures if t > now - WINDOW]
        self.failures.append(now)
        limit = RELEASE_LIMIT if self.release else LIMIT
        if len(self.failures) >= limit:
            self.hold = self._give_up()

    def refused(self, *, value: bool) -> None:
        """Hold the slot at once: the lock refused the value, or the slot."""
        if value and self.want is not None:
            self.hold = Repair.SLOT_DISABLED
        else:
            self.hold = self._give_up()

    def resume(self) -> None:
        """Lift a hold: the user acknowledged its repair."""
        self.failures.clear()
        self.hold = None

    def _give_up(self) -> Repair:
        return Repair.CODE_MAY_REMAIN if self.want is None else Repair.CANT_SET
