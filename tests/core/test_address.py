"""One owner per address: readings and writes, ordered against each other."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.core.address import (
    AddressState,
    AddressView,
    Written,
)
from custom_components.lock_code_manager.core.values import (
    EMPTY,
    HOLDS_MASKED,
    NOTHING,
    WANT_EMPTY,
    Holds,
    Observation,
    Strength,
    WantPin,
    pin_at,
)

WANT = WantPin("1234")


def _state() -> AddressState:
    return AddressState(pin_at(1), b"salt")


# Writes ---------------------------------------------------------------------


def test_a_write_that_returned_is_believed() -> None:
    """Returned means believed: the PIN written is what the view compares against."""
    state = _state()
    with state.writing(WANT, 1.0) as write:
        assert state.view(WANT).last_write is Written.IN_FLIGHT
        assert write.returned(Strength.APPLIED, 2.0)

    assert state.view(WANT).last_write is Written.TARGET
    assert state.view(WantPin("4321")).last_write is Written.OTHER
    assert state.view(WANT_EMPTY).last_write is Written.OTHER


def test_a_write_that_raised_leaves_no_belief() -> None:
    """Raised means failed (decision 3), and the earlier belief is gone too."""
    state = _state()
    with state.writing(WANT, 1.0) as write:
        write.returned(Strength.APPLIED, 2.0)
    with pytest.raises(ValueError), state.writing(WANT_EMPTY, 3.0):
        raise ValueError("the lock refused")

    assert state.view(WANT).last_write is Written.FAILED
    assert state.view(WANT_EMPTY).last_write is Written.FAILED


def test_a_write_left_without_an_ending_fails() -> None:
    """Cancelled or forgotten, a write never sits in flight for good."""
    state = _state()
    with state.writing(WANT, 1.0):
        pass

    assert state.view(WANT).last_write is Written.FAILED


def test_a_write_that_never_reached_the_lock_leaves_the_address_as_it_was() -> None:
    """Abandoned (nothing sent): the earlier write and its evidence still stand."""
    state = _state()
    with state.writing(WANT, 1.0) as write:
        write.returned(Strength.APPLIED, 2.0)
    state.observe(Holds("1234"), 3.0)
    with state.writing(WantPin("4321"), 4.0) as write:
        assert write.abandoned()

    assert state.view(WANT).last_write is Written.TARGET
    assert state.view(WANT).seen == Observation(Holds("1234"), 3.0)


def test_abandoning_an_older_write_leaves_the_newer_one_current() -> None:
    """Only the newest write can put the address back: an older one changes nothing."""
    state = _state()
    with state.writing(WANT, 1.0) as older:
        with state.writing(WANT_EMPTY, 2.0) as newer:
            assert not older.abandoned()
            assert state.view(WANT_EMPTY).last_write is Written.IN_FLIGHT
            newer.returned(Strength.SENT, 3.0)
    assert state.view(WANT_EMPTY).last_write is Written.TARGET


def test_a_settling_that_fails_changes_nothing() -> None:
    """
    Review round 4, finding 1: a return that cannot be recorded leaves the write out.

    It then ends as failed when its block exits, and never shows the
    previous write's belief as current.
    """
    state = _state()
    with state.writing(WANT, 1.0) as write:
        write.returned(Strength.APPLIED, 2.0)
    with state.writing(WantPin("\ud800"), 3.0) as write:
        with pytest.raises(UnicodeEncodeError):
            write.returned(Strength.APPLIED, 4.0)
        assert state.view(WANT).last_write is Written.IN_FLIGHT

    assert state.view(WANT).last_write is Written.FAILED


def test_only_the_newest_write_settles_the_address() -> None:
    """A write ending after a newer one started records nothing (review round 3)."""
    state = _state()
    with state.writing(WANT, 1.0) as stale:
        with state.writing(WANT_EMPTY, 5.0) as current:
            assert not stale.returned(Strength.APPLIED, 6.0)
            assert state.view(WANT).last_write is Written.IN_FLIGHT
            assert current.returned(Strength.SENT, 7.0)
        assert state.view(WANT_EMPTY).last_write is Written.TARGET


def test_a_write_ending_twice_is_a_bug() -> None:
    """Each write ends once: returned, failed or abandoned."""
    state = _state()
    with state.writing(WANT, 1.0) as write:
        write.returned(Strength.APPLIED, 2.0)
        with pytest.raises(RuntimeError):
            write.returned(Strength.APPLIED, 3.0)
        with pytest.raises(RuntimeError):
            write.failed()
        with pytest.raises(RuntimeError):
            write.abandoned()


# Readings -------------------------------------------------------------------


def test_a_reading_from_before_a_write_returned_is_superseded() -> None:
    """Until a write returns, the lock may still report the value it replaced."""
    state = _state()
    state.observe(EMPTY, 0.0)
    with state.writing(WANT, 10.0) as write:
        state.observe(EMPTY, 11.0)
        write.returned(Strength.APPLIED, 12.0)

    assert state.view(WANT).seen is None
    state.observe(Holds("1234"), 13.0)
    assert state.view(WANT).seen == Observation(Holds("1234"), 13.0)


def test_a_failed_write_is_measured_from_its_start() -> None:
    """With no return to wait for, anything said after the start counts."""
    state = _state()
    with state.writing(WANT, 10.0):
        pass
    state.observe(EMPTY, 11.0)

    assert state.view(WANT).seen == Observation(EMPTY, 11.0)


def test_a_stale_write_returning_does_not_move_the_cutoff() -> None:
    """Review round 3, finding 1: evidence after the current write survives."""
    state = _state()
    with state.writing(WANT, 1.0) as stale:
        with state.writing(WANT, 2.0) as current:
            stale.returned(Strength.APPLIED, 6.0)
            current.returned(Strength.APPLIED, 4.0)
    state.observe(Holds("9999"), 5.0)

    assert state.view(WANT).seen == Observation(Holds("9999"), 5.0)


def test_silence_never_erases_what_the_lock_said() -> None:
    """A later Nothing leaves the last informative reading in place."""
    state = _state()
    state.observe(Holds("1234"), 1.0)
    state.observe(NOTHING, 2.0)

    assert state.view(WANT).seen == Observation(Holds("1234"), 1.0)


def test_silence_does_not_shut_out_a_later_real_reading() -> None:
    """A reading newer than the last real one counts, whatever silence came between."""
    state = _state()
    state.observe(Holds("1234"), 1.0)
    state.observe(NOTHING, 5.0)

    assert state.observe(EMPTY, 3.0)
    assert state.view(WANT).seen == Observation(EMPTY, 3.0)


def test_a_late_reading_changes_nothing() -> None:
    """A reading older than the one held arrived late, and is dropped."""
    state = _state()
    state.observe(Holds("1234"), 5.0)

    assert not state.observe(EMPTY, 3.0)
    assert state.view(WANT).seen == Observation(Holds("1234"), 5.0)


def test_a_fresh_reading_of_the_same_value_is_news() -> None:
    """The same value read again moves its time on (#737)."""
    state = _state()
    state.observe(EMPTY, 1.0)

    assert state.observe(EMPTY, 4.0)
    assert state.view(WANT_EMPTY).seen == Observation(EMPTY, 4.0)


# Whether a read has completed ----------------------------------------------


def test_never_read_until_a_read_completes_silence_included() -> None:
    """A silent read completes a read (spec section 1)."""
    state = _state()
    assert state.view(WANT) == AddressView(Written.NOTHING, None, False)
    state.observe(NOTHING, 1.0)
    assert state.ever_read


def test_a_reading_from_before_a_write_arriving_late_is_not_a_read() -> None:
    """Whether a read completed is fixed when a write starts."""
    state = _state()
    with state.writing(WANT, 10.0):
        state.observe(EMPTY, 5.0)
        assert not state.ever_read
        state.observe(NOTHING, 11.0)
        assert state.ever_read


def test_a_read_completed_never_goes_back() -> None:
    """Review round 3, finding 2: a write returning cannot undo a read."""
    state = _state()
    with state.writing(WANT, 1.0) as write:
        state.observe(NOTHING, 1.5)
        assert state.ever_read
        write.returned(Strength.APPLIED, 2.0)
    assert state.ever_read

    later = _state()
    later.observe(HOLDS_MASKED, 0.5)
    with later.writing(WANT, 1.0):
        assert later.ever_read
