"""Round 1a adversarial review of PR #1683: regression tests."""

from __future__ import annotations

from custom_components.lock_code_manager.core.slot import Repair, Status

from .test_worker import FakeLock, rig_factory  # noqa: F401


async def test_a_release_after_unanswered_pin_writes_reaches_the_lock(
    rig_factory,  # noqa: F811
) -> None:
    """
    ``released`` from an earlier release must not excuse a later one.

    A release is answered but the code stays; the slot is claimed again and
    its PIN writes go unanswered (they may have landed: a native-user lock
    may have made the user); the slot is given up again and reads empty.
    The new release must still be sent.
    """
    lock = FakeLock({5: "1111"})
    rig = rig_factory(lock)
    rig.worker.want(5, "1111")
    rig.worker.start()
    await rig.settle()
    assert rig.status(5) == (Status.IN_SYNC, None)

    lock.ignore_writes = True
    rig.worker.want(5, None, release=True)
    await rig.settle()
    assert rig.status(5) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    assert rig.worker.slots[5].released

    # Claimed again; the code is gone from the keypad, and the PIN writes go
    # unanswered.
    lock.ignore_writes = False
    lock.writes_unanswered = True
    del lock.codes[5]
    rig.worker.want(5, "2222")
    await rig.settle()
    assert (5, "2222") in lock.writes

    lock.writes_unanswered = False
    writes = len(lock.writes)
    rig.worker.want(5, None, release=True)
    await rig.settle()
    assert (5, None) in lock.writes[writes:]
