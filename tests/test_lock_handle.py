"""
One owner per physical lock: built once, torn down once, never two at once.

The first tests hold a write at lock 1, bring a second reader to the lock,
and count what reached it while the write was held -- on the held instance
and on any other built meanwhile. The rest follow a lock instance through
teardowns that go wrong: a setup that fails, a teardown that raises, and a
holder that is deleted without letting go.
"""

from __future__ import annotations

from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import ATTR_IN_SYNC, DOMAIN
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.lock_handle import CLOSE_WAIT_SECONDS
from custom_components.lock_code_manager.providers._base import MIN_OPERATION_DELAY

from .common import (
    BASE_CONFIG,
    CONFIG_ENTRY_VERSION,
    LOCK_1_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    LockFault,
    MockLCMLock,
    async_advance_clock,
    async_run_until_quiet,
    async_set_slot_pin,
    async_step_clock_until,
    async_tick_until,
    current_entry,
    in_sync_entity_id,
    mock_lock,
    when_mock_lock_built,
)


def _lock_io(lock: MockLCMLock) -> int:
    """Count the operations that reached ``lock``: everything but its lifecycle."""
    return sum(call not in ("setup", "unload") for call in lock.call_order)


async def _async_hold_a_write(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> tuple[MockConfigEntry, MockLCMLock, LockFault]:
    """Set up an entry on lock 1 with a PIN change on its way, held at the lock."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="Holding lock 1"
    )
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    await async_step_clock_until(hass, freezer, setup.done, 100, "first setup")
    if not setup.result():
        pytest.fail("setup: the first entry did not set up")
    in_syncs = [in_sync_entity_id(hass, entry, slot) for slot in (1, 2)]
    await async_tick_until(
        hass,
        freezer,
        lambda: all(hass.states.get(e).state == STATE_ON for e in in_syncs),
        max_ticks=5,
        what="both slots in sync",
    )
    lock = mock_lock(entry)
    held = lock.hang("set", 1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, MIN_OPERATION_DELAY, settle=False)
    await held.async_wait_held()
    return entry, lock, held


async def _async_reach_lock_1_while_held(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lock: MockLCMLock,
    held: LockFault,
    entry: MockConfigEntry,
) -> int:
    """
    Set up ``entry`` beside the held write; return what reached lock 1 meanwhile.

    Counts the held lock's own operations from now on, and every lock 1 a
    provider builds along the way, so neither joining the lock nor building
    a new one hides a call. Setup may finish or wait its turn; once the
    count is taken the write is let go and setup must succeed.
    """
    before = _lock_io(lock)
    entry.add_to_hass(hass)
    with when_mock_lock_built(lambda built: built) as built:
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        for _ in range(5):
            await async_advance_clock(hass, freezer, 2, settle=False)
        if not held.held:
            pytest.fail("setup: the write is no longer held")
        reached = (_lock_io(lock) - before) + sum(
            _lock_io(other)
            for other in built
            if other.lock.entity_id == LOCK_1_ENTITY_ID
        )
        held.clear()
        await async_step_clock_until(hass, freezer, setup.done, 100, "the setup")
    if not setup.result():
        pytest.fail(f"setup: {entry.title} did not set up")
    return reached


async def _async_unload_all(hass: HomeAssistant) -> None:
    """Unload every entry, failing the test on one that will not unload."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and not (
            await hass.config_entries.async_unload(entry.entry_id)
        ):
            pytest.fail(f"teardown: {entry.title} did not unload")


async def test_an_entry_set_up_beside_one_unloading_waits_for_its_write(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    A second entry on a lock waits its turn while the first one unloads.

    The first entry is unloading with a write still at the lock: its stop
    grace lets the write finish. An entry setting up meanwhile used to build
    its own instance and read the lock while the write was in flight -- two
    at once on one lock, which the turn exists to prevent (#139). The lock's
    handle stays registered until its close has finished, and an entry
    acquiring the lock waits for that.
    """
    first, lock, held = await _async_hold_a_write(hass, freezer)
    unload = hass.async_create_task(hass.config_entries.async_unload(first.entry_id))
    await async_run_until_quiet()
    if first.state is not ConfigEntryState.UNLOAD_IN_PROGRESS:
        pytest.fail(f"setup: the first entry is {first.state}, not unloading")

    # Well inside the first entry's stop grace.
    reached = await _async_reach_lock_1_while_held(
        hass, freezer, lock, held, current_entry("Beside the unload", {3: "3333"})
    )
    await async_step_clock_until(hass, freezer, unload.done, 100, "first unload")
    await _async_unload_all(hass)
    assert reached == 0


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "open: the migration's unmanaged-code sweep reads a lock past the "
        "write in flight"
    ),
)
async def test_the_unmanaged_code_sweep_waits_for_a_write_in_flight(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    An entry migrating beside a busy lock reads it in that lock's turn.

    Migrating from version 3 or older sweeps each of the entry's locks for
    codes nothing manages, through a throwaway provider of its own. That
    read skips the turn the entry already holding the lock is using, so it
    reaches the lock while that entry's write is still in flight.
    """
    _first, lock, held = await _async_hold_a_write(hass, freezer)
    migrating = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Legacy")

    reached = await _async_reach_lock_1_while_held(hass, freezer, lock, held, migrating)
    if migrating.version != CONFIG_ENTRY_VERSION:
        pytest.fail(f"setup: the entry migrated to {migrating.version} only")
    await _async_unload_all(hass)
    assert reached == 0


async def test_a_lock_is_built_anew_only_once_its_old_instance_is_torn_down(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    An entry acquiring a lock whose instance is still being torn down waits.

    The last entry holding lock 1 unloads, and its provider's teardown takes
    a while. An entry setting up on lock 1 meanwhile builds nothing until
    that teardown has finished -- providers do work there a new instance
    depends on, like the virtual lock writing its store back -- and then
    gets an instance of its own, not the one torn down.
    """
    first = current_entry("Leaving", {1: "1234"})
    first.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(first.entry_id))
    await async_step_clock_until(hass, freezer, setup.done, 100, "first setup")
    if not setup.result():
        pytest.fail("setup: the first entry did not set up")
    old = mock_lock(first)
    teardown = old.hang("unload")
    unload = hass.async_create_task(hass.config_entries.async_unload(first.entry_id))
    await teardown.async_wait_held()

    second = current_entry("Arriving", {3: "3333"})
    second.add_to_hass(hass)
    with when_mock_lock_built(lambda built: built) as built:
        setup = hass.async_create_task(hass.config_entries.async_setup(second.entry_id))
        for _ in range(5):
            await async_advance_clock(hass, freezer, 2, settle=False)
        if not teardown.held:
            pytest.fail("setup: the old instance's teardown is no longer held")
        early = [lock for lock in built if lock.call_order]

        teardown.clear()
        await async_step_clock_until(hass, freezer, unload.done, 100, "first unload")
        await async_step_clock_until(hass, freezer, setup.done, 100, "second setup")
        if not setup.result():
            pytest.fail("setup: the second entry did not set up")
        in_sync = in_sync_entity_id(hass, second, 3)
        await async_tick_until(
            hass,
            freezer,
            lambda: hass.states.get(in_sync).state == STATE_ON,
            max_ticks=5,
            what="the second entry's slot in sync",
        )
    set_up = [lock for lock in built if lock.service_calls["setup"]]
    await _async_unload_all(hass)

    assert early == []
    assert set_up and set_up[0] is not old


async def _async_set_up_on_lock_1(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    slot: int,
) -> None:
    """
    Set ``entry`` up and bring its slot on lock 1 into sync, well inside the wait.

    The clock moves less than ``CLOSE_WAIT_SECONDS`` in all, so an entry that
    waited out a close that never finished would not get there. The slot's
    in-sync entity comes from the entry's first pass, which can still be
    running once setup returns, so it is looked up as it appears.
    """
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    ent_reg = er.async_get(hass)
    unique_id = build_slot_unique_id(
        entry.entry_id, slot, ATTR_IN_SYNC, LOCK_1_ENTITY_ID
    )

    def _in_sync() -> bool:
        entity_id = ent_reg.async_get_entity_id(BINARY_SENSOR_DOMAIN, DOMAIN, unique_id)
        return (
            entity_id is not None
            and (state := hass.states.get(entity_id)) is not None
            and state.state == STATE_ON
        )

    await async_step_clock_until(
        hass,
        freezer,
        lambda: setup.done() and (not setup.result() or _in_sync()),
        int(CLOSE_WAIT_SECONDS) - 5,
        f"{entry.title} set up and in sync",
        step=1.0,
    )
    if not setup.result():
        pytest.fail(f"setup: {entry.title} did not set up")


async def test_a_lock_whose_setup_failed_is_built_anew_for_the_next_entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    A lock dropped when its setup failed leaves nothing for the next entry to wait on.

    The first entry's lock claims push updates its provider never
    implemented, which fails setup after its coordinator is built, and the
    entry drops it. An entry setting up on the lock later builds and sets up
    an instance of its own straight away.
    """
    failing = current_entry("Cannot subscribe", {1: "1234"})
    failing.add_to_hass(hass)
    with (
        patch.object(MockLCMLock, "supports_push", property(lambda self: True)),
        when_mock_lock_built(lambda built: built) as first_built,
    ):
        setup = hass.async_create_task(
            hass.config_entries.async_setup(failing.entry_id)
        )
        await async_step_clock_until(hass, freezer, setup.done, 100, "failing setup")
    dropped = [lock for lock in first_built if lock.service_calls["setup"]]
    if len(dropped) != 1 or LOCK_1_ENTITY_ID in failing.runtime_data.locks:
        pytest.fail("setup: the first entry did not drop the lock it set up")

    with when_mock_lock_built(lambda built: built) as built:
        await _async_set_up_on_lock_1(
            hass, freezer, current_entry("Next", {3: "3333"}), 3
        )
    set_up = [lock for lock in built if lock.service_calls["setup"]]
    await _async_unload_all(hass)

    assert len(set_up) == 1
    assert set_up[0] is not dropped[0]


async def test_a_teardown_that_raises_still_lets_the_next_entry_in(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    An entry acquiring a lock does not wait on a teardown that already failed.

    The last entry holding lock 1 unloads, and its provider's teardown
    raises. The lock's instance is gone either way, so an entry setting up on
    the lock next builds its own straight away.
    """
    first = current_entry("Teardown fails", {1: "1234"})
    await _async_set_up_on_lock_1(hass, freezer, first, 1)
    mock_lock(first).fail("unload")
    await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    if first.state is not ConfigEntryState.FAILED_UNLOAD:
        pytest.fail(f"setup: the failing teardown left the entry {first.state}")

    with when_mock_lock_built(lambda built: built) as built:
        await _async_set_up_on_lock_1(
            hass, freezer, current_entry("Next", {3: "3333"}), 3
        )
    set_up = [lock for lock in built if lock.service_calls["setup"]]
    await _async_unload_all(hass)

    assert len(set_up) == 1


async def test_a_holder_deleted_without_letting_go_does_not_keep_the_lock_alive(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    The last remaining holder of a lock tears it down, even after a stuck one is deleted.

    Two entries share lock 1. The first fails to unload its platforms, so it
    is left FAILED_UNLOAD still holding the lock, and is then deleted, which
    Home Assistant does without unloading it again. When the second entry
    unloads, no existing entry holds the lock any more, so it is torn down.
    """
    stuck = current_entry("Stuck", {1: "1234"})
    staying = current_entry("Staying", {3: "3333"})
    await _async_set_up_on_lock_1(hass, freezer, stuck, 1)
    await _async_set_up_on_lock_1(hass, freezer, staying, 3)
    lock = mock_lock(staying)
    if mock_lock(stuck) is not lock:
        pytest.fail("setup: the two entries do not share lock 1")

    original = hass.config_entries.async_unload_platforms

    async def _fail_for_stuck(entry, platforms):
        if entry.entry_id == stuck.entry_id:
            return False
        return await original(entry, platforms)

    with patch.object(
        hass.config_entries, "async_unload_platforms", side_effect=_fail_for_stuck
    ):
        await hass.config_entries.async_unload(stuck.entry_id)
        await hass.async_block_till_done()
    if stuck.state is not ConfigEntryState.FAILED_UNLOAD:
        pytest.fail(f"setup: the stuck entry is {stuck.state}, not FAILED_UNLOAD")
    await hass.config_entries.async_remove(stuck.entry_id)
    await hass.async_block_till_done()

    unloaded = await hass.config_entries.async_unload(staying.entry_id)
    await hass.async_block_till_done()
    if not unloaded:
        pytest.fail("teardown: the staying entry did not unload")

    assert lock.service_calls["unload"]
