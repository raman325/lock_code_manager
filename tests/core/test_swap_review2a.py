"""The core swap's second review (2a): what it found, kept as regression tests."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.lock_code_manager.core.worker import BACKOFF_AFTER, LockError

from .test_worker import FakeLock, _started, rig_factory  # noqa: F401


async def test_a_probe_from_outside_leaves_no_stale_backoff_timer(rig_factory) -> None:  # noqa: F811
    """
    A lock brought back by a probe from outside (the hard refresh service)
    does not keep the old backoff's timer.

    The timer still sleeping out the old backoff blocks the next outage's
    probe from being scheduled: the lock stays down, every sync dropped,
    until that old timer fires -- up to 30 minutes for a 60s backoff.
    """
    lock = FakeLock({1: "1234"})
    rig = rig_factory(lock)
    stuck = asyncio.Event()
    first_long = True

    async def sleep(seconds: float) -> None:
        nonlocal first_long
        rig.clock.now += seconds
        if seconds >= 60 and first_long:
            # The first backoff's timer: still sleeping (a long backoff).
            first_long = False
            await stuck.wait()
        await asyncio.sleep(0)

    rig.worker.sleep = sleep
    await _started(rig, {1: "1234"})

    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable

    # The hard refresh service: a probe from outside, and the lock answers.
    lock.reads_unanswered = False
    await rig.worker.read(probe=True)
    assert rig.worker.reachable

    # A second outage, and the lock answers again once probed.
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable
    lock.reads_unanswered = False
    for _ in range(50):
        if rig.worker.reachable:
            break
        await asyncio.sleep(0)
    assert rig.worker.reachable
    stuck.set()
