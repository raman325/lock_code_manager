"""
One address's verdict, and whether to write it: a pure function.

The judge weighs what is wanted against what Lock Code Manager last wrote and
what the lock has said since. It does no I/O and keeps no state; the caller
supplies everything, including whether the address's breaker has tripped.

The rules are the credential-model decisions:

- Nothing is set or cleared before a read of the lock has completed: until
  then the verdict is unknown, unless the last write was already the target.
  A read that completed counts even if the lock said nothing informative
  (spec section 1): a lock that never says what it holds must still be
  written.
- In sync means the target was written and nothing the lock said since
  contradicts it. On a lock that never says which code it holds, that is
  trust, not evidence (decision 1).
- A reading that disagrees with the target contradicts it at once: out of
  sync, and written again (decision 2). If the target was what Lock Code
  Manager last wrote, that write failed: the verdict names it
  (``charge_for``, the time it returned), so the caller charges the breaker
  once for it however often the address is judged.
- A code that is on the lock but disabled always contradicts (decision 5).
- A code still there after a clear contradicts the clear: the lock is
  believed, and the clear sent again (decision 12).
- Silence never contradicts and never confirms: the caller passes only the
  latest informative observation since the last write.
- A tripped breaker suspends the address until its target changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .belief import BeliefRecord
from .refusals import Repair
from .values import (
    Address,
    Cleared,
    Desired,
    Disabled,
    Empty,
    Holds,
    HoldsMasked,
    Observation,
    Unknown,
    WantEmpty,
    WantPin,
    Wrote,
)


class Status(StrEnum):
    """An address's sync status."""

    # Not Lock Code Manager's to manage.
    NOT_OURS = "not_ours"
    # Not read yet, and no write of the target to trust.
    UNKNOWN = "unknown"
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    # Left alone until its target changes.
    SUSPENDED = "suspended"


class Written(StrEnum):
    """How the last write to an address relates to its target (``LastWrite.kind``)."""

    # Nothing written since startup (or since the belief was forgotten).
    NOTHING = "nothing"
    # The last write was the target, and it returned.
    TARGET = "target"
    # The last write was something else, and it returned.
    OTHER = "other"
    # The last write raised, or has not returned.
    UNSETTLED = "unsettled"


@dataclass(frozen=True, slots=True)
class LastWrite:
    """How the last write to an address relates to its target, and when."""

    kind: Written
    # When the write returned (or, unsettled, started); None if nothing written.
    at: float | None = None


NOTHING_WRITTEN = LastWrite(Written.NOTHING)


@dataclass(frozen=True, slots=True)
class Verdict:
    """An address's status, what to write next, and what to charge."""

    status: Status
    # What to write now, or None to leave the address alone.
    write: Desired | None = None
    # A write of the target that has been contradicted, by when it returned:
    # charge the breaker for it, once.
    charge_for: float | None = None
    # The repair a suspension raises.
    repair: Repair | None = None


def written(beliefs: BeliefRecord, address: Address, desired: Desired) -> LastWrite:
    """Return how the last write to ``address`` relates to ``desired``."""
    belief = beliefs.get(address)
    if belief is None:
        return NOTHING_WRITTEN
    if isinstance(belief, Unknown):
        return LastWrite(Written.UNSETTLED, belief.at)
    assert isinstance(belief, Wrote | Cleared)
    if isinstance(desired, WantPin):
        target = beliefs.holds(address, desired)
    else:
        target = isinstance(belief, Cleared)
    return LastWrite(Written.TARGET if target else Written.OTHER, belief.at)


def contradicts(desired: Desired, observation: Observation) -> bool:
    """Return whether what the lock said rules out the address holding ``desired``."""
    reading = observation.reading
    if isinstance(reading, Disabled):
        return True
    if isinstance(desired, WantEmpty):
        return isinstance(reading, Holds | HoldsMasked)
    assert isinstance(desired, WantPin)
    if isinstance(reading, Empty):
        return True
    return isinstance(reading, Holds) and reading.pin != desired.pin


def confirms(desired: Desired, observation: Observation) -> bool:
    """Return whether what the lock said shows the address holding ``desired``."""
    reading = observation.reading
    if isinstance(desired, WantEmpty):
        return isinstance(reading, Empty)
    assert isinstance(desired, WantPin)
    return isinstance(reading, Holds) and reading.pin == desired.pin


def judge(
    desired: Desired | None,
    last_write: LastWrite,
    seen: Observation | None,
    *,
    ever_read: bool,
    tripped: bool,
    release: bool = False,
) -> Verdict:
    """
    Return the verdict on one address.

    ``seen`` is the latest informative observation since the last write
    (``ObservationLog.informative_since_write``). ``ever_read`` is whether a
    read of the address has completed, silence included
    (``ObservationLog.ever_read``). ``release`` marks an
    address no entry manages any more, whose code is being cleared on the way
    out (design-review decision 6): its suspension asks the user to check the
    code is gone.
    """
    if desired is None:
        return Verdict(Status.NOT_OURS)
    if tripped:
        repair = Repair.CODE_MAY_REMAIN if release else Repair.SLOT_SUSPENDED
        return Verdict(Status.SUSPENDED, repair=repair)
    if seen is not None and contradicts(desired, seen):
        return Verdict(
            Status.OUT_OF_SYNC,
            write=desired,
            charge_for=last_write.at if last_write.kind is Written.TARGET else None,
        )
    if last_write.kind is Written.TARGET:
        return Verdict(Status.IN_SYNC)
    if seen is not None and confirms(desired, seen):
        return Verdict(Status.IN_SYNC)
    if not ever_read:
        return Verdict(Status.UNKNOWN)
    return Verdict(Status.OUT_OF_SYNC, write=desired)
