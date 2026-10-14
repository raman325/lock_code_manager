"""
Guards for behaviour the retired sync manager's tests covered and nothing else does.

Each scenario names the regression it keeps fixed. Core scenarios run on the
worker rig from ``test_worker.py``; the rest drive Home Assistant.
"""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.core.slot import Seen, Status

from ..common import (
    LOCK_1_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_set_slot_pin,
    in_sync_entity_id,
    lcm_issue_ids,
    mock_lock,
    slot_entity_id,
)
from .test_worker import FakeLock, _started, rig_factory  # noqa: F401


async def test_a_new_pin_on_a_slot_the_lock_hides_is_written_once(
    rig_factory,  # noqa: F811
) -> None:
    """
    #962: a lock that hides its codes still gets a changed PIN, once.

    The slot reads occupied whatever it holds. A code put there by someone
    else is written over once; a new PIN is written once; neither is
    written again.
    """
    lock = FakeLock({1: "1234"}, masked=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert lock.writes == [(1, "1234")]

    rig.worker.want(1, "4321")
    await rig.settle()
    for _ in range(3):
        await rig.pass_()

    assert lock.writes == [(1, "1234"), (1, "4321")]
    assert lock.codes == {1: "4321"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_code_deleted_at_the_lock_and_pushed_is_put_back(
    rig_factory,  # noqa: F811
) -> None:
    """#1538: an in-sync slot the lock says is now empty goes out of sync and is rewritten."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert lock.writes == []

    del lock.codes[1]
    rig.worker.push(1, Seen.EMPTY)
    await rig.settle()

    assert lock.writes == [(1, "1234")]
    assert lock.codes == {1: "1234"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_user_turned_off_with_no_pin_settles_in_sync(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """#1085: a disabled user saved with no PIN is in sync, not waiting for one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_ENABLED: False}},
        },
        unique_id="pr1085",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(in_sync_entity_id(hass, entry, 1)).state == STATE_ON
    assert 1 not in mock_lock(entry).codes
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_restart_keeps_the_repair_of_a_slot_turned_off_for_a_refused_pin(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    #1243: starting up fixes nothing, so a refused PIN's repair outlives it.

    The lock refused slot 1's PIN, which turned the slot off and raised the
    repair. After a reload the slot is off and the lock empty there -- in
    sync -- but nobody has answered the repair yet.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="Restart Repairs"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"slot_disabled_{entry.entry_id}_1"
    mock_lock(entry).refuse("set", 1, times=1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()
    enabled = slot_entity_id(hass, "switch", entry, 1, CONF_ENABLED)
    assert hass.states.get(enabled).state == STATE_OFF
    assert issue_id in lcm_issue_ids(hass, "slot_disabled")

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(in_sync_entity_id(hass, entry, 1)).state == STATE_ON
    assert 1 not in mock_lock(entry).codes
    assert issue_id in lcm_issue_ids(hass, "slot_disabled")
    assert await hass.config_entries.async_unload(entry.entry_id)
