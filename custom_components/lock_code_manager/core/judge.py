"""
One address's verdict, and whether to write it: a pure function.

The judge weighs what is wanted against one ``AddressView`` -- how the last
write relates to it, the latest informative reading since that write, and
whether a read has completed -- and whether the address's breaker has
tripped. It does no I/O and keeps no state.

The rules are the credential-model decisions, in order:

1. Not ours: left alone.
2. A tripped breaker suspends the address until its target changes. A clear
   that would not take may leave a working code on the door, and says so.
3. While a write to the address is out, nothing more is written to it.
4. A reading that disagrees with the target contradicts it at once: out of
   sync, and written again (decision 2). A code disabled on the lock always
   contradicts (decision 5), and so does a code still there after a clear
   (decision 12). If the last write was the target, that write failed: the
   verdict says to charge, and the caller charges the breaker for that
   write (``SlotBreaker.charge(at, write)``), which counts it once.
5. The last write was the target: in sync. On a lock that never says which
   code it holds that is trust, not evidence (decision 1).
6. The lock shows the target: in sync.
7. No read has completed: unknown, and nothing is written.
8. Otherwise out of sync, and written: a failed write retried, or a target
   that changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .address import AddressView, Written
from .refusals import Repair
from .values import (
    Desired,
    Disabled,
    Empty,
    Holds,
    HoldsMasked,
    Observation,
    WantEmpty,
    WantPin,
)


class Status(StrEnum):
    """An address's sync status."""

    # Not Lock Code Manager's to manage.
    NOT_OURS = "not_ours"
    # No read has completed, and no write of the target to trust.
    UNKNOWN = "unknown"
    IN_SYNC = "in_sync"
    OUT_OF_SYNC = "out_of_sync"
    # Left alone until its target changes.
    SUSPENDED = "suspended"


@dataclass(frozen=True, slots=True)
class Verdict:
    """An address's status, what to write next, and what to charge."""

    status: Status
    # What to write now, or None to leave the address alone.
    write: Desired | None = None
    # The last write of the target is contradicted: charge the breaker for it,
    # passing the write, so it is counted once (``SlotBreaker.charge``).
    charge: bool = False
    # The repair a suspension raises.
    repair: Repair | None = None


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
    view: AddressView,
    *,
    tripped: bool,
    release: bool = False,
) -> Verdict:
    """
    Return the verdict on one address.

    ``view`` comes from ``AddressState.view(desired)``. ``release`` marks an
    address no entry manages any more, whose code is being cleared on its way
    out (design-review decision 6).
    """
    if desired is None:
        return Verdict(Status.NOT_OURS)
    if tripped:
        clearing = release or isinstance(desired, WantEmpty)
        repair = Repair.CODE_MAY_REMAIN if clearing else Repair.SLOT_SUSPENDED
        return Verdict(Status.SUSPENDED, repair=repair)
    if view.last_write is Written.IN_FLIGHT:
        return Verdict(Status.OUT_OF_SYNC)
    seen = view.seen
    if seen is not None and contradicts(desired, seen):
        return Verdict(
            Status.OUT_OF_SYNC,
            write=desired,
            charge=view.last_write is Written.TARGET,
        )
    if view.last_write is Written.TARGET:
        return Verdict(Status.IN_SYNC)
    if seen is not None and confirms(desired, seen):
        return Verdict(Status.IN_SYNC)
    if not view.ever_read:
        return Verdict(Status.UNKNOWN)
    return Verdict(Status.OUT_OF_SYNC, write=desired)
