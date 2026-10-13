"""
Properties of the core's pure pieces (credential-model spec, "Testing").

1. Silence never changes a verdict, once a read has completed (before that,
   the first completed read is what lets the judge decide at all).
2. A reading said before the current write started never changes a verdict.
3. A disabled code is never in sync.
6. Three charges inside the window trip a breaker, and only a reset untrips it.
7. A write that failed never leaves a belief.
8. A fresh reading of the same value moves its time on.

Property 4 (allocation) and 5 (observed data holds only what a device said)
belong to the lock loop and provider port, not to these pure pieces.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.core.address import AddressState, Written
from custom_components.lock_code_manager.core.breakers import SlotBreaker
from custom_components.lock_code_manager.core.judge import Status, judge
from custom_components.lock_code_manager.core.values import (
    DISABLED,
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

PINS = st.sampled_from(["1234", "5678", "0000"])
READINGS = st.one_of(
    st.just(EMPTY),
    st.just(HOLDS_MASKED),
    st.just(DISABLED),
    st.just(NOTHING),
    PINS.map(Holds),
)
DESIRED = st.one_of(st.just(WANT_EMPTY), PINS.map(WantPin))
TIMES = st.floats(min_value=0, max_value=1_000, allow_nan=False)
# What happened to a write: still out, failed, or returned.
ENDINGS = st.sampled_from(["out", "failed", "returned", "abandoned"])


def _state() -> AddressState:
    return AddressState(pin_at(1), b"salt")


def _write(state: AddressState, target, start: float, ending: str, gap: float):
    """
    Start a write and end it the given way.

    "out" leaves it in flight: the block is entered and not left, as a write
    the lock is still answering would be.
    """
    block = state.writing(target, start)
    write = block.__enter__()
    if ending == "failed":
        block.__exit__(None, None, None)
    elif ending == "returned":
        write.returned(Strength.APPLIED, start + gap)
        block.__exit__(None, None, None)
    elif ending == "abandoned":
        write.abandoned()
        block.__exit__(None, None, None)
    return write


def _verdict(state: AddressState, desired, tripped: bool):
    return judge(desired, state.view(desired), tripped=tripped)


@given(
    history=st.lists(st.tuples(READINGS, TIMES), max_size=6),
    write=st.one_of(st.none(), st.tuples(DESIRED, TIMES, ENDINGS)),
    desired=DESIRED,
    tripped=st.booleans(),
    silence_at=TIMES,
)
def test_silence_never_changes_a_verdict(
    history, write, desired, tripped, silence_at
) -> None:
    """Property 1, in arrival order (late arrivals included), with or without a write."""
    state = _state()
    state.observe(NOTHING, -1.0)  # a read has completed
    if write is not None:
        target, start, ending = write
        _write(state, target, start, ending, 1.0)
    for reading, at in history:
        state.observe(reading, at)
    before = _verdict(state, desired, tripped)
    state.observe(NOTHING, silence_at)
    assert _verdict(state, desired, tripped) == before


@given(
    reading=READINGS,
    before=st.one_of(st.none(), READINGS),
    after=st.one_of(st.none(), READINGS),
    target=DESIRED,
    # An abandoned write never happened, so nothing is superseded by it.
    ending=st.sampled_from(["out", "failed", "returned"]),
    write_at=TIMES,
    gap=st.floats(min_value=0.001, max_value=100, allow_nan=False),
    desired=DESIRED,
)
def test_a_reading_from_before_the_write_changes_nothing(
    reading, before, after, target, ending, write_at, gap, desired
) -> None:
    """
    Property 2: a reading said before the current write started is superseded.

    Whether or not a read had completed before the write, with or without a
    reading after it, and whether the write is out, failed or returned.
    """
    with_it, without = _state(), _state()
    for state in (with_it, without):
        if before is not None:
            state.observe(before, write_at - 2 * gap)
        _write(state, target, write_at, ending, gap)
        if after is not None:
            state.observe(after, write_at + 2 * gap)
    # Arriving after everything else, but said before the write started.
    with_it.observe(reading, write_at - gap)

    assert _verdict(with_it, desired, False) == _verdict(without, desired, False)


@given(
    target=DESIRED,
    ending=ENDINGS,
    desired=DESIRED,
    read_first=st.booleans(),
)
def test_a_disabled_code_is_never_in_sync(target, ending, desired, read_first) -> None:
    """Property 3, whatever was written and however it ended."""
    state = _state()
    if read_first:
        state.observe(NOTHING, 0.0)
    _write(state, target, 1.0, ending, 1.0)
    state.observe(DISABLED, 5.0)
    assert _verdict(state, desired, False).status is not Status.IN_SYNC


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


@given(writes=st.lists(st.tuples(DESIRED, ENDINGS), min_size=1, max_size=8))
def test_a_write_that_failed_never_leaves_a_belief(writes) -> None:
    """Property 7: after a failed write, nothing is believed, whatever came before."""
    state = _state()
    for at, (target, ending) in enumerate(writes):
        _write(state, target, float(at), ending, 0.5)
        if ending == "failed":
            for desired in (target, WANT_EMPTY, WantPin("1234")):
                assert state.view(desired).last_write is Written.FAILED


@given(reading=READINGS, first=TIMES, gap=st.floats(min_value=0, max_value=100))
def test_the_same_value_read_again_is_news(reading, first, gap) -> None:
    """Property 8."""
    state = _state()
    state.observe(reading, first)
    assert state.observe(reading, first + gap)
    if reading.informative:
        assert state.view(WANT_EMPTY).seen == Observation(reading, first + gap)
