"""
Everything known about one address on one lock, kept by one owner.

What the lock said and what Lock Code Manager wrote are two halves of one
story, so one object keeps both, and orders them against each other:

- A write is started with ``start_write`` and ends exactly once, through its
  ticket: returned, or failed. Only the newest ticket settles anything. A
  ticket left without an ending -- an exception, a cancellation -- ends as
  failed, so a write can never sit in flight for good.
- Raised means failed, returned means believed (credential-model decision
  3): a returned write records what was written, by salted fingerprint, and
  a failed one records nothing.
- A reading stamped before the current write returned is superseded: until
  then the lock may still report the value the write replaced. A write that
  failed is measured from its start.
- Silence (``Nothing``) never becomes the informative reading, and never
  shuts out a later real one; late arrivals are dropped; a same-value
  reading still moves the time on (#737).
- Whether a read has completed counts silence too, and is measured against
  the current write's start, so a reading from before the write that
  arrives late changes nothing, and it never goes back to False.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from .values import (
    Address,
    Desired,
    Observation,
    Reading,
    Strength,
    WantEmpty,
    WantPin,
    fingerprint,
)


class Written(StrEnum):
    """How the last write to an address relates to what is wanted now."""

    # Nothing written since the address was taken on.
    NOTHING = "nothing"
    # The last write was what is wanted, and it returned.
    TARGET = "target"
    # The last write was something else, and it returned.
    OTHER = "other"
    # The last write has not ended yet.
    IN_FLIGHT = "in_flight"
    # The last write raised.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AddressView:
    """What the judge weighs about one address, relative to what is wanted."""

    last_write: Written
    # The latest informative reading not superseded by the current write.
    seen: Observation | None
    # Whether a read of the address has completed, silence included.
    ever_read: bool


@dataclass(frozen=True, slots=True)
class _Wrote:
    fingerprint: str
    strength: Strength
    at: float


@dataclass(frozen=True, slots=True)
class _Cleared:
    strength: Strength
    at: float


class _Outcome(StrEnum):
    IN_FLIGHT = "in_flight"
    RETURNED = "returned"
    FAILED = "failed"


class WriteTicket:
    """One write to one address: ended once, by its return or its failure."""

    def __init__(self, state: AddressState, target: Desired, started: float) -> None:
        """Start a write of ``target`` at ``started``; see ``AddressState``."""
        self._state = state
        self.target = target
        self.started = started
        self._ended = False

    def returned(self, strength: Strength, at: float) -> bool:
        """End the write as returned at ``at``; return whether it settled the address."""
        self._end()
        return self._state._settle(self, _Outcome.RETURNED, strength, at)

    def failed(self) -> bool:
        """End the write as failed; return whether it settled the address."""
        self._end()
        return self._state._settle(self, _Outcome.FAILED, None, None)

    @property
    def ended(self) -> bool:
        """Return whether the write has ended."""
        return self._ended

    def __enter__(self) -> WriteTicket:
        """Return the ticket, for ``with state.start_write(...) as ticket``."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """End a write left without an ending as failed; never swallow anything."""
        if not self._ended:
            self.failed()

    def _end(self) -> None:
        if self._ended:
            raise RuntimeError("a write ended twice")
        self._ended = True


class AddressState:
    """Readings and writes for one address on one lock."""

    def __init__(self, address: Address, salt: bytes) -> None:
        """Start with nothing read and nothing written; ``salt`` keys fingerprints."""
        self.address = address
        self._salt = salt
        self._latest: Observation | None = None
        self._informative: Observation | None = None
        self._ticket: WriteTicket | None = None
        self._outcome: _Outcome | None = None
        self._belief: _Wrote | _Cleared | None = None
        self._write_started: float | None = None
        self._read_before_write = False
        self._cutoff: float | None = None
        self._charged = False

    def observe(self, reading: Reading, at: float) -> bool:
        """
        Record that the lock said ``reading`` about the address at ``at``.

        Returns whether anything changed. A reading older than the last
        informative one arrived late and changes nothing; silence older than
        the last reading of any kind is dropped the same way.
        """
        observation = Observation(reading, at)
        changed = False
        if self._latest is None or at >= self._latest.at:
            self._latest = observation
            changed = True
        if reading.informative and (
            self._informative is None or at >= self._informative.at
        ):
            self._informative = observation
            changed = True
        return changed

    def start_write(self, target: Desired, at: float) -> WriteTicket:
        """
        Start a write of ``target`` at ``at``, superseding any write still out.

        What was believed before no longer counts: until this write returns
        the view says it is out, and if it fails, that it failed. Only its
        return records a belief again.
        """
        self._read_before_write = self.ever_read
        ticket = WriteTicket(self, target, at)
        self._ticket = ticket
        self._outcome = _Outcome.IN_FLIGHT
        self._write_started = at
        self._cutoff = at
        self._charged = False
        return ticket

    @property
    def ever_read(self) -> bool:
        """Return whether a read of the address has completed, silence included."""
        if self._latest is None:
            return False
        if self._write_started is None:
            return True
        return self._read_before_write or self._latest.at >= self._write_started

    def view(self, desired: Desired) -> AddressView:
        """Return what the judge weighs, relative to ``desired``."""
        return AddressView(self._written(desired), self._seen(), self.ever_read)

    def take_charge(self) -> bool:
        """
        Return True once per returned write: charge the breaker for it.

        For a write the judge found contradicted; asked again for the same
        write, it returns False, however often the address is judged.
        """
        if self._charged or self._outcome is not _Outcome.RETURNED:
            return False
        self._charged = True
        return True

    def _written(self, desired: Desired) -> Written:
        if self._outcome is None:
            return Written.NOTHING
        if self._outcome is _Outcome.IN_FLIGHT:
            return Written.IN_FLIGHT
        if self._outcome is _Outcome.FAILED:
            return Written.FAILED
        belief = self._belief
        if isinstance(desired, WantPin):
            target = isinstance(belief, _Wrote) and belief.fingerprint == fingerprint(
                desired.pin, self._salt
            )
        else:
            target = isinstance(belief, _Cleared)
        return Written.TARGET if target else Written.OTHER

    def _seen(self) -> Observation | None:
        informative = self._informative
        if informative is None:
            return None
        if self._cutoff is not None and informative.at < self._cutoff:
            return None
        return informative

    def _settle(
        self,
        ticket: WriteTicket,
        outcome: _Outcome,
        strength: Strength | None,
        at: float | None,
    ) -> bool:
        """Settle the address from ``ticket``'s ending, if it is the current write."""
        if ticket is not self._ticket:
            return False
        self._outcome = outcome
        if outcome is _Outcome.RETURNED:
            assert strength is not None and at is not None
            target = ticket.target
            if isinstance(target, WantEmpty):
                self._belief = _Cleared(strength, at)
            else:
                assert isinstance(target, WantPin)
                self._belief = _Wrote(fingerprint(target.pin, self._salt), strength, at)
            assert self._cutoff is not None
            self._cutoff = max(self._cutoff, at)
        return True
