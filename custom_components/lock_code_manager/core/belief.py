"""
What Lock Code Manager last wrote to each address of one lock.

The protocol is fixed: ``invalidate`` before every write, then ``record_*``
once it returns. A write that raises is never recorded, so it leaves the
address ``Unknown``: raised means failed, returned means believed
(credential-model decision 3). Recording without invalidating first is a bug
and raises.

PINs are kept only as salted fingerprints, so a belief can be persisted, and
logged, without the PIN.

Beliefs are kept for addresses some entry manages, and for an address being
cleared on its way out (a release clear) until that clear is settled: it is
judged like any other write.
"""

from __future__ import annotations

from .values import (
    Address,
    Belief,
    Cleared,
    Strength,
    Unknown,
    WantPin,
    Wrote,
    fingerprint,
)


class BeliefRecord:
    """Per-address beliefs for one lock."""

    def __init__(self, salt: bytes) -> None:
        """Start with no beliefs; ``salt`` keys every fingerprint."""
        self._salt = salt
        self._beliefs: dict[Address, Belief] = {}

    def invalidate(self, address: Address, at: float) -> None:
        """Mark ``address`` as being written from ``at``: no belief until it returns."""
        self._beliefs[address] = Unknown(at)

    def record_pin(
        self, address: Address, pin: str, strength: Strength, at: float
    ) -> bool:
        """
        Record that a write of ``pin`` to ``address`` returned at ``at``.

        Returns False, recording nothing, when the address was forgotten while
        the write was out: it is no longer anyone's.
        """
        if not self._may_record(address):
            return False
        self._beliefs[address] = Wrote(fingerprint(pin, self._salt), strength, at)
        return True

    def record_cleared(self, address: Address, strength: Strength, at: float) -> bool:
        """Record that a clear of ``address`` returned at ``at``; see ``record_pin``."""
        if not self._may_record(address):
            return False
        self._beliefs[address] = Cleared(strength, at)
        return True

    def get(self, address: Address) -> Belief | None:
        """Return the belief about ``address``, or None if nothing was ever written."""
        return self._beliefs.get(address)

    def holds(self, address: Address, want: WantPin) -> bool:
        """Return whether the last write to ``address`` was ``want``'s PIN."""
        belief = self._beliefs.get(address)
        return isinstance(belief, Wrote) and belief.fingerprint == fingerprint(
            want.pin, self._salt
        )

    def forget(self, address: Address) -> None:
        """Drop the belief about ``address``: for permanent events only."""
        self._beliefs.pop(address, None)

    def _may_record(self, address: Address) -> bool:
        """
        Return whether a returning write may be recorded.

        Only over the ``Unknown`` its start left. Nothing there means the
        address was forgotten meanwhile; a settled belief there means the
        write was never started, which is a bug.
        """
        belief = self._beliefs.get(address)
        if belief is None:
            return False
        if not isinstance(belief, Unknown):
            raise ValueError(f"{address!r}: recorded a write that was never started")
        return True
