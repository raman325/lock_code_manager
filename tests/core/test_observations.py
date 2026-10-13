"""What a lock said, ordered against Lock Code Manager's writes."""

from __future__ import annotations

from custom_components.lock_code_manager.core.observations import ObservationLog
from custom_components.lock_code_manager.core.values import (
    EMPTY,
    NOTHING,
    Holds,
    Observation,
    pin_at,
)

SLOT = pin_at(1)


def test_an_observation_from_before_a_write_is_superseded() -> None:
    """What the slot held before a write neither confirms nor contradicts it."""
    log = ObservationLog()
    log.observe(SLOT, EMPTY, at=1.0)
    log.mark_write(SLOT, at=2.0)

    assert log.informative_since_write(SLOT) is None
    assert log.is_superseded(SLOT)
    assert log.latest(SLOT) == Observation(EMPTY, 1.0)


def test_silence_never_erases_what_the_lock_said() -> None:
    """A later Nothing leaves the last informative reading in place."""
    log = ObservationLog()
    log.observe(SLOT, Holds("1234"), at=1.0)
    log.observe(SLOT, NOTHING, at=2.0)

    assert log.informative_since_write(SLOT) == Observation(Holds("1234"), 1.0)
    assert log.latest(SLOT) == Observation(NOTHING, 2.0)


def test_a_late_observation_changes_nothing() -> None:
    """An observation older than the one held arrived late, and is dropped."""
    log = ObservationLog()
    log.observe(SLOT, Holds("1234"), at=5.0)

    assert not log.observe(SLOT, EMPTY, at=3.0)
    assert log.latest(SLOT) == Observation(Holds("1234"), 5.0)


def test_a_fresh_reading_of_the_same_value_is_news() -> None:
    """The same value read again moves its time on (#737)."""
    log = ObservationLog()
    log.observe(SLOT, EMPTY, at=1.0)

    assert log.observe(SLOT, EMPTY, at=4.0)
    assert log.latest(SLOT) == Observation(EMPTY, 4.0)


def test_never_read_is_not_a_reading() -> None:
    """An address the lock said nothing about has no observation at all."""
    log = ObservationLog()

    assert not log.ever_read(SLOT)
    assert log.latest(SLOT) is None
    log.observe(SLOT, NOTHING, at=1.0)
    assert log.ever_read(SLOT)
    assert log.informative_since_write(SLOT) is None


def test_silence_does_not_shut_out_a_later_real_reading() -> None:
    """A reading newer than the last real one counts, whatever silence came between."""
    log = ObservationLog()
    log.observe(SLOT, Holds("1234"), at=1.0)
    log.observe(SLOT, NOTHING, at=5.0)

    assert log.observe(SLOT, EMPTY, at=3.0)
    assert log.informative_since_write(SLOT) == Observation(EMPTY, 3.0)
    assert log.latest(SLOT) == Observation(NOTHING, 5.0)


def test_a_report_stamped_while_the_write_was_out_is_superseded() -> None:
    """Until a write returns, the lock may still report the value it replaced."""
    log = ObservationLog()
    log.mark_write(SLOT, at=10.0)
    log.observe(SLOT, EMPTY, at=11.0)
    log.mark_returned(SLOT, at=12.0)

    assert log.informative_since_write(SLOT) is None
    log.observe(SLOT, Holds("1234"), at=13.0)
    assert log.informative_since_write(SLOT) == Observation(Holds("1234"), 13.0)


def test_a_reading_from_before_a_write_arriving_late_is_not_a_read() -> None:
    """Whether a read completed is fixed when a write starts, and moved on only after."""
    log = ObservationLog()
    log.mark_write(SLOT, at=10.0)
    log.observe(SLOT, EMPTY, at=5.0)

    assert not log.ever_read(SLOT)
    log.observe(SLOT, NOTHING, at=11.0)
    assert log.ever_read(SLOT)


def test_a_read_before_the_write_still_counts_after_it() -> None:
    """A read that completed before a write started stays completed."""
    log = ObservationLog()
    log.observe(SLOT, EMPTY, at=5.0)
    log.mark_write(SLOT, at=10.0)

    assert log.ever_read(SLOT)
