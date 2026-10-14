"""The core swap's first review: what it found, kept as regression tests."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from custom_components.lock_code_manager.core.registry import LockRegistry
from custom_components.lock_code_manager.core.slot import Repair, Status
from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationUnsupported,
)

from .test_adapter import ports  # noqa: F401  (fixture)
from .test_registry import Locks, _settle
from .test_worker import (  # noqa: F401  (rig_factory is a fixture)
    FakeLock,
    _poll,
    _run,
    _started,
    rig_factory,
)


async def _fast(_: float) -> None:
    await asyncio.sleep(0)


async def test_f1_a_slot_past_capacity_is_no_answer_from_a_dead_lock(ports) -> None:  # noqa: F811
    """
    A read of only slots past the lock's capacity asks the lock nothing.

    The port filters them out and returns {} -- which the worker takes as
    the lock answering. During an outage the probe of the held past-capacity
    slot then "answers": the backoff ends, the owed try of the normal slot is
    paid, and the lock never goes offline. The outage holds a normal slot.
    """
    _, worker, lock = await ports()
    worker.sleep = _fast

    async def capacity(self, slot: int) -> None:
        if slot > 30:
            raise LockOperationUnsupported(f"slot {slot} is past 30")

    with patch.object(type(lock), "_assert_slot_within_capacity", capacity):
        worker.want(1, "1111")
        worker.want(40, "4040")
        worker.start()
        await worker.settled()
        assert worker.status(1) == (Status.IN_SYNC, None)
        assert worker.status(40) == (Status.SUSPENDED, Repair.CANT_SET)

        # The lock goes dead, and the user changes slot 1's PIN.
        lock.fail("get", error=LockDisconnected)
        lock.fail("set", error=LockDisconnected)
        worker.want(1, "2222")
        async with asyncio.timeout(5):
            while not worker.offline and worker.status(1)[1] is None:
                await asyncio.sleep(0)

        # An outage alone never holds a normal slot.
        assert worker.status(1)[1] is None
        assert worker.offline
        await worker.stop(grace=0.1)


async def test_f2_a_one_off_write_asked_again_is_tried_again() -> None:
    """
    An unmanaged clear held after its tries, asked for again by the user.

    On a lock an entry holds, the held one-off slot stays in the worker; the
    same request again changes no target, so nothing resumes it: the call
    answers "held" at once and the lock is never asked, for ever.
    """
    lock = FakeLock(codes={1: "1111", 7: "7777"}, refuse_clear={7})
    locks = Locks(front=lock)
    registry = LockRegistry(locks.make, grace=0.1)
    worker = await registry.hold("front", "a", {1: "1111"})
    try:
        await _check_retry(registry, worker, lock, _settle)
    finally:
        await registry.stop()


async def _check_retry(registry, worker, lock, _settle) -> None:
    await _settle(worker)
    await registry.write_once("front", 7, None)
    await _settle(worker)
    assert worker.status(7) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)

    # The user fixes the lock and presses "clear" again.
    lock.refuse_clear = set()
    await registry.write_once("front", 7, None)
    await _settle(worker)

    assert 7 not in lock.codes


async def test_f1b_a_lock_with_only_slots_past_capacity_holds_them_at_once(
    ports,  # noqa: F811
) -> None:
    """Its reads ask nothing: each slot is held, the lock is not taken for gone."""
    _, worker, lock = await ports()
    worker.sleep = _fast

    async def capacity(self, slot: int) -> None:
        if slot > 30:
            raise LockOperationUnsupported(f"slot {slot} is past 30")

    with patch.object(type(lock), "_assert_slot_within_capacity", capacity):
        worker.want(40, "4040")
        worker.start()
        await worker.settled()
        assert worker.status(40) == (Status.SUSPENDED, Repair.CANT_SET)
        assert worker.reachable
        assert 40 not in lock.codes
        await worker.stop(grace=0.1)


async def test_u1_polls_of_a_lock_with_a_held_ignored_slot(rig_factory) -> None:  # noqa: F811
    """
    A lock that fails a whole read over one slot it ignores (ZHA).

    Once slot 7 is held, polls leave it out: the drift on slot 1 is seen, and
    the lock is not backed off by a slot nothing is trying any more.
    """
    lock = FakeLock({1: "1111", 2: "2222"}, strict=True)
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.ignored = {7}
    rig.worker.want(7, "7777")
    await _run(rig, lambda: rig.status(7)[0] is Status.SUSPENDED)
    assert rig.status(7)[0] is Status.SUSPENDED
    lock.codes[1] = "0000"  # changed at the keypad
    for _ in range(3):
        await _poll(rig)
    await rig.settle()
    # The drift on slot 1 is seen and put right; the lock is not taken as gone.
    assert lock.codes[1] == "1111"
    assert (1, "1111") in lock.writes
    assert rig.worker.reachable
