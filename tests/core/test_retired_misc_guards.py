"""
Guards for behaviour the retired breaker, sync-manager and in-sync tests held.

The old core's CircuitBreaker and SlotSyncManager.calculate_in_sync are gone;
what they guaranteed now lives in the worker's backoff, a slot's fights and
``Slot.status``. These tests hold the parts nothing else did.
"""

from __future__ import annotations

from hypothesis import given, strategies as st
import pytest

from custom_components.lock_code_manager.core.slot import (
    CLEARED,
    WINDOW,
    Holds,
    Repair,
    Seen,
    Slot,
    Status,
    fingerprint,
)
from custom_components.lock_code_manager.core.worker import (
    BACKOFF_AFTER,
    BACKOFF_MAX,
    BACKOFF_MIN,
    LockError,
)

from .test_worker import SALT, FakeLock, _run, _started, rig_factory  # noqa: F401

# The lock as a whole -------------------------------------------------------------


async def test_the_backoff_doubles_from_a_minute_to_half_an_hour_and_starts_over(
    rig_factory,  # noqa: F811
) -> None:
    """
    A lock that stops answering waits a minute, then twice that each time, never
    more than half an hour; once it answers, the next outage starts at a minute.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    waits: list[float] = []
    paced = rig.clock.sleep

    async def _recorded(seconds: float) -> None:
        if seconds >= BACKOFF_MIN:
            waits.append(seconds)
        await paced(seconds)

    rig.worker.sleep = _recorded
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER - 1):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert rig.worker.reachable  # below the threshold: no backoff yet
    with pytest.raises(LockError):
        await rig.worker.read([1])
    assert not rig.worker.reachable

    await _run(rig, lambda: len(waits) >= 8, 400)
    assert waits[:8] == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0, 1800.0]
    assert BACKOFF_MAX == 1800.0

    lock.reads_unanswered = False
    await _run(rig, lambda: rig.worker.reachable, 400)
    assert rig.worker.reachable

    waits.clear()
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    await _run(rig, lambda: bool(waits), 400)
    assert waits[0] == BACKOFF_MIN


# Fights --------------------------------------------------------------------------


async def test_fights_spread_inside_the_window_hold_and_a_resume_forgets_them(
    rig_factory,  # noqa: F811
) -> None:
    """
    Three codes taken back within five minutes, minutes apart, hold the slot;
    once the user resumes it, one more is put right, not held.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    step = WINDOW / 3
    for round_ in range(3):
        rig.clock.now += step - 1
        lock.codes[1] = f"000{round_}"
        rig.worker.push(1, Holds(f"000{round_}"))
        await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)

    rig.worker.resume(1)
    await rig.settle()
    assert rig.status(1) == (Status.IN_SYNC, None)
    lock.codes[1] = "0009"
    rig.worker.push(1, Holds("0009"))
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert lock.codes[1] == "1234"


# In sync -------------------------------------------------------------------------

PINS = st.text(alphabet="0123456789", min_size=4, max_size=8)
WROTE = st.one_of(st.none(), st.just(CLEARED), PINS.map(lambda p: fingerprint(p, SALT)))
HIDDEN = st.sampled_from([Seen.MASKED, Seen.SILENT])


def _slot(want: str | None, wrote: str | None, seen) -> Slot:
    slot = Slot(want)
    slot.wrote = wrote
    slot.seen = seen
    return slot


@given(want=PINS, shown=PINS, wrote=WROTE, same=st.booleans())
def test_a_code_the_lock_shows_is_in_sync_exactly_when_it_is_the_pin(
    want: str, shown: str, wrote: str | None, same: bool
) -> None:
    """Whatever was written, even the PIN itself, a code the lock shows decides."""
    if same:
        wrote = fingerprint(want, SALT)
    status = _slot(want, wrote, Holds(shown)).status(SALT)
    assert status is (Status.IN_SYNC if shown == want else Status.OUT_OF_SYNC)


@given(want=PINS, wrote=WROTE, same=st.booleans())
def test_an_empty_slot_is_never_in_sync_with_a_pin(
    want: str, wrote: str | None, same: bool
) -> None:
    """The lock says empty: out of sync, even right after writing the PIN (#1538)."""
    if same:
        wrote = fingerprint(want, SALT)
    assert _slot(want, wrote, Seen.EMPTY).status(SALT) is Status.OUT_OF_SYNC


@given(want=PINS, wrote=WROTE, seen=HIDDEN, same=st.booleans())
def test_a_hidden_code_is_in_sync_exactly_when_the_pin_was_written(
    want: str, wrote: str | None, seen: Seen, same: bool
) -> None:
    """A lock that will not show or say: the last write is the only evidence."""
    if same:
        wrote = fingerprint(want, SALT)
    expected = wrote == fingerprint(want, SALT)
    status = _slot(want, wrote, seen).status(SALT)
    assert status is (Status.IN_SYNC if expected else Status.OUT_OF_SYNC)


@given(
    wrote=WROTE,
    seen=st.one_of(
        st.sampled_from([Seen.EMPTY, Seen.MASKED, Seen.SILENT, Seen.DISABLED]),
        PINS.map(Holds),
    ),
)
def test_a_slot_wanting_nothing_is_in_sync_once_no_code_remains(
    wrote: str | None, seen
) -> None:
    """
    Empty is in sync; a code it shows (or one not in force) never is; one it
    hides is in sync only once a clear was the last write (#1437).
    """
    if seen is Seen.EMPTY:
        expected = True
    elif seen in (Seen.MASKED, Seen.SILENT):
        expected = wrote == CLEARED
    else:
        expected = False
    status = _slot(None, wrote, seen).status(SALT)
    assert status is (Status.IN_SYNC if expected else Status.OUT_OF_SYNC)


@given(want=st.one_of(st.none(), PINS), wrote=WROTE)
def test_a_slot_never_read_is_unknown_and_a_held_one_suspended(
    want: str | None, wrote: str | None
) -> None:
    """Nothing is claimed before the lock has spoken; a hold outranks everything."""
    assert _slot(want, wrote, None).status(SALT) is Status.UNKNOWN
    held = _slot(want, wrote, Seen.EMPTY)
    held.hold = Repair.CANT_SET
    assert held.status(SALT) is Status.SUSPENDED
