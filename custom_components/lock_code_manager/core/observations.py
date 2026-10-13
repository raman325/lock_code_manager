"""
What one lock last said about each of its addresses, ordered against writes.

The core owns ordering; providers own integrity. A provider reports what the
lock said and when it said it. The log decides what that means against the
writes Lock Code Manager has made since:

- An observation older than the address's last write is superseded: it says
  what the address held before the write, so it neither confirms nor
  contradicts it. A write is "done" when it returns: until then the lock may
  still report the old value, so anything stamped before the return is
  superseded too. A write that raised is measured from its start.
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
        # Supersession cutoff: the last write's return, or its start until it
        # returns (and for good, if it raised).
        self._cutoff: dict[Address, float] = {}
        # Whether a read had completed when the last write started: a
        # reading from before the write arriving late must not change it.
        self._read_by_write: dict[Address, bool] = {}

    def observe(self, address: Address, reading: Reading, at: float) -> bool:
        """
        Record that the lock said ``reading`` about ``address`` at ``at``.

        Returns whether anything changed. A reading older than the last
        informative one arrived late and changes nothing; silence older than
        the last observation of any kind is dropped the same way.
        """
        observation = Observation(reading, at)
        latest = self._latest.get(address)
        changed = False
        if latest is None or at >= latest.at:
            self._latest[address] = observation
            changed = True
        if reading.informative:
            # Ordered only against what the lock said, never against silence:
            # a real reading newer than the last real one counts, however
            # much silence arrived meanwhile.
            informative = self._informative.get(address)
            if informative is None or at >= informative.at:
                self._informative[address] = observation
                changed = True
        return changed

    def mark_write(self, address: Address, at: float) -> None:
        """Record that a write to ``address`` started at ``at``."""
        self._read_by_write[address] = self.ever_read(address)
        self._cutoff[address] = at

    def mark_returned(self, address: Address, at: float) -> None:
        """Record that the last write to ``address`` returned at ``at``."""
        self._cutoff[address] = max(self._cutoff.get(address, at), at)

    def latest(self, address: Address) -> Observation | None:
        """Return the last thing the lock said about ``address``, if anything."""
        return self._latest.get(address)

    def is_superseded(self, address: Address) -> bool:
        """Return whether the last observation predates the last write's end."""
        latest = self._latest.get(address)
        cutoff = self._cutoff.get(address)
        return latest is not None and cutoff is not None and latest.at < cutoff

    def informative_since_write(self, address: Address) -> Observation | None:
        """
        Return the latest informative observation not older than the last write.

        This is what the judge weighs: silence and anything from before the
        last write are left out.
        """
        observation = self._informative.get(address)
        if observation is None:
            return None
        cutoff = self._cutoff.get(address)
        if cutoff is not None and observation.at < cutoff:
            return None
        return observation

    def ever_read(self, address: Address) -> bool:
        """
        Return whether a read of ``address`` has completed, silence included.

        Counted as of the last write's start, plus anything said since: a
        reading from before the write that arrives after it changes nothing.
        """
        latest = self._latest.get(address)
        if latest is None:
            return False
        cutoff = self._cutoff.get(address)
        if cutoff is None:
            return True
        return self._read_by_write.get(address, False) or latest.at >= cutoff

    def forget(self, address: Address) -> None:
        """Drop everything known about ``address``."""
        self._latest.pop(address, None)
        self._informative.pop(address, None)
        self._cutoff.pop(address, None)
        self._read_by_write.pop(address, None)

    def addresses(self) -> frozenset[Address]:
        """Return every address the lock has said something about."""
        return frozenset(self._latest)
