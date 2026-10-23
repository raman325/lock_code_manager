"""The lock registry: one worker per lock, shared by entries that own its slots."""

import asyncio

import pytest

from custom_components.lock_code_manager.sync.registry import LockRegistry, SlotTaken
from custom_components.lock_code_manager.sync.slot import Holds, Repair, Seen, Status
from custom_components.lock_code_manager.sync.worker import LockWorker

from .test_worker import SALT, SETTLE_LIMIT, Clock, FakeLock


class Locks:
    """Fake locks by id, and every worker built for them."""

    def __init__(self, **locks: FakeLock) -> None:
        """Hold ``locks``; build workers on one fake clock."""
        self.locks = locks
        self.clock = Clock()
        self.built: list[tuple[str, LockWorker]] = []
        self.reads: list[tuple[int, ...]] = []
        for lock in locks.values():
            original = lock.read

            async def _read(slots, original=original):
                self.reads.append(tuple(slots))
                return await original(slots)

            lock.read = _read  # type: ignore[method-assign]

    def make(self, lock: str) -> LockWorker:
        """Build a worker for ``lock``."""
        worker = LockWorker(
            self.locks[lock], SALT, clock=self.clock, sleep=self.clock.sleep
        )
        self.built.append((lock, worker))
        return worker

    def running(self, lock: str) -> list[LockWorker]:
        """Return the workers for ``lock`` started and not yet stopped."""
        return [
            w
            for name, w in self.built
            if name == lock and w.started and not w._stopped.is_set()
        ]


@pytest.fixture
async def setup():
    """A registry on fake locks; everything stopped after the test."""
    registries: list[LockRegistry] = []

    def _build(**locks: FakeLock) -> tuple[LockRegistry, Locks]:
        fakes = Locks(**locks)
        registry = LockRegistry(fakes.make, grace=0.1)
        registries.append(registry)
        return registry, fakes

    yield _build
    for registry in registries:
        await registry.stop()


async def _settle(worker: LockWorker) -> None:
    async with asyncio.timeout(SETTLE_LIMIT):
        await worker.settled()
        # Let a stop the last job set off run.
        for _ in range(10):
            await asyncio.sleep(0)


# Sharing -----------------------------------------------------------------------


async def test_entries_on_one_lock_share_one_worker(setup) -> None:
    """Each entry's slots go through the same worker."""
    registry, locks = setup(front=FakeLock())
    first = await registry.hold("front", "a", {1: "1111"})
    second = await registry.hold("front", "b", {2: "2222"})
    await _settle(first)

    assert first is second
    assert len(locks.built) == 1
    assert locks.locks["front"].codes == {1: "1111", 2: "2222"}


async def test_a_new_worker_reads_every_slot_wanted_at_once(setup) -> None:
    """The first read takes all of the first holder's slots in one exchange."""
    registry, locks = setup(front=FakeLock({1: "1111", 2: "2222", 3: "3333"}))
    worker = await registry.hold("front", "a", {1: "1111", 2: "2222", 3: "3333"})
    await _settle(worker)

    assert locks.reads == [(1, 2, 3)]


async def test_a_slot_owned_by_another_entry_is_refused(setup) -> None:
    """Nothing changes: not the slot, not who holds the lock."""
    registry, locks = setup(front=FakeLock())
    worker = await registry.hold("front", "a", {1: "1111"})
    await _settle(worker)

    with pytest.raises(SlotTaken):
        await registry.hold("front", "b", {2: "2222", 1: "9999"})
    with pytest.raises(KeyError):
        registry.want("front", "b", 2, "2222")  # b never came to hold it
    await registry.hold("front", "c")
    with pytest.raises(SlotTaken):
        registry.want("front", "c", 1, "9999")
    await _settle(worker)

    assert worker.slots[1].want == "1111"
    assert 2 not in worker.slots
    assert locks.locks["front"].codes == {1: "1111"}


async def test_only_a_holder_sets_a_slot(setup) -> None:
    """An entry that does not hold the lock cannot want, give up or unclaim."""
    registry, _ = setup(front=FakeLock())
    await registry.hold("front", "a", {1: "1111"})

    for act in (
        lambda: registry.want("front", "b", 2, "2222"),
        lambda: registry.give_up("front", "b", 1),
        lambda: registry.unclaim("front", "b", 1),
        lambda: registry.want("back", "a", 1, "1111"),
    ):
        with pytest.raises(KeyError):
            act()


# Letting go --------------------------------------------------------------------


async def test_the_last_holder_out_stops_the_worker_and_leaves_the_codes(
    setup,
) -> None:
    """Unloading is not giving up: the codes stay on the lock."""
    registry, locks = setup(front=FakeLock())
    worker = await registry.hold("front", "a", {1: "1111"})
    await registry.hold("front", "b", {2: "2222"})
    await _settle(worker)

    await registry.release("front", "a")
    assert locks.running("front") == [worker]
    await registry.release("front", "b")

    assert locks.running("front") == []
    assert locks.locks["front"].codes == {1: "1111", 2: "2222"}


async def test_an_unclaimed_slot_is_left_alone(setup) -> None:
    """A slot no entry claims is never written."""
    registry, locks = setup(front=FakeLock({1: "1111"}))
    worker = await registry.hold("front", "a", {1: "1111"})
    await _settle(worker)
    registry.unclaim("front", "a", 1)
    locks.locks["front"].codes[1] = "0000"  # someone else's code now
    await worker.read([1])  # the integration polls
    await _settle(worker)

    assert locks.locks["front"].codes == {1: "0000"}
    assert locks.locks["front"].writes == []


async def test_an_entry_unloading_leaves_its_slots_to_the_lock(setup) -> None:
    """Its slots are no longer managed while another entry keeps the lock."""
    lock = FakeLock({1: "1111"})
    registry, _ = setup(front=lock)
    worker = await registry.hold("front", "a", {1: "1111"})
    await registry.hold("front", "b", {2: "2222"})
    await _settle(worker)
    await registry.release("front", "a")
    lock.codes[1] = "0000"
    await worker.read([1, 2])
    await _settle(worker)

    assert lock.codes == {1: "0000", 2: "2222"}


async def test_a_slot_given_up_is_cleared_even_after_its_owner_unloads(setup) -> None:
    """The worker runs on until the clear is done, then stops."""
    lock = FakeLock({1: "1111"})
    registry, locks = setup(front=lock)
    worker = await registry.hold("front", "a", {1: "1111"})
    await _settle(worker)
    lock.unreachable = True
    registry.give_up("front", "a", 1)
    await registry.release("front", "a")
    await _settle(worker)
    assert locks.running("front") == [worker]

    lock.unreachable = False
    async with asyncio.timeout(SETTLE_LIMIT):
        while locks.running("front"):
            await asyncio.sleep(0)

    assert lock.codes == {}


async def test_a_clear_held_does_not_keep_the_worker(setup) -> None:
    """A release clear the lock refuses is held: nothing left to wait for."""
    lock = FakeLock({1: "1111"}, refuse_clear={1})
    registry, locks = setup(front=lock)
    worker = await registry.hold("front", "a", {1: "1111"})
    await _settle(worker)
    registry.give_up("front", "a", 1)
    await _settle(worker)
    assert worker.status(1) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)

    await registry.release("front", "a")

    assert locks.running("front") == []


async def test_claiming_a_slot_being_cleared_replaces_the_clear(setup) -> None:
    """The new owner's PIN wins; the slot is its own from then on."""
    lock = FakeLock({1: "1111"})
    registry, _ = setup(front=lock)
    worker = await registry.hold("front", "a", {1: "1111"})
    await registry.hold("front", "b")
    await _settle(worker)
    lock.unreachable = True
    registry.give_up("front", "a", 1)
    registry.want("front", "b", 1, "2222")
    lock.unreachable = False
    worker.request_sync()
    async with asyncio.timeout(SETTLE_LIMIT):
        while worker.status(1)[0] is not Status.IN_SYNC or not worker.reachable:
            await asyncio.sleep(0)

    assert lock.codes == {1: "2222"}
    with pytest.raises(SlotTaken):
        registry.want("front", "a", 1, "1111")


async def test_a_lock_held_again_while_stopping_never_has_two_workers(setup) -> None:
    """The new hold waits for the old worker to stop, then starts a fresh one."""
    lock = FakeLock({1: "1111"})
    registry, locks = setup(front=lock)
    old = await registry.hold("front", "a", {1: "1111"})
    await _settle(old)
    gate = asyncio.Event()
    original = lock.read

    async def _slow(slots):
        await gate.wait()
        return await original(slots)

    lock.read = _slow  # type: ignore[method-assign]
    old.request_sync()
    reading = asyncio.ensure_future(old.read([1]))
    await asyncio.sleep(0)
    releasing = asyncio.ensure_future(registry.release("front", "a"))
    holding = asyncio.ensure_future(registry.hold("front", "b", {1: "1111"}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not holding.done()
    gate.set()
    lock.read = original  # type: ignore[method-assign]
    new = await holding
    await releasing
    await asyncio.gather(reading, return_exceptions=True)

    assert new is not old
    assert locks.running("front") == [new]


# One-off reads -----------------------------------------------------------------


async def test_a_read_with_no_worker_running_starts_and_stops_one(setup) -> None:
    """Config flows read a lock no entry holds yet."""
    registry, locks = setup(front=FakeLock({3: "3333"}))

    assert await registry.read_once("front", [3, 4]) == {
        3: Holds("3333"),
        4: Seen.EMPTY,
    }
    assert locks.running("front") == []


async def test_a_read_goes_through_the_running_worker(setup) -> None:
    """Never a second worker beside the one already talking to the lock."""
    registry, locks = setup(front=FakeLock({3: "3333"}))
    worker = await registry.hold("front", "a", {1: None})
    await _settle(worker)

    assert await registry.read_once("front", [3]) == {3: Holds("3333")}
    assert len(locks.built) == 1
    assert locks.running("front") == [worker]


async def test_stopping_stops_every_worker_clears_included(setup) -> None:
    """Home Assistant stopping ends everything, even a clear still going."""
    lock = FakeLock({1: "1111"}, unreachable=True)
    registry, locks = setup(front=lock, back=FakeLock())
    worker = await registry.hold("front", "a", {1: "1111"})
    await registry.hold("back", "a", {1: "1111"})
    registry.give_up("front", "a", 1)
    await registry.release("front", "a")
    await _settle(worker)

    await registry.stop()

    assert locks.running("front") == locks.running("back") == []


async def _until_stopped(locks: Locks, lock: str) -> None:
    async with asyncio.timeout(SETTLE_LIMIT):
        while locks.running(lock):
            await asyncio.sleep(0)


async def test_a_failing_change_callback_still_stops_the_worker(setup) -> None:
    """The worker shrugs off an integration callback that raises; so does stopping."""
    lock = FakeLock({1: "1111"})
    registry, locks = setup(front=lock)

    def _make(name: str) -> LockWorker:
        worker = locks.make(name)

        def _boom() -> None:
            raise RuntimeError("integration bug")

        worker.on_change = _boom
        return worker

    registry._make = _make
    worker = await registry.hold("front", "a", {1: "1111"})
    await _settle(worker)
    registry.give_up("front", "a", 1)
    await registry.release("front", "a")
    await _until_stopped(locks, "front")

    assert lock.codes == {}


async def test_a_hold_after_stopping_is_refused(setup) -> None:
    """Home Assistant stopped: nothing would ever stop a worker started now."""
    registry, locks = setup(front=FakeLock())
    await registry.stop()

    with pytest.raises(RuntimeError):
        await registry.hold("front", "a", {1: "1111"})
    assert locks.running("front") == []


async def test_a_hold_waiting_on_a_stop_when_stopping_starts_nothing(setup) -> None:
    """A hold queued behind a stopping worker does not outlive the registry."""
    lock = FakeLock({1: "1111"})
    registry, locks = setup(front=lock)
    old = await registry.hold("front", "a", {1: "1111"})
    await _settle(old)
    gate = asyncio.Event()
    original = lock.read

    async def _slow(slots):
        await gate.wait()
        return await original(slots)

    lock.read = _slow  # type: ignore[method-assign]
    reading = asyncio.ensure_future(old.read([1]))
    await asyncio.sleep(0)
    releasing = asyncio.ensure_future(registry.release("front", "a"))
    holding = asyncio.ensure_future(registry.hold("front", "b", {1: "1111"}))
    for _ in range(5):
        await asyncio.sleep(0)
    stopping = asyncio.ensure_future(registry.stop())
    gate.set()
    lock.read = original  # type: ignore[method-assign]
    await asyncio.gather(reading, releasing, stopping, return_exceptions=True)

    with pytest.raises(RuntimeError):
        await holding
    assert locks.running("front") == []


async def test_a_read_abandoned_midway_leaves_nothing(setup) -> None:
    """A config flow cancelled mid-read leaves no worker running."""
    lock = FakeLock({3: "3333"})
    registry, locks = setup(front=lock)
    gate = asyncio.Event()
    original = lock.read

    async def _slow(slots):
        await gate.wait()
        return await original(slots)

    lock.read = _slow  # type: ignore[method-assign]
    task = asyncio.ensure_future(registry.read_once("front", [3]))
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    gate.set()
    await asyncio.gather(task, return_exceptions=True)
    await _until_stopped(locks, "front")

    assert locks.running("front") == []


async def test_stopping_twice_beside_a_release_raises_nothing(setup) -> None:
    """Shutdown racing an entry's unload, and itself, ends cleanly."""
    registry, locks = setup(front=FakeLock(), back=FakeLock())
    worker = await registry.hold("front", "a", {1: "1111"})
    await registry.hold("back", "a", {1: "1111"})
    await _settle(worker)
    releasing = asyncio.ensure_future(registry.release("front", "a"))
    await asyncio.sleep(0)
    await asyncio.gather(registry.stop(), registry.stop(), releasing)

    assert locks.running("front") == locks.running("back") == []


async def test_a_cancelled_shutdown_does_not_cancel_an_unload(setup) -> None:
    """Shutdown's caller cancelled: an unload and a hold waiting on it are not."""
    lock = FakeLock({1: "1111"})
    registry, _ = setup(front=lock)
    old = await registry.hold("front", "a", {1: "1111"})
    await _settle(old)
    gate = asyncio.Event()
    original = lock.read

    async def _slow(slots):
        await gate.wait()
        return await original(slots)

    lock.read = _slow  # type: ignore[method-assign]
    reading = asyncio.ensure_future(old.read([1]))
    await asyncio.sleep(0)
    releasing = asyncio.ensure_future(registry.release("front", "a"))
    holding = asyncio.ensure_future(registry.hold("front", "b", {1: "1111"}))
    for _ in range(5):
        await asyncio.sleep(0)
    stopping = asyncio.ensure_future(registry.stop())
    for _ in range(5):
        await asyncio.sleep(0)
    stopping.cancel()
    gate.set()
    lock.read = original  # type: ignore[method-assign]
    await asyncio.gather(reading, stopping, return_exceptions=True)

    assert await releasing is None
    with pytest.raises(RuntimeError):
        await holding


# Preparing and cleaning up -------------------------------------------------------


class Hooks:
    """Record what the registry asks of the lock around its worker."""

    def __init__(self, locks: Locks) -> None:
        """Watch ``locks``' reads too, so their order is known."""
        self.calls: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.raises = False
        self._locks = locks

    async def prepare(self, lock: str, worker: LockWorker) -> None:
        """Prepare the lock, once the gate is open."""
        self.calls.append(f"prepare {lock}")
        await self.gate.wait()
        if self.raises:
            raise RuntimeError("setup broke")
        self.calls.append(f"prepared {lock}")

    async def cleanup(self, lock: str, worker: LockWorker) -> None:
        """Clean the lock up."""
        assert worker._stopped.is_set()
        self.calls.append(f"cleanup {lock}")


def _hooked(**fakes: FakeLock) -> tuple[LockRegistry, Locks, Hooks]:
    locks = Locks(**fakes)
    hooks = Hooks(locks)
    registry = LockRegistry(
        locks.make, grace=0.1, prepare=hooks.prepare, cleanup=hooks.cleanup
    )
    return registry, locks, hooks


async def test_a_lock_is_prepared_before_its_first_read() -> None:
    """The provider is set up before anything asks the lock."""
    registry, locks, hooks = _hooked(front=FakeLock())
    hooks.gate.clear()
    holding = asyncio.ensure_future(registry.hold("front", "a", {1: "1111"}))
    for _ in range(5):
        await asyncio.sleep(0)
    assert locks.reads == []

    hooks.gate.set()
    worker = await holding
    await _settle(worker)
    await registry.stop()

    assert locks.reads[0] == (1,)
    assert hooks.calls == ["prepare front", "prepared front", "cleanup front"]


async def test_entries_joining_while_it_is_prepared_share_the_first_read() -> None:
    """Every slot wanted before the worker starts is read in one exchange."""
    registry, locks, hooks = _hooked(front=FakeLock())
    hooks.gate.clear()
    first = asyncio.ensure_future(registry.hold("front", "a", {1: "1111"}))
    await asyncio.sleep(0)
    second = await registry.hold("front", "b", {2: "2222"})
    hooks.gate.set()
    worker = await first
    await _settle(worker)
    await registry.stop()

    assert worker is second
    assert locks.reads[0] == (1, 2)
    assert hooks.calls.count("prepare front") == 1


async def test_a_one_off_read_does_not_prepare_the_lock() -> None:
    """Setup renames codes on some locks: a config flow's read must not run it."""
    registry, _, hooks = _hooked(front=FakeLock({3: "3333"}))

    assert await registry.read_once("front", [3]) == {3: Holds("3333")}
    # Cleaned up all the same: reading can leave a subscription behind.
    assert hooks.calls == ["cleanup front"]


async def test_an_entry_joining_a_one_off_reads_worker_prepares_it() -> None:
    """The worker was started unprepared; the first entry to hold it prepares it."""
    lock = FakeLock({3: "3333"})
    registry, locks, hooks = _hooked(front=lock)
    gate = asyncio.Event()
    original = lock.read

    async def _slow(slots):
        await gate.wait()
        return await original(slots)

    lock.read = _slow  # type: ignore[method-assign]
    reading = asyncio.ensure_future(registry.read_once("front", [3]))
    await asyncio.sleep(0)
    await registry.hold("front", "a", {1: "1111"})
    gate.set()
    lock.read = original  # type: ignore[method-assign]
    await reading
    await registry.stop()

    assert hooks.calls == ["prepare front", "prepared front", "cleanup front"]
    assert len(locks.built) == 1


async def test_a_lock_that_fails_to_prepare_still_gets_its_worker() -> None:
    """Degraded, not dropped: the worker runs, and the failure is logged."""
    registry, locks, hooks = _hooked(front=FakeLock())
    hooks.raises = True
    worker = await registry.hold("front", "a", {1: "1111"})
    await _settle(worker)

    assert locks.running("front") == [worker]
    await registry.stop()
    assert hooks.calls[-1] == "cleanup front"


async def test_a_lock_retired_while_it_is_prepared_is_never_started() -> None:
    """Shutdown during setup: no worker starts after it, and cleanup waits."""
    registry, locks, hooks = _hooked(front=FakeLock())
    hooks.gate.clear()
    holding = asyncio.ensure_future(registry.hold("front", "a", {1: "1111"}))
    await asyncio.sleep(0)
    stopping = asyncio.ensure_future(registry.stop())
    await asyncio.sleep(0)
    hooks.gate.set()
    worker = await holding
    await stopping

    assert not worker.started
    assert locks.reads == []
    assert hooks.calls == ["prepare front", "prepared front", "cleanup front"]


async def test_a_new_hold_waits_for_the_old_locks_cleanup() -> None:
    """The provider is unloaded before a new one is set up for the same lock."""
    registry, _, hooks = _hooked(front=FakeLock())
    await registry.hold("front", "a", {1: "1111"})
    await registry.release("front", "a")
    await registry.hold("front", "b", {1: "1111"})
    await registry.stop()

    assert hooks.calls == [
        "prepare front",
        "prepared front",
        "cleanup front",
        "prepare front",
        "prepared front",
        "cleanup front",
    ]


async def test_the_caller_that_built_it_cancelled_still_starts_the_worker() -> None:
    """Another entry may hold it by then: it must not be left unstarted."""
    registry, locks, hooks = _hooked(front=FakeLock())
    hooks.gate.clear()
    first = asyncio.ensure_future(registry.hold("front", "a", {1: "1111"}))
    await asyncio.sleep(0)
    second = await registry.hold("front", "b", {2: "2222"})
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    hooks.gate.set()
    await _settle(second)

    assert second.started
    assert locks.running("front") == [second]
    await registry.stop()


# One-off writes --------------------------------------------------------------------


async def test_a_one_off_write_goes_on_after_the_call_and_then_stops() -> None:
    """An unmanaged code written from the card: ownerless, then the worker stops."""
    lock = FakeLock(unreachable=True)
    registry, locks, hooks = _hooked(front=lock)
    await registry.write_once("front", 5, "5555")
    [worker] = locks.running("front")
    await _settle(worker)
    assert locks.running("front") == [worker]  # the write is still owed

    lock.unreachable = False
    async with asyncio.timeout(SETTLE_LIMIT):
        while locks.running("front"):
            await asyncio.sleep(0)

    assert lock.codes == {5: "5555"}
    # A write needs the lock set up, unlike a read.
    assert hooks.calls[0] == "prepare front"


async def test_a_one_off_write_to_an_owned_slot_is_refused(setup) -> None:
    """Only a slot no entry owns can be written from outside."""
    registry, _ = setup(front=FakeLock())
    worker = await registry.hold("front", "a", {1: "1111"})

    with pytest.raises(SlotTaken):
        await registry.write_once("front", 1, "9999")
    assert worker.slots[1].want == "1111"


async def test_the_worker_in_service_is_the_one_held(setup) -> None:
    """Looked up by lock: the held worker, and none once it has stopped."""
    registry, _ = setup(front=FakeLock())
    worker = await registry.hold("front", "a", {1: "1111"})

    assert registry.worker("front") is worker
    assert registry.worker("back") is None
    await registry.release("front", "a")
    assert registry.worker("front") is None


async def test_a_one_off_write_asked_again_is_tried_again() -> None:
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


# Keys that are not slot numbers ------------------------------------------------


def _slot_of(key: tuple[int, str]) -> int:
    return key[0]


@pytest.fixture
async def keyed():
    """A registry whose workers key by (slot, kind), owning by slot."""
    registries: list[LockRegistry] = []

    def _build(**locks: FakeLock) -> tuple[LockRegistry, Locks]:
        fakes = Locks(**locks)
        registry = LockRegistry(fakes.make, grace=0.1, slot_of=_slot_of)
        registries.append(registry)
        return registry, fakes

    yield _build
    for registry in registries:
        await registry.stop()


async def test_an_entry_owns_every_kind_in_its_slot(keyed) -> None:
    """Another entry wanting another kind in an owned slot is refused."""
    registry, _ = keyed(front=FakeLock())
    await registry.hold("front", "a", {(3, "pin"): "1234"})
    await registry.hold("front", "b", {})

    with pytest.raises(SlotTaken) as taken:
        registry.want("front", "b", (3, "password"), "secret")

    assert (taken.value.slot, taken.value.owner) == (3, "a")
    assert registry.owner("front", 3) == "a"


async def test_giving_up_a_slot_releases_every_kind_in_it(keyed) -> None:
    """One give-up clears the slot's PIN and password, and leaves other slots."""
    lock = FakeLock()
    registry, _ = keyed(front=lock)
    worker = await registry.hold(
        "front",
        "a",
        {(3, "pin"): "1234", (3, "password"): "secret", (4, "pin"): "5678"},
    )
    await _settle(worker)
    assert lock.codes == {
        (3, "pin"): "1234",
        (3, "password"): "secret",
        (4, "pin"): "5678",
    }

    registry.give_up("front", "a", 3)
    await _settle(worker)

    assert lock.codes == {(4, "pin"): "5678"}
    assert registry.owner("front", 3) is None


async def test_unclaiming_a_slot_forgets_every_kind_in_it(keyed) -> None:
    """Letting go of a slot stops tracking all of it and leaves the lock as it is."""
    lock = FakeLock()
    registry, _ = keyed(front=lock)
    worker = await registry.hold(
        "front", "a", {(3, "pin"): "1234", (3, "password"): "secret"}
    )
    await _settle(worker)

    registry.unclaim("front", "a", 3)

    assert worker.slots == {}
    assert lock.codes == {(3, "pin"): "1234", (3, "password"): "secret"}
    assert registry.owner("front", 3) is None
