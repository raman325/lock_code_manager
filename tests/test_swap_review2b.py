"""Adversarial review (round 2b) of the core swap: the integration's use of the core."""

from __future__ import annotations

import time

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
)
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    SERVICE_HARD_REFRESH_USERCODES,
)
from custom_components.lock_code_manager.domain.lock_runtime import (
    lock_codes,
    lock_registry,
)
from custom_components.lock_code_manager.domain.unmanaged import unmanaged_issue_id
from custom_components.lock_code_manager.repairs import async_create_fix_flow

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    in_sync_entity_id,
    lcm_issue_ids,
    mock_lock,
    sync_status,
    when_mock_lock_built,
)

CONDITION = "input_boolean.r2b_access"


async def _entry(
    hass: HomeAssistant, data: dict = TWO_SLOTS_ON_LOCK_1, unique_id: str = "r2b"
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_condition_change_shows_the_slot_syncing(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The calendar/condition turning a slot off queues a clear: the sensor says syncing.

    The old sensor showed ``syncing`` from the moment a sync was due. Now the
    worker reports a change only when a job finishes, and a condition change
    writes no entity state, so the sensor keeps saying in_sync for the whole
    of a slow clear.
    """
    hass.states.async_set(CONDITION, "on")
    entry = await _entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {
                    CONF_NAME: "test1",
                    CONF_PIN: "1234",
                    CONF_ENABLED: True,
                    CONF_CONDITION: CONDITION,
                },
            },
        },
    )
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    assert sync_status(hass, in_sync) == "in_sync"

    fault = lock.hang("clear", 1)
    hass.states.async_set(CONDITION, "off")
    await fault.async_wait_held()

    assert sync_status(hass, in_sync) == "syncing"

    fault.clear()
    await hass.async_block_till_done()
    assert 1 not in lock.codes
    assert sync_status(hass, in_sync) == "in_sync"


async def test_a_code_removed_at_the_keypad_leaves_the_lock_card(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An unmanaged code deleted at the lock disappears from what the card shows.

    The old coordinator replaced its data with each read; ``port.others`` is
    only ever added to, so the deleted code is shown for good.
    """
    entry = await _entry(hass)
    lock = mock_lock(entry)
    lock.codes[7] = "4242"
    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert lock_codes(hass, LOCK_1_ENTITY_ID)[7].pin == "4242"

    del lock.codes[7]  # deleted at the keypad
    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    await hass.async_block_till_done()

    credential = lock_codes(hass, LOCK_1_ENTITY_ID).get(7)
    assert credential is None or not credential.pin


async def test_clearing_an_unmanaged_code_on_a_lock_no_entry_holds(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The lock left every entry after the sweep: the repair says so, at once.

    The old flow refused with "not managed". Now the clear starts a worker
    for a provider nobody sets up: every write is unsent, the flow waits a
    minute and then reports a failed clear "after two tries" that never
    reached the lock -- and a worker is left running for the lock.
    """
    await _entry(hass)  # holds lock 1 only
    flow = await async_create_fix_flow(
        hass,
        unmanaged_issue_id(LOCK_2_ENTITY_ID, 2),
        {"lock_entity_id": LOCK_2_ENTITY_ID, "slot": 2},
    )
    flow.hass = hass
    started = time.monotonic()
    result = await flow.async_step_clear()
    await hass.async_block_till_done()

    assert time.monotonic() - started < 10
    assert result["type"] == "abort"
    assert "two tries" not in result["description_placeholders"].get("error", "")
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None


async def test_acknowledging_a_held_clear_on_a_lock_that_left_every_entry(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Decision F: the acknowledge clears it with a worker started for the purpose.

    The lock was taken out of the entry, and its release clear was refused.
    Acknowledging the repair starts a worker -- but nothing sets its provider
    up (no entry lists the lock), so the clear is never sent: the slot is held
    again and the same "did not clear after two tries" repair comes back.
    """
    two_locks = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        CONF_SLOTS: TWO_SLOTS_ON_LOCK_1[CONF_SLOTS],
    }
    entry = await _entry(hass, two_locks)
    lock_2 = mock_lock(entry, LOCK_2_ENTITY_ID)
    lock_2.refuse("clear", 1)
    hass.config_entries.async_update_entry(
        entry, options={**two_locks, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_2_ENTITY_ID}_1"
    assert issue_id in lcm_issue_ids(hass, "slot_suspended")
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None

    with when_mock_lock_built(lambda lock: lock) as built:
        flow = await async_create_fix_flow(
            hass, issue_id, {"lock_entity_id": LOCK_2_ENTITY_ID, "slot": 1}
        )
        flow.hass = hass
        await flow.async_step_init({})
        await hass.async_block_till_done()

    assert built, "a worker was started for the clear"
    assert 1 not in built[0].codes
    assert built[0].service_calls["clear_usercode"] or built[0].service_calls.get(
        "release_managed_slot"
    )
    assert issue_id not in lcm_issue_ids(hass, "slot_suspended")
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None


async def test_the_hard_refresh_service_walks_the_whole_lock(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    "Query the lock for all usercodes": the service asks the provider for a hard refresh.

    The old service called the provider's unscoped ``async_hard_refresh_codes``
    -- for a cloud or Zigbee lock the only call that bypasses its cache, for
    Z-Wave the only one that re-reads slots nobody manages. Now it is a read
    of the managed slots through ``async_get_usercodes``, so a code added at
    the keypad of a caching provider is never fetched.
    """
    entry = await _entry(hass)
    lock = mock_lock(entry)
    lock.service_calls["hard_refresh_codes"].clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert (None,) in lock.service_calls["hard_refresh_codes"]
