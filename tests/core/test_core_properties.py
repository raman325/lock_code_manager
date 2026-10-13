"""
Properties of the core's pure pieces (credential-model spec, "Testing").

1. Silence never changes a verdict, once a read has completed (before that,
   the first completed read is what lets the judge decide at all).
2. An observation from before the last write never changes a verdict.
3. A disabled code is never in sync.
6. Three charges inside the window trip a breaker, and only a reset untrips it.
7. A write that raised never leaves a belief.
8. A fresh observation of the same value moves its time on.

Property 4 (allocation) and 5 (observed data holds only what a device said)
belong to the lock loop and provider port, not to these pure pieces.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.core.belief import BeliefRecord
from custom_components.lock_code_manager.core.breakers import SlotBreaker
from custom_components.lock_code_manager.core.judge import (
    LastWrite,
    Status,
    Written,
    judge,
    written,
)
from custom_components.lock_code_manager.core.observations import ObservationLog
from custom_components.lock_code_manager.core.values import (
    DISABLED,
    EMPTY,
    HOLDS_MASKED,
    NOTHING,
    WANT_EMPTY,
    Holds,
    Strength,
    Unknown,
    WantPin,
    pin_at,
)

SLOT = pin_at(1)
PINS = st.sampled_from(["1234", "5678", "0000"])
READINGS = st.one_of(
    st.just(EMPTY),
    st.just(HOLDS_MASKED),
    st.just(DISABLED),
    st.just(NOTHING),
    PINS.map(Holds),
)
DESIRED = st.one_of(st.just(WANT_EMPTY), PINS.map(WantPin))
WRITTEN = st.sampled_from(list(Written)).map(
    lambda kind: LastWrite(kind, None if kind is Written.NOTHING else 1)
)
TIMES = st.floats(min_value=0, max_value=1_000, allow_nan=False)


def _verdict(log: ObservationLog, desired, last_write: LastWrite, tripped: bool):
    return judge(
        desired,
        last_write,
        log.informative_since_write(SLOT),
        ever_read=log.ever_read(SLOT),
        tripped=tripped,
    )


@given(
    history=st.lists(st.tuples(READINGS, TIMES), max_size=6),
    desired=DESIRED,
    last_write=WRITTEN,
    tripped=st.booleans(),
    silence_at=TIMES,
)
def test_silence_never_changes_a_verdict(
    history, desired, last_write, tripped, silence_at
) -> None:
    """Property 1, once the lock has said anything about the address."""
    log = ObservationLog()
    # In arrival order, not time order: late arrivals included.
    for reading, at in history:
        log.observe(SLOT, reading, at)
    log.observe(SLOT, NOTHING, -1.0)  # a read has completed
    before = _verdict(log, desired, last_write, tripped)
    log.observe(SLOT, NOTHING, silence_at)
    assert _verdict(log, desired, last_write, tripped) == before


@given(
    reading=READINGS,
    before=st.one_of(st.none(), READINGS),
    after=st.one_of(st.none(), READINGS),
    returned=st.booleans(),
    write_at=TIMES,
    gap=st.floats(min_value=0.001, max_value=100, allow_nan=False),
    desired=DESIRED,
    last_write=WRITTEN,
)
def test_an_observation_from_before_the_write_changes_nothing(
    reading, before, after, returned, write_at, gap, desired, last_write
) -> None:
    """
    Property 2: a reading said before the last write started is superseded.

    Whether or not a read had completed before the write, with or without a
    reading after it, and whether the write returned or raised.
    """
    with_it = ObservationLog()
    without = ObservationLog()
    for log in (with_it, without):
        if before is not None:
            log.observe(SLOT, before, write_at - 2 * gap)
        log.mark_write(SLOT, write_at)
        if returned:
            log.mark_returned(SLOT, write_at + gap)
        if after is not None:
            log.observe(SLOT, after, write_at + 2 * gap)
    # Arriving after everything else, but said before the write started.
    with_it.observe(SLOT, reading, write_at - gap)

    assert _verdict(with_it, desired, last_write, False) == _verdict(
        without, desired, last_write, False
    )


@given(desired=DESIRED, last_write=WRITTEN, ever_read=st.booleans())
def test_a_disabled_code_is_never_in_sync(desired, last_write, ever_read) -> None:
    """Property 3."""
    log = ObservationLog()
    log.observe(SLOT, DISABLED, 1.0)
    verdict = judge(
        desired,
        last_write,
        log.informative_since_write(SLOT),
        ever_read=ever_read,
        tripped=False,
    )
    assert verdict.status is not Status.IN_SYNC


@given(
    times=st.lists(TIMES, min_size=1, max_size=10),
    later=st.floats(min_value=0, max_value=100_000, allow_nan=False),
)
def test_a_breaker_trips_on_three_inside_the_window_and_stays_tripped(
    times, later
) -> None:
    """
    Property 6, with charges in arrival order, late ones included.

    The model: after each charge, the charges so far that fall inside the
    window back from the latest of them; three of those trip it for good.
    """
    breaker = SlotBreaker(window=300.0, limit=3)
    should_trip = False
    for index, at in enumerate(times):
        breaker.charge(at)
        so_far = times[: index + 1]
        latest = max(so_far)
        recent = [t for t in so_far if t > latest - 300.0]
        should_trip = should_trip or len(recent) >= 3
        assert breaker.tripped is should_trip
    breaker.charge(max(times) + later)
    assert breaker.tripped or not should_trip
    breaker.reset()
    assert not breaker.tripped


@given(
    writes=st.lists(st.tuples(PINS, st.booleans()), min_size=1, max_size=8),
)
def test_a_write_that_raised_never_leaves_a_belief(writes) -> None:
    """Property 7: each write is invalidated first and recorded only if it returned."""
    beliefs = BeliefRecord(b"salt")
    for at, (pin, returned) in enumerate(writes):
        write = beliefs.invalidate(SLOT, float(at))
        if returned:
            beliefs.record_pin(SLOT, write, pin, Strength.APPLIED, float(at) + 0.5)
        else:
            beliefs.record_failed(SLOT, write)
            assert isinstance(beliefs.get(SLOT), Unknown)
            assert written(beliefs, SLOT, WantPin(pin)).kind is Written.FAILED


@given(reading=READINGS, first=TIMES, gap=st.floats(min_value=0, max_value=100))
def test_the_same_value_read_again_is_news(reading, first, gap) -> None:
    """Property 8."""
    log = ObservationLog()
    log.observe(SLOT, reading, first)
    assert log.observe(SLOT, reading, first + gap)
    assert log.latest(SLOT).at == first + gap
