"""What Lock Code Manager last wrote: invalidated before, recorded after."""

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
    beliefs.invalidate(SLOT, at=1.0)
    beliefs.record_pin(SLOT, "1234", Strength.APPLIED, at=2.0)

    assert beliefs.holds(SLOT, WantPin("1234"))
    assert not beliefs.holds(SLOT, WantPin("4321"))


def test_a_write_that_raised_leaves_no_belief() -> None:
    """Raised means failed: the address stays unknown (decision 3)."""
    beliefs = BeliefRecord(b"salt")
    beliefs.invalidate(SLOT, at=1.0)
    beliefs.record_pin(SLOT, "1234", Strength.APPLIED, at=2.0)
    beliefs.invalidate(SLOT, at=3.0)

    assert beliefs.get(SLOT) == Unknown(3.0)
    assert not beliefs.holds(SLOT, WantPin("1234"))


def test_recording_over_a_settled_write_is_a_bug() -> None:
    """A write is recorded only over the Unknown its start left."""
    beliefs = BeliefRecord(b"salt")
    beliefs.invalidate(SLOT, at=1.0)
    assert beliefs.record_cleared(SLOT, Strength.SENT, at=2.0)
    assert beliefs.get(SLOT) == Cleared(Strength.SENT, 2.0)

    with pytest.raises(ValueError):
        beliefs.record_cleared(SLOT, Strength.SENT, at=3.0)


def test_a_write_returning_after_its_address_was_forgotten_records_nothing() -> None:
    """A slot removed while its write was out is no longer anyone's to believe."""
    beliefs = BeliefRecord(b"salt")
    beliefs.invalidate(SLOT, at=1.0)
    beliefs.forget(SLOT)

    assert not beliefs.record_pin(SLOT, "1234", Strength.APPLIED, at=2.0)
    assert beliefs.get(SLOT) is None
