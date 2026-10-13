"""
What Lock Code Manager last wrote to each address of one lock.

The protocol is fixed. ``invalidate`` starts a write and returns its id; the
write then ends exactly once, by ``record_pin`` or ``record_cleared`` when it
returns, or by ``record_failed`` when it raises. Only the write that started
last can settle the address: a write that ends after a newer one started, or
after its address was forgotten, records nothing. Raised means failed,
returned means believed (credential-model decision 3).

PINs are kept only as salted fingerprints, so a belief can be persisted, and
logged, without the PIN.

Beliefs are kept for addresses some entry manages, and for an address being
cleared on its way out (a release clear) until that clear is settled: it is
judged like any other write.
"""

from __future__ import annotations

from dataclasses import replace

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
        self._next_id = 1

    def invalidate(self, address: Address, at: float) -> int:
        """Start a write to ``address`` at ``at``; return its id."""
        write_id = self._next_id
        self._next_id += 1
        self._beliefs[address] = Unknown(write_id, at)
        return write_id

    def record_pin(
        self,
        address: Address,
        write_id: int,
        pin: str,
        strength: Strength,
        at: float,
    ) -> bool:
        """Record that write ``write_id`` of ``pin`` returned at ``at``."""
        if not self._is_out(address, write_id):
            return False
        self._beliefs[address] = Wrote(
            write_id, fingerprint(pin, self._salt), strength, at
        )
        return True

    def record_cleared(
        self, address: Address, write_id: int, strength: Strength, at: float
    ) -> bool:
        """Record that write ``write_id``, a clear, returned at ``at``."""
        if not self._is_out(address, write_id):
            return False
        self._beliefs[address] = Cleared(write_id, strength, at)
        return True

    def record_failed(self, address: Address, write_id: int) -> bool:
        """Record that write ``write_id`` raised: no belief, and nothing out."""
        if not self._is_out(address, write_id):
            return False
        belief = self._beliefs[address]
        assert isinstance(belief, Unknown)
        self._beliefs[address] = replace(belief, failed=True)
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

    def _is_out(self, address: Address, write_id: int) -> bool:
        """
        Return whether write ``write_id`` is the one ``address`` is waiting on.

        A newer write started since, or the address forgotten, means this one
        ending changes nothing. Ending a write twice is a bug.
        """
        belief = self._beliefs.get(address)
        if belief is None or belief_write_id(belief) != write_id:
            return False
        if not isinstance(belief, Unknown) or belief.failed:
            raise ValueError(f"{address!r}: write {write_id} ended twice")
        return True


def belief_write_id(belief: Belief) -> int:
    """Return the id of the write a belief is about."""
    assert isinstance(belief, Unknown | Wrote | Cleared)
    return belief.write_id
