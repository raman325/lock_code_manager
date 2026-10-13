"""
What a failed write does to its address: one table (design-review decision 1).

Failures are classified by what would change the outcome, not by where they
came from. A refused value is not going to land however often it is sent; a
refused clear may leave a code working on the door; a transient failure may
not repeat; an unreachable lock is the lock's problem, not the address's; and
Lock Code Manager's own queue running out is nobody's word about anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorKind(StrEnum):
    """Why a write did not succeed, as the provider reports it."""

    # The lock will not take this value (a duplicate, a PIN it rejects).
    VALUE_REFUSED = "value_refused"
    # The lock will not let this slot be written.
    SLOT_REFUSED = "slot_refused"
    # The lock answered with a failure that may not repeat.
    TRANSIENT = "transient"
    # The lock could not be reached, or did not answer in time.
    UNREACHABLE = "unreachable"
    # The lock already holds what was asked (clearing an empty slot).
    ALREADY_SO = "already_so"
    # Lock Code Manager's own queue for the turn ran out; nothing was sent.
    TURN_TIMEOUT = "turn_timeout"


class Outcome(StrEnum):
    """What happens to the address."""

    DISABLE = "disable"
    SUSPEND = "suspend"
    SLOT_STRIKE = "slot_strike"
    LOCK_STRIKE = "lock_strike"
    SUCCESS = "success"
    NOTHING = "nothing"


class Repair(StrEnum):
    """The repair a refusal raises."""

    # The code was refused; the slot is turned off until the user changes it.
    SLOT_DISABLED = "slot_disabled"
    # The slot was refused; nothing is written until its target changes.
    SLOT_SUSPENDED = "slot_suspended"
    # A clear was refused: the code may still work on the lock.
    CODE_MAY_REMAIN = "code_may_remain"


@dataclass(frozen=True, slots=True)
class Response:
    """What a failed write does, and the repair it raises, if any."""

    outcome: Outcome
    repair: Repair | None = None


def respond(kind: ErrorKind, *, clearing: bool) -> Response:
    """Return what a write that failed as ``kind`` does to its address."""
    if kind is ErrorKind.TURN_TIMEOUT:
        return Response(Outcome.NOTHING)
    if kind is ErrorKind.UNREACHABLE:
        return Response(Outcome.LOCK_STRIKE)
    if kind is ErrorKind.TRANSIENT:
        return Response(Outcome.SLOT_STRIKE)
    if kind is ErrorKind.ALREADY_SO:
        return Response(Outcome.SUCCESS)
    if clearing:
        # A refused clear never disables: disabling asks for a clear, the
        # same clear the lock just refused, and loops (#1633).
        return Response(Outcome.SUSPEND, Repair.CODE_MAY_REMAIN)
    if kind is ErrorKind.VALUE_REFUSED:
        return Response(Outcome.DISABLE, Repair.SLOT_DISABLED)
    return Response(Outcome.SUSPEND, Repair.SLOT_SUSPENDED)
