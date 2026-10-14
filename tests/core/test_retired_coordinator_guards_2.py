"""Guards for behaviour the retired coordinator's tests (part 2) pinned."""

from __future__ import annotations

from custom_components.lock_code_manager.core.slot import Status

from .test_worker import FakeLock, _started, rig_factory  # noqa: F401


async def test_a_write_is_read_back_by_asking_only_about_its_slot(
    rig_factory,  # noqa: F811
) -> None:
    """
    The read-back after a write asks about the slot written, not the whole lock.

    On a lock that drops half its answers, reading one slot back lands about
    half the time and a walk of every slot almost never (#1549).
    """
    lock = FakeLock({1: "1111", 2: "2222", 3: "3333"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222", 3: "3333"})
    reads: list[tuple[int, ...]] = []
    original = lock.read

    async def _note(slots):
        reads.append(tuple(slots))
        return await original(slots)

    lock.read = _note  # type: ignore[method-assign]
    rig.worker.want(2, "9999")
    await rig.settle()

    assert lock.writes == [(2, "9999")]
    assert reads == [(2,)]
    assert rig.status(2) == (Status.IN_SYNC, None)
