"""
Open bugs, each pinned by a strict xfail at the seam.

Each test states the behavior the issue asks for, driven and checked only
through Home Assistant and the mock lock. While the bug is open the test
fails; a fix makes it pass, and the strict marker then fails the run until
the marker is removed.
"""

from __future__ import annotations

import itertools

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.providers._base import (
    MIN_OPERATION_DELAY,
    OPERATION_TIMEOUT,
)

from .common import (
    LOCK_1_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_advance_clock,
    async_advance_sync_ticks,
    async_set_slot_pin,
    async_tick_until,
    in_sync_entity_id,
    mock_lock,
)


def _lcm_issues(hass: HomeAssistant) -> list[str]:
    """Return the ids of every Lock Code Manager repair."""
    return [
        issue.issue_id
        for issue in ir.async_get(hass).issues.values()
        if issue.domain == DOMAIN
    ]


async def _async_setup_two_slots(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, *, paced: bool = False
) -> MockConfigEntry:
    """Set up an entry with slots 1 and 2 on lock 1, both in sync."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="Open bugs"
    )
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    # Paced, setup waits out the delay between its reads on the frozen clock.
    await async_advance_clock(hass, freezer, 10, settle=False)
    assert setup.done()
    assert setup.result()
    in_syncs = [in_sync_entity_id(hass, entry, slot) for slot in (1, 2)]
    await async_tick_until(
        hass,
        freezer,
        lambda: all(hass.states.get(e).state == STATE_ON for e in in_syncs),
        max_ticks=5,
        what="both slots in sync",
        settle=not paced,
    )
    return entry


@pytest.mark.xfail(
    strict=True,
    reason="#1622: a release clear the lock acknowledges but ignores is never checked",
)
async def test_a_deleted_users_code_does_not_stay_on_the_lock_unreported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    A clear the lock ignores, when a user is deleted, is retried or reported.

    Otherwise the deleted user's PIN keeps opening the door and nothing says so.
    """
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    lock.ignore("clear", 2)
    removed = get_entry_config(entry).name_for(2)
    subentry = next(s for s in entry.subentries.values() if s.title == removed)

    assert hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()
    await async_advance_sync_ticks(hass, freezer, 10)

    assert 2 not in lock.codes or _lcm_issues(hass)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="#1642: a refused write does not count toward the lock's pacing",
)
@pytest.mark.paced
async def test_the_operation_after_a_refused_write_is_paced(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """Every operation that reaches the lock, refused or not, is spaced from the next."""
    entry = await _async_setup_two_slots(hass, freezer, paced=True)
    lock = mock_lock(entry)
    await async_advance_sync_ticks(hass, freezer, 3, settle=False)
    lock.run_times.clear()
    lock.refuse("set", 1, times=1)

    await async_set_slot_pin(hass, entry, 1, "1111")
    await async_set_slot_pin(hass, entry, 2, "2222")
    await async_advance_sync_ticks(hass, freezer, 4, settle=False)

    gaps = [b - a for a, b in itertools.pairwise(lock.run_times)]
    # Unloaded before asserting: the unload waits out the pacing on the
    # frozen clock, which a failed test's teardown would never move.
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await async_advance_clock(hass, freezer, 10 * MIN_OPERATION_DELAY, settle=False)
    assert unload.done()
    assert len(gaps) >= 2
    assert min(gaps) >= MIN_OPERATION_DELAY


@pytest.mark.xfail(
    strict=True,
    reason="#1643: strikes against a PIN no longer wanted count against the new one",
)
async def test_a_pin_changed_while_a_write_is_pending_starts_with_no_strikes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """The new PIN's first failure does not suspend the slot."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"

    # Two strikes against 4321, then it lands, and the read confirming it fails.
    lock.fail("set", 1, times=2)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_sync_ticks(hass, freezer, 2)
    lock.fail("get", times=1)
    await async_advance_sync_ticks(hass, freezer)

    # The user changes the PIN while 4321 waits, and its first write fails.
    await async_set_slot_pin(hass, entry, 1, "5555")
    lock.fail("set", 1, times=1)
    await async_advance_sync_ticks(hass, freezer, 4)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert lock.codes[1] == "5555"
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="#1644: writes that hang every time never escalate while reads succeed",
)
async def test_a_write_that_hangs_every_time_is_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """A slot whose write never returns is suspended with a repair, not retried forever."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    wedge = lock.hang("set", 1)

    await async_set_slot_pin(hass, entry, 1, "4321")
    for _ in range(6):
        await async_advance_clock(hass, freezer, OPERATION_TIMEOUT, settle=False)

    issues = _lcm_issues(hass)
    wedge.clear()
    assert issues
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="#1644: writes failing as disconnected never escalate while reads succeed",
)
async def test_writes_failing_as_disconnected_are_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """A slot whose every write fails, while reads answer, is suspended with a repair."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    lock.fail("set", 1, error=LockDisconnected)

    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_sync_ticks(hass, freezer, 60)

    assert _lcm_issues(hass)
    lock.clear_faults()
    await hass.config_entries.async_unload(entry.entry_id)
