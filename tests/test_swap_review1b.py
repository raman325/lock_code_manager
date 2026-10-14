"""Adversarial review (round 1b) of the core swap: the integration's use of the core."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager.const import (
    DOMAIN,
    SERVICE_HARD_REFRESH_USERCODES,
)
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.lock_runtime import lock_registry
from custom_components.lock_code_manager.domain.queries import get_entry_config

from .common import (
    LOCK_1_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_advance_clock,
    async_set_slot_pin,
    lcm_issue_ids,
    mock_lock,
    when_mock_lock_built,
)


async def _entry(
    hass: HomeAssistant, data: dict = TWO_SLOTS_ON_LOCK_1, unique_id: str = "r1b"
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _remove_user_in_slot(
    hass: HomeAssistant, entry: MockConfigEntry, slot: int
) -> None:
    """Delete the user holding ``slot``, the way the editor does."""
    name = get_entry_config(entry).name_for(slot)
    subentry = next(s for s in entry.subentries.values() if s.title == name)
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()


async def test_a_suspended_repair_from_before_a_restart_goes_once_in_sync(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A persistent slot_suspended issue left by the last run is resolved.

    The old sync manager deleted the issue by id whenever the slot was in
    sync; the new reconcile only deletes issues it raised in this run.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="r1b")
    entry.add_to_hass(hass)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="slot_suspended",
        translation_placeholders={
            "lock_entity_id": LOCK_1_ENTITY_ID,
            "lock_name": LOCK_1_ENTITY_ID,
            "slot_num": "1",
            "reason": "x",
        },
        data={"lock_entity_id": LOCK_1_ENTITY_ID, "slot": 1},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert mock_lock(entry).codes[1] == "1234"
    assert issue_id not in lcm_issue_ids(hass, "slot_suspended")


async def test_a_held_release_clear_keeps_its_repair_across_a_reload(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The code may still open the door: reloading the entry must not drop the repair.

    Decision F: the slot_clear_failed issue stays once no entry holds the lock.
    """
    entry = await _entry(hass)
    lock = mock_lock(entry)
    lock.refuse("clear", 1)
    await _remove_user_in_slot(hass, entry, 1)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    assert issue_id in lcm_issue_ids(hass, "slot_suspended")
    assert lock.codes[1] == "1234"

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert issue_id in lcm_issue_ids(hass, "slot_suspended")


async def test_an_entry_setting_up_during_a_one_off_read_is_synced_at_once(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A config flow's occupancy read is in flight when an entry takes the lock.

    The entry joins the read's worker, which is already started, so nothing
    waits for the provider's setup: the worker's reads go unanswered until
    it is set up, it backs off, and nothing wakes it once setup is done --
    the entry's codes wait out the backoff (60 s or more).
    """
    entry = await _entry(hass)  # migrated: the next setup does no sweep
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    faults = []

    def _act(lock):
        if not faults:
            lock.codes.clear()
            faults.append(lock.hang("get", times=1))
            faults.append(lock.hang("setup", times=1))
        return lock

    with when_mock_lock_built(_act) as built:
        read = hass.async_create_task(
            lock_registry(hass).read_once(LOCK_1_ENTITY_ID, [5])
        )
        await faults[0].async_wait_held()
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        await faults[1].async_wait_held()
        faults[0].clear()
        await read
        faults[1].clear()
        await setup
        await hass.async_block_till_done()

    assert built[0].codes == {1: "1234", 2: "5678"}


async def test_a_hard_refresh_asks_a_lock_that_is_backing_off(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """
    The lock is back; the user asks Lock Code Manager to look again.

    The old service read the lock whatever had happened before. Through a
    worker backing off, the read fails at once, unsent, and the slot waits
    out the backoff (up to 30 minutes).
    """
    entry = await _entry(hass)
    lock = mock_lock(entry)
    outage = lock.fail(None, error=LockDisconnected)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, 10 * 60)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None
    assert not worker.reachable
    outage.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert worker.reachable
    assert lock.codes[1] == "4321"
