"""
Everything known about one address on one lock, kept by one owner.

What the lock said and what Lock Code Manager wrote are two halves of one
story, so one object keeps both, and orders them against each other:

- A write exists only inside ``with state.writing(target, at) as write:``,
  and ends exactly once: returned, failed, or abandoned (never sent). Only
  the newest write settles anything. Leaving the block without an ending --
  an exception, a cancellation -- ends it as failed, so a write can never
  sit in flight for good.
- Raised means failed, returned means believed (credential-model decision
  3): a returned write records what was written, by salted fingerprint, and
  a failed one records nothing. An abandoned write never reached the lock,
  so the address is left exactly as it was before it.
- A reading stamped before the current write returned is superseded: until
  then the lock may still report the value the write replaced. A write that
  failed is measured from its start.
- Silence (``Nothing``) never becomes the informative reading, and never
  shuts out a later real one; late arrivals are dropped; a same-value
  reading still moves the time on (#737).
- Whether a read has completed counts silence too, and is measured against
  the current write's start, so a reading from before the write that
  arrives late changes nothing, and it never goes back to False.

Each fact is kept once: whether a write is out, when it started and how it
ended live on the write itself, and the state only says which write is
current.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

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
class Belief:
    """What a returned write put on the lock: a fingerprint, or nothing."""

    # The salted fingerprint of the PIN written, or None for a clear.
    fingerprint: str | None
    strength: Strength
    at: float


class _Ending(StrEnum):
    RETURNED = "returned"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(slots=True)
class _Before:
    """The address as it was when a write started, for an abandoned write."""

    write: Write | None
    read_before_write: bool


class Write:
    """
    One write to one address: ended once, by its return, failure or abandonment.

    Obtained only from ``AddressState.writing``.
    """

    def __init__(
        self, state: AddressState, target: Desired, started: float, before: _Before
    ) -> None:
        """Start a write of ``target`` at ``started``."""
        self._state = state
        self.target = target
        self.started = started
        self._before = before
        self._ending: _Ending | None = None
        self._belief: Belief | None = None
        self._returned_at: float | None = None

    @property
    def out(self) -> bool:
        """Return whether the write has not ended yet."""
        return self._ending is None

    def returned(self, strength: Strength, at: float) -> bool:
        """
        End the write as returned at ``at``; return whether it settled the address.

        The belief is built before anything changes, so a failure building
        it leaves the write out, to end as failed when its block exits.
        """
        belief = self._state._belief_for(self.target, strength, at)
        self._end(_Ending.RETURNED)
        self._belief = belief
        self._returned_at = at
        return self._state._is_current(self)

    def failed(self) -> bool:
        """End the write as having raised; return whether it was current."""
        self._end(_Ending.FAILED)
        return self._state._is_current(self)

    def abandoned(self) -> bool:
        """
        End a write that never reached the lock; return whether it was current.

        Nothing was sent, so the address goes back to how it was before.
        """
        self._end(_Ending.ABANDONED)
        return self._state._abandon(self)

    def _end(self, ending: _Ending) -> None:
        if self._ending is not None:
            raise RuntimeError("a write ended twice")
        self._ending = ending


class AddressState:
    """Readings and writes for one address on one lock."""

    def __init__(self, address: Address, salt: bytes) -> None:
        """Start with nothing read and nothing written; ``salt`` keys fingerprints."""
        self.address = address
        self._salt = salt
        self._latest: Observation | None = None
        self._informative: Observation | None = None
        self._write: Write | None = None
        self._read_before_write = False

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

    @contextmanager
    def writing(self, target: Desired, at: float) -> Generator[Write]:
        """
        Start a write of ``target`` at ``at``, superseding any write still out.

        The write is ended by the caller inside the block; if it is not, it
        ends as failed when the block exits, however it exits.
        """
        before = _Before(self._write, self._read_before_write)
        self._read_before_write = self.ever_read
        write = Write(self, target, at, before)
        self._write = write
        try:
            yield write
        finally:
            if write.out:
                write.failed()

    @property
    def last_write(self) -> Write | None:
        """Return the current write: the newest one started and not abandoned."""
        return self._write

    @property
    def latest(self) -> Observation | None:
        """Return the last thing the lock said about the address, if anything."""
        return self._latest

    @property
    def ever_read(self) -> bool:
        """Return whether a read of the address has completed, silence included."""
        if self._latest is None:
            return False
        if self._write is None:
            return True
        return self._read_before_write or self._latest.at >= self._write.started

    def view(self, desired: Desired) -> AddressView:
        """Return what the judge weighs, relative to ``desired``."""
        return AddressView(self._written(desired), self._seen(), self.ever_read)

    def _written(self, desired: Desired) -> Written:
        write = self._write
        if write is None:
            return Written.NOTHING
        if write.out:
            return Written.IN_FLIGHT
        if write._ending is _Ending.FAILED:
            return Written.FAILED
        belief = write._belief
        assert belief is not None
        if isinstance(desired, WantPin):
            target = belief.fingerprint == fingerprint(desired.pin, self._salt)
        else:
            target = belief.fingerprint is None
        return Written.TARGET if target else Written.OTHER

    def _seen(self) -> Observation | None:
        informative = self._informative
        if informative is None:
            return None
        write = self._write
        if write is not None:
            cutoff = write.started
            if write._returned_at is not None:
                cutoff = max(cutoff, write._returned_at)
            if informative.at < cutoff:
                return None
        return informative

    def _belief_for(self, target: Desired, strength: Strength, at: float) -> Belief:
        if isinstance(target, WantEmpty):
            return Belief(None, strength, at)
        assert isinstance(target, WantPin)
        return Belief(fingerprint(target.pin, self._salt), strength, at)

    def _is_current(self, write: Write) -> bool:
        return write is self._write

    def _abandon(self, write: Write) -> bool:
        """Put the address back as it was before ``write``, if it is current."""
        if write is not self._write:
            return False
        self._write = write._before.write
        self._read_before_write = write._before.read_before_write
        return True
