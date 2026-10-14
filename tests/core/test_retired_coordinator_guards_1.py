"""
Guards for behaviour the retired lock coordinator's tests covered (part 1).

What the old coordinator did that still matters, now the worker's and the
provider port's: the poll and drift-check cadence, push subscriptions, pushes
reaching the worker, and backing a silent lock off.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.core.slot import Holds, Seen
from custom_components.lock_code_manager.core.worker import (
    BACKOFF_AFTER,
    BACKOFF_MAX,
    BACKOFF_MIN,
    OFFLINE_AFTER,
    LockError,
)
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.models import SlotCredential

from ..common import MockLCMPushLock
from .test_adapter import ports  # noqa: F401
from .test_worker import FakeLock, _started, rig_factory  # noqa: F401


async def _reads_after(hass: HomeAssistant, lock, worker, later: timedelta) -> int:
    """Return how many reads reach the lock when the clock moves ``later`` on."""
    before = len(lock.service_calls["get_usercodes"])
    async_fire_time_changed(hass, dt_util.utcnow() + later)
    await hass.async_block_till_done()
    await worker.settled()
    return len(lock.service_calls["get_usercodes"]) - before


async def _push_lock(ports, drift: timedelta | None, *, push: bool = True):  # noqa: F811
    """Set up a push lock with a drift-check interval, its slot 1 in sync."""
    port, worker, lock = await ports(MockLCMPushLock, setup=False)
    lock._supports_push = push
    lock._hard_refresh_interval = drift
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(lock.hass)
    await port.async_setup(entry)
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    return port, worker, lock


async def test_a_lock_that_pushes_is_read_by_its_drift_check_alone(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """No scan for a lock that pushes; its drift check reads it on time."""
    _, worker, lock = await _push_lock(ports, timedelta(hours=1))

    assert await _reads_after(hass, lock, worker, timedelta(minutes=5)) == 0
    assert await _reads_after(hass, lock, worker, timedelta(minutes=61)) > 0


async def test_a_lock_that_pushes_with_no_drift_check_is_never_polled(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """Without a drift-check interval nothing reads a pushing lock on a timer."""
    _, worker, lock = await _push_lock(ports, None)

    assert await _reads_after(hass, lock, worker, timedelta(hours=3)) == 0


async def test_a_lock_that_does_not_push_is_never_subscribed(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """Set up and unloaded, a lock without push is neither subscribed nor unsubscribed."""
    port, worker, lock = await _push_lock(ports, None, push=False)
    assert lock.subscribe_calls == 0

    await worker.stop(grace=0.1)
    await port.async_unload(False)

    assert (lock.subscribe_calls, lock.unsubscribe_calls) == (0, 0)


async def test_a_lock_that_seems_to_stop_pushing_is_not_polled(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """
    Push data going missing for a while is not a lock that stopped pushing.

    The provider says so after every setup that succeeds; chasing it would
    give the lock a poll that the next publication takes away again.
    """
    _, worker, lock = await _push_lock(ports, None)
    lock._supports_push = False
    assert lock.coordinator is not None
    lock.coordinator.note_push_capability()

    assert await _reads_after(hass, lock, worker, timedelta(minutes=5)) == 0


async def test_a_push_about_several_slots_reaches_each(
    ports,  # noqa: F811
) -> None:
    """Every slot in one push reaches the worker; one nobody manages is kept aside."""
    port, worker, lock = await ports(MockLCMPushLock)
    worker.want(1, "1234")
    worker.want(3, None)
    pushed: list[tuple[int, object]] = []
    worker.push = lambda slot, value: pushed.append((slot, value))  # type: ignore[method-assign]
    assert lock.coordinator is not None

    lock.coordinator.push_update(
        {
            1: SlotCredential.known("9999"),
            3: SlotCredential.empty(),
            7: SlotCredential.known("7777"),
        }
    )

    assert pushed == [(1, Holds("9999")), (3, Seen.EMPTY)]
    assert port.others == {7: Holds("7777")}


async def test_a_poll_the_lock_does_not_answer_raises_nothing_and_keeps_the_reading(
    ports,  # noqa: F811
) -> None:
    """A failed poll is the worker's to count: the slot keeps what was last read."""
    port, worker, lock = await ports()
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    seen = worker.slots[1].seen
    lock.fail("get", error=LockDisconnected)

    await port._read_tracked()

    assert worker.slots[1].seen == seen == Holds("1234")
    assert worker.reachable


async def _until(condition, rounds: int = 500) -> None:
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never came")


async def test_a_silent_lock_backs_off_doubling_to_the_cap_then_starts_over(
    rig_factory,  # noqa: F811
) -> None:
    """
    Unanswered reads back the lock off only from the threshold, doubling to a cap.

    It is offline only once that many have gone unanswered; answering ends it
    all, and the next outage backs off from the minimum again.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    paced = rig.clock.sleep
    backoffs: list[tuple[float, bool]] = []

    async def _recorded(seconds: float) -> None:
        if seconds >= BACKOFF_MIN:
            backoffs.append((seconds, rig.worker.offline))
        await paced(seconds)

    rig.worker.sleep = _recorded
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER - 1):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert rig.worker.reachable

    with pytest.raises(LockError):
        await rig.worker.read([1])
    assert not rig.worker.reachable
    probes = OFFLINE_AFTER - BACKOFF_AFTER + 1
    await _until(lambda: len(backoffs) >= probes)

    expected = [min(BACKOFF_MIN * 2**n, BACKOFF_MAX) for n in range(probes)]
    assert [seconds for seconds, _ in backoffs[:probes]] == expected
    assert expected[-1] == BACKOFF_MAX
    assert [offline for _, offline in backoffs[:probes]] == [False] * (probes - 1) + [
        True
    ]

    lock.reads_unanswered = False
    await _until(lambda: rig.worker.reachable)
    assert not rig.worker.offline
    backoffs.clear()
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    await _until(lambda: bool(backoffs))

    assert backoffs[0] == (BACKOFF_MIN, False)
