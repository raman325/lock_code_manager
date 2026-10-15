"""The core wired into Home Assistant: the lock registry and what it raises."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_TURN_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.core.slot import Repair, Status
from custom_components.lock_code_manager.domain.exceptions import (
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationUnsupported,
)
from custom_components.lock_code_manager.domain.lock_runtime import (
    async_give_up,
    async_resume_slot,
    async_want,
    lock_registry,
    port_of,
)

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_advance_clock,
    async_set_slot_pin,
    current_entry,
    lcm_issue_ids,
    lock_workers,
    mock_lock,
    slot_entity_id,
    when_mock_lock_built,
)


async def _entry(
    hass: HomeAssistant, data: dict = TWO_SLOTS_ON_LOCK_1, unique_id: str = "rt"
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_home_assistant_stopping_stops_every_lock_worker(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Nothing is left talking to a lock once Home Assistant stops."""
    await _entry(hass)
    workers = lock_workers(hass)
    assert workers

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    assert all(worker._stopped.is_set() for worker in workers)


async def test_a_one_off_read_does_not_unload_the_provider_it_borrowed(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Unloading would write a virtual lock's store back, erasing its codes."""
    with when_mock_lock_built(lambda lock: lock) as built:
        await lock_registry(hass).read_once(LOCK_2_ENTITY_ID, [1])
        await hass.async_block_till_done()

    assert built
    assert all(not lock.service_calls["unload"] for lock in built)


async def test_an_entry_overlapping_a_slot_still_gets_the_lock(
    hass: HomeAssistant, mock_lock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """The overlapping slot stays the first entry's; the rest are managed."""
    await _entry(hass)
    second = await _entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                2: {CONF_NAME: "other2", CONF_PIN: "2222", CONF_ENABLED: True},
                3: {CONF_NAME: "other3", CONF_PIN: "3333", CONF_ENABLED: True},
            },
        },
        unique_id="rt2",
    )

    assert LOCK_1_ENTITY_ID in second.runtime_data.locks
    assert mock_lock(second).codes[3] == "3333"
    assert mock_lock(second).codes[2] == "5678"  # still the first entry's
    assert "managed by another entry" in caplog.text


async def test_a_slot_given_up_then_wanted_again_is_cleared_plainly(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Wanted again, the slot is no longer on its way out: no release."""
    entry = await _entry(hass)
    lock = mock_lock(entry)
    lock.fail(None, error=LockDisconnected)  # the clear cannot go yet
    async_give_up(hass, entry, LOCK_1_ENTITY_ID, 1)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None
    assert 1 in port_of(worker).releasing

    async_want(hass, entry, LOCK_1_ENTITY_ID, 1, "1234")

    assert 1 not in port_of(worker).releasing


async def test_a_lock_that_stops_answering_raises_its_offline_repair(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """Answered before, silent since: the user is told, once it has backed off."""
    entry = await _entry(hass)
    mock_lock(entry).fail(None, error=LockDisconnected)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, 6 * 60 * 60)

    assert lcm_issue_ids(hass, "lock_offline")


async def test_a_refused_pin_s_repair_goes_once_the_slot_is_back_in_sync(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Re-enabled with a PIN the lock takes: the refusal is answered."""
    entry = await _entry(hass)
    lock = mock_lock(entry)
    refusal = lock.refuse("set", 1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()
    assert lcm_issue_ids(hass, "slot_disabled")

    refusal.clear()
    await hass.services.async_call(
        "switch",
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: slot_entity_id(hass, "switch", entry, 1, CONF_ENABLED)},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert lock.codes[1] == "4321"
    assert not lcm_issue_ids(hass, "slot_disabled")


async def test_a_held_clear_is_tried_again_after_its_entry_has_gone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Acknowledging the repair starts the clear over, with no worker left."""
    entry = await _entry(hass)
    lock = mock_lock(entry)
    refusal = lock.refuse("clear", 1)
    async_give_up(hass, entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None
    assert worker.status(1) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is None

    refusal.clear()
    with when_mock_lock_built(
        lambda new: (new.codes.update(lock.codes), new)[1]
    ) as built:
        await async_resume_slot(hass, LOCK_1_ENTITY_ID, 1)
        await hass.async_block_till_done()

    assert built
    assert 1 not in built[0].codes


async def test_a_held_release_tried_again_without_its_entry_is_still_a_release(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A lock with users deletes the one the slot anchored, as the first try would."""
    entry = await _entry(hass)
    lock = mock_lock(entry)
    refusal = lock.refuse("clear", 1)
    async_give_up(hass, entry, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    refusal.clear()
    with when_mock_lock_built(lambda new: new) as built:
        await async_resume_slot(hass, LOCK_1_ENTITY_ID, 1)
        worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
        assert worker is not None
        assert 1 in port_of(worker).releasing
        await hass.async_block_till_done()

    assert built


LINK_HEALTH = "Radio link: 126 of 256 commands went unanswered."


def _hold_reason(hass: HomeAssistant, entry: MockConfigEntry, slot: int) -> str:
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_{slot}"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    return issue.translation_placeholders["reason"]


async def test_a_driver_that_will_not_set_a_code_is_quoted_in_the_repair(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The driver's own explanation reaches the repair, not a generic one (#1398)."""
    entry = await _entry(hass)
    mock_lock(entry).fail(
        "set",
        1,
        error=LockOperationUnsupported(
            "set credential slot 1 failed: zwave_error: Z-Wave error 322"
        ),
    )
    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()

    assert "Z-Wave error 322" in _hold_reason(hass, entry, 1)


async def test_what_the_provider_measures_of_its_link_is_quoted_in_the_repair(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A provider that can measure its transport is quoted, after the reason (#1397)."""
    entry = await _entry(hass)
    lock = mock_lock(entry)
    lock.ignore("set", 1)
    with patch.object(lock, "describe_link_health", return_value=LINK_HEALTH):
        await async_set_slot_pin(hass, entry, 1, "4321")
        await hass.async_block_till_done()

    reason = _hold_reason(hass, entry, 1)
    assert reason.startswith("the lock did not take the code after several tries")
    assert reason.endswith(LINK_HEALTH)


async def test_a_provider_that_cannot_measure_its_link_leaves_the_reason_alone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Nothing is added when the provider has nothing to say of its transport."""
    entry = await _entry(hass)
    mock_lock(entry).ignore("set", 1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()

    assert _hold_reason(hass, entry, 1) == (
        "the lock did not take the code after several tries"
    )


async def test_a_user_removed_while_setup_fails_validation_is_cleared_later(
    hass: HomeAssistant, freezer, mock_lock_config_entry
) -> None:
    """
    A user deleted while the lock fails its setup check leaves the lock later.

    A failed check other than "no PIN support" (a slot count unknown until a
    re-interview) can recover, and the lock still holds the user's PIN. Giving
    the slot up unclaimed then left that PIN opening the door once it did.
    """
    entry = current_entry("Failing validation", {1: "1234", 2: "5678"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(
        lambda lock: lock.fail(
            "setup", error=LockCodeManagerProviderError("no slot count")
        )
    ) as faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        lock = mock_lock(entry)
        assert not lock.provider_setup_succeeded
        assert lock.codes[2] == "5678"

        subentry = next(s for s in entry.subentries.values() if s.title == "user2")
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        await hass.async_block_till_done()

        # A re-interview fixes it, and the lock's integration reloads.
        faults[0].clear()
        await lock._async_on_integration_loaded()
        await async_advance_clock(hass, freezer, 60 * 60)

    assert lock.provider_setup_succeeded
    assert 2 not in lock.codes
