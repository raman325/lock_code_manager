"""What Lock Code Manager last wrote: started, then ended exactly once."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.core.belief import BeliefRecord
from custom_components.lock_code_manager.core.values import (
    Cleared,
    Strength,
    Unknown,
    WantPin,
    pin_at,
)

SLOT = pin_at(1)


def test_a_write_that_returned_is_believed() -> None:
    """Returned means believed: the PIN written is held, by fingerprint."""
    beliefs = BeliefRecord(b"salt")
    write = beliefs.invalidate(SLOT, at=1.0)
    assert beliefs.record_pin(SLOT, write, "1234", Strength.APPLIED, at=2.0)

    assert beliefs.holds(SLOT, WantPin("1234"))
    assert not beliefs.holds(SLOT, WantPin("4321"))


def test_a_write_that_raised_leaves_no_belief() -> None:
    """Raised means failed: the address is unknown, and nothing is out (decision 3)."""
    beliefs = BeliefRecord(b"salt")
    first = beliefs.invalidate(SLOT, at=1.0)
    beliefs.record_pin(SLOT, first, "1234", Strength.APPLIED, at=2.0)
    second = beliefs.invalidate(SLOT, at=3.0)
    assert beliefs.get(SLOT) == Unknown(second, 3.0)

    assert beliefs.record_failed(SLOT, second)
    assert beliefs.get(SLOT) == Unknown(second, 3.0, failed=True)
    assert not beliefs.holds(SLOT, WantPin("1234"))


def test_only_the_latest_write_can_settle_the_address() -> None:
    """A write ending after a newer one started records nothing."""
    beliefs = BeliefRecord(b"salt")
    stale = beliefs.invalidate(SLOT, at=1.0)
    current = beliefs.invalidate(SLOT, at=5.0)

    assert not beliefs.record_pin(SLOT, stale, "1234", Strength.APPLIED, at=6.0)
    assert beliefs.get(SLOT) == Unknown(current, 5.0)
    assert beliefs.record_cleared(SLOT, current, Strength.SENT, at=7.0)
    assert beliefs.get(SLOT) == Cleared(current, Strength.SENT, 7.0)


def test_a_write_ending_twice_is_a_bug() -> None:
    """Each write ends once: returned or raised."""
    beliefs = BeliefRecord(b"salt")
    write = beliefs.invalidate(SLOT, at=1.0)
    beliefs.record_cleared(SLOT, write, Strength.SENT, at=2.0)

    with pytest.raises(ValueError):
        beliefs.record_cleared(SLOT, write, Strength.SENT, at=3.0)
    with pytest.raises(ValueError):
        beliefs.record_failed(SLOT, write)


def test_a_write_returning_after_its_address_was_forgotten_records_nothing() -> None:
    """A slot removed while its write was out is no longer anyone's to believe."""
    beliefs = BeliefRecord(b"salt")
    write = beliefs.invalidate(SLOT, at=1.0)
    beliefs.forget(SLOT)

    assert not beliefs.record_pin(SLOT, write, "1234", Strength.APPLIED, at=2.0)
    assert beliefs.get(SLOT) is None
