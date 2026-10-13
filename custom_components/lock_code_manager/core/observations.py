"""
What one lock last said about each of its addresses, ordered against writes.

The core owns ordering; providers own integrity. A provider reports what the
lock said and when it said it. The log decides what that means against the
writes Lock Code Manager has made since:

- An observation older than the address's last write is superseded: it says
  what the address held before the write, so it neither confirms nor
  contradicts it.
- Silence (``Nothing``) never becomes the address's informative observation,
  so it never contradicts a write and never erases a contradiction.
- A fresh observation of the same value is still news: its time moves on
  (#737).
"""

from __future__ import annotations

from .values import Address, Observation, Reading


class ObservationLog:
    """Per-address readings for one lock."""

    def __init__(self) -> None:
        """Start with nothing observed and nothing written."""
        self._latest: dict[Address, Observation] = {}
        self._informative: dict[Address, Observation] = {}
        self._write_started: dict[Address, float] = {}

    def observe(self, address: Address, reading: Reading, at: float) -> bool:
        """
        Record that the lock said ``reading`` about ``address`` at ``at``.

        Returns whether anything changed. An observation older than the one
        already held for the address changes nothing: it arrived late.
        """
        latest = self._latest.get(address)
        if latest is not None and at < latest.at:
            return False
        observation = Observation(reading, at)
        self._latest[address] = observation
        if reading.informative:
            self._informative[address] = observation
        return True

    def mark_write(self, address: Address, at: float) -> None:
        """Record that a write to ``address`` started at ``at``."""
        self._write_started[address] = at

    def latest(self, address: Address) -> Observation | None:
        """Return the last thing the lock said about ``address``, if anything."""
        return self._latest.get(address)

    def is_superseded(self, address: Address) -> bool:
        """Return whether the last observation predates the last write."""
        latest = self._latest.get(address)
        written = self._write_started.get(address)
        return latest is not None and written is not None and latest.at < written

    def informative_since_write(self, address: Address) -> Observation | None:
        """
        Return the latest informative observation not older than the last write.

        This is what the judge weighs: silence and anything from before the
        last write are left out.
        """
        observation = self._informative.get(address)
        if observation is None:
            return None
        written = self._write_started.get(address)
        if written is not None and observation.at < written:
            return None
        return observation

    def ever_read(self, address: Address) -> bool:
        """Return whether the lock has said anything at all about ``address``."""
        return address in self._latest

    def forget(self, address: Address) -> None:
        """Drop everything known about ``address``."""
        self._latest.pop(address, None)
        self._informative.pop(address, None)
        self._write_started.pop(address, None)

    def addresses(self) -> frozenset[Address]:
        """Return every address the lock has said something about."""
        return frozenset(self._latest)
