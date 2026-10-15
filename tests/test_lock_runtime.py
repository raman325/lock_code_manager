"""The core wired into Home Assistant: the lock registry and what it raises."""

from functools import partial
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.ha.runtime import (
    GIVEN_UP_KEY,
    ISSUES_KEY,
    _label,
    async_give_up,
    async_resume_slot,
    async_want,
    lock_registry,
    signal_lock_changed,
)
from custom_components.lock_code_manager.model.exceptions import (
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationBlocked,
    LockOperationFailed,
    LockOperationUnsupported,
)
from custom_components.lock_code_manager.model.models import SlotCode, SlotCredential
from custom_components.lock_code_manager.model.queries import (
    get_entry_config,
    subentry_id_for_slot,
)
from custom_components.lock_code_manager.repairs import async_create_fix_flow
from custom_components.lock_code_manager.sync.slot import (
    Holds,
    Reading,
    Repair,
    Seen,
    Status,
)
from custom_components.lock_code_manager.sync.worker import BACKOFF_MAX, OFFLINE_AFTER

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_advance_clock,
    async_set_slot_pin,
    async_setup_lcm_entry,
    async_switch_slot,
    code_entity_id,
    current_entry,
    in_sync_entity_id,
    lcm_issue_ids,
    lock_workers,
    mock_lock,
    slot_entity_id,
    when_mock_lock_built,
)


async def test_home_assistant_stopping_stops_every_lock_worker(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Nothing is left talking to a lock once Home Assistant stops."""
    await async_setup_lcm_entry(hass)
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
    await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(
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


_SECOND_ON_SLOT_2 = {
    CONF_LOCKS: [LOCK_1_ENTITY_ID],
    CONF_SLOTS: {2: {CONF_NAME: "other2", CONF_PIN: "2222", CONF_ENABLED: True}},
}


@pytest.mark.parametrize("clear", [True, False], ids=["cleared", "left"])
async def test_a_slot_given_up_goes_to_the_entry_still_configuring_it(
    hass: HomeAssistant, mock_lock_config_entry, clear: bool
) -> None:
    """Stored storage with two entries on one slot: the other's PIN lands."""
    first = await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")
    lock = mock_lock(second)
    assert lock.codes[2] == "5678"  # the first entry's

    async_give_up(hass, first, LOCK_1_ENTITY_ID, 2, clear=clear)
    await hass.async_block_till_done()

    assert lock.codes[2] == "2222"
    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) == second.entry_id


async def test_a_slot_given_up_is_not_handed_to_an_entry_on_another_lock(
    hass: HomeAssistant, mock_lock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """The same slot number on a different lock is no overlap: nothing is pushed."""
    first = await async_setup_lcm_entry(hass)
    await async_setup_lcm_entry(
        hass, {**_SECOND_ON_SLOT_2, CONF_LOCKS: [LOCK_2_ENTITY_ID]}, unique_id="rt2"
    )

    async_give_up(hass, first, LOCK_1_ENTITY_ID, 2)
    await hass.async_block_till_done()

    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) is None
    assert "cannot set slot" not in caplog.text


async def test_a_slot_on_a_lock_its_owner_drops_goes_to_the_entry_still_configuring_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The owner takes the lock out: the other entry configuring the slot takes it over."""
    two_locks = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        CONF_SLOTS: TWO_SLOTS_ON_LOCK_1[CONF_SLOTS],
    }
    first = await async_setup_lcm_entry(hass, two_locks)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")
    lock = mock_lock(second)

    hass.config_entries.async_update_entry(
        first, options={**two_locks, CONF_LOCKS: [LOCK_2_ENTITY_ID]}
    )
    await hass.async_block_till_done()

    assert lock.codes[2] == "2222"
    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) == second.entry_id


async def test_a_slot_of_a_deleted_entry_goes_to_the_entry_still_configuring_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The owner is deleted: the other entry configuring the slot takes it over."""
    first = await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")
    lock = mock_lock(second)

    assert await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    assert lock.codes[2] == "2222"
    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) == second.entry_id


async def test_an_owner_reloading_keeps_its_slot(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A reload is no hand-off: the owner still configures the slot, and gets it back."""
    first = await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")

    assert await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()

    assert mock_lock(first).codes[2] == "5678"
    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) == first.entry_id
    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) != second.entry_id


async def test_a_disabled_owner_keeps_its_slot(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Disabling is no hand-off: the slot keeps the owner's PIN, and nobody claims it."""
    first = await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")

    assert await hass.config_entries.async_set_disabled_by(
        first.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()

    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) is None
    assert mock_lock(second).codes[2] == "5678"


async def test_a_slot_of_a_disabled_entry_deleted_goes_to_the_entry_still_configuring_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Deleted while disabled, so already unloaded: the delete still hands the slot off."""
    first = await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")
    assert await hass.config_entries.async_set_disabled_by(
        first.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()

    assert await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) == second.entry_id
    assert mock_lock(second).codes[2] == "2222"


async def test_a_slot_its_owner_removes_goes_to_the_entry_still_configuring_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The owner deletes the slot (its user): the other entry configuring it takes it over."""
    first = await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(hass, _SECOND_ON_SLOT_2, unique_id="rt2")

    hass.config_entries.async_remove_subentry(first, subentry_id_for_slot(first, 2))
    await hass.async_block_till_done()

    assert lock_registry(hass).owner(LOCK_1_ENTITY_ID, 2) == second.entry_id
    assert mock_lock(second).codes[2] == "2222"


async def test_a_slot_given_up_then_wanted_again_is_cleared_plainly(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Wanted again, the slot is no longer on its way out: no release."""
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)
    lock.fail(None, error=LockDisconnected)  # the clear cannot go yet
    async_give_up(hass, entry, LOCK_1_ENTITY_ID, 1)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None
    assert 1 in worker.port.releasing

    async_want(hass, entry, LOCK_1_ENTITY_ID, 1, "1234")

    assert 1 not in worker.port.releasing


async def test_a_lock_that_stops_answering_raises_its_offline_repair(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """Answered before, silent since: the user is told, once it has backed off."""
    entry = await async_setup_lcm_entry(hass)
    mock_lock(entry).fail(None, error=LockDisconnected)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, 6 * 60 * 60)

    assert lcm_issue_ids(hass, "lock_offline")


async def test_a_refused_pin_s_repair_goes_once_the_slot_is_back_in_sync(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Re-enabled with a PIN the lock takes: the refusal is answered."""
    entry = await async_setup_lcm_entry(hass)
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
    entry = await async_setup_lcm_entry(hass)
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
    entry = await async_setup_lcm_entry(hass)
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
        assert 1 in worker.port.releasing
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
    entry = await async_setup_lcm_entry(hass)
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
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)
    lock.ignore("set", 1)
    with patch.object(lock, "describe_link_health", return_value=LINK_HEALTH):
        await async_set_slot_pin(hass, entry, 1, "4321")
        await hass.async_block_till_done()

    reason = _hold_reason(hass, entry, 1)
    assert reason.startswith("the lock did not take the code after several tries")
    assert reason.endswith(LINK_HEALTH)


@pytest.mark.parametrize(
    "error",
    [LockDisconnected("timed out"), LockOperationFailed("the lock said no")],
    ids=["disconnected", "failed"],
)
async def test_a_write_that_fails_in_transport_is_followed_by_its_link(
    hass: HomeAssistant, mock_lock_config_entry, error: Exception
) -> None:
    """A failure nobody explained keeps the generic reason and the link (#1397)."""
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)
    lock.fail("set", 1, error=error)
    with patch.object(lock, "describe_link_health", return_value=LINK_HEALTH):
        await async_set_slot_pin(hass, entry, 1, "4321")
        await hass.async_block_till_done()

    assert _hold_reason(hass, entry, 1) == (
        f"the lock did not take the code after several tries\n\n{LINK_HEALTH}"
    )


async def test_a_cause_the_provider_names_is_not_followed_by_its_link(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Beside a cause, the link's counters point away from it."""
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)
    lock.fail("set", 1, error=LockOperationBlocked("user 7 holds this index"))
    with patch.object(lock, "describe_link_health", return_value=LINK_HEALTH):
        await async_set_slot_pin(hass, entry, 1, "4321")
        await hass.async_block_till_done()

    assert _hold_reason(hass, entry, 1) == "user 7 holds this index"


async def test_a_provider_that_cannot_measure_its_link_leaves_the_reason_alone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Nothing is added when the provider has nothing to say of its transport."""
    entry = await async_setup_lcm_entry(hass)
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


async def test_a_slot_removed_while_setup_revalidation_failed_is_not_silently_kept(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_lock_config_entry
) -> None:
    """
    A slot removed while the provider's setup flag is down still leaves the lock.

    ``provider_setup_succeeded`` drops back to False when a reconnect's
    revalidation fails, on a lock whose codes were written. Removing a slot
    then must not leave its PIN on the lock with nothing to show for it: it
    is cleared once setup recovers, or a repair says the code may remain.
    """
    entry = current_entry("r1a-setup", {1: "1111", 2: "2222"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    assert lock.codes[2] == "2222"

    # A reconnect's revalidation failed: the flag is down.
    lock._setup_succeeded = False

    subentry = next(s for s in entry.subentries.values() if s.title == "user2")
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()

    # Revalidation recovers.
    lock._setup_succeeded = True
    # The backoff's probe finds it set up again.
    await async_advance_clock(hass, freezer, 300)

    assert 2 not in lock.codes or any(
        "slot_clear_failed" in i or "slot_suspended" in i for i in lcm_issue_ids(hass)
    )


async def test_reloading_beside_a_dead_lock_keeps_its_offline_repair(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """
    The repair goes when the lock answers, not when a new worker starts.

    A reload (the first thing a user tries) deleted it while the lock was
    still dead, and a worker that never reached the lock never raises it again.
    """
    entry = await async_setup_lcm_entry(hass)
    mock_lock(entry).fail(None, error=LockDisconnected)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, 6 * 60 * 60)
    assert lcm_issue_ids(hass, "lock_offline")

    with when_mock_lock_built(
        lambda lock: lock.fail(None, error=LockDisconnected)
    ) as faults:
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 6 * 60 * 60)
        assert lcm_issue_ids(hass, "lock_offline")

        faults[0].clear()
        await async_advance_clock(hass, freezer, 60 * 60)
        assert not lcm_issue_ids(hass, "lock_offline")


async def test_a_pin_another_user_has_on_the_lock_turns_the_slot_off(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A PIN another user already has on the lock is refused before it is sent.

    No config-flow check refuses the same PIN in two slots, so without this
    two users would share one PIN, in sync, with no repair. The slot is
    turned off and gets its repair.
    """
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)

    await async_set_slot_pin(hass, entry, 2, "1234")  # slot 1's PIN
    await hass.async_block_till_done()

    assert lock.codes.get(2) != "1234"
    assert lcm_issue_ids(hass, "slot_disabled")


async def test_moving_a_pin_from_a_slot_turned_off_is_not_a_duplicate(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A slot on its way out does not count against the PIN it held."""
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)

    await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
    await async_set_slot_pin(hass, entry, 2, "1234")
    await hass.async_block_till_done()

    assert lock.codes.get(2) == "1234"
    assert 1 not in lock.codes or not lock.codes[1]
    assert not lcm_issue_ids(hass, "slot_disabled")


async def test_a_user_removed_while_the_lock_is_dead_at_boot_still_leaves_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_lock_config_entry
) -> None:
    """
    A restart with the lock unreachable defers its provider's setup.

    Removing a user then (revoking a dog walker's access) unclaimed the slot
    instead of clearing it: the worker never answered, the provider is not
    set up, so ``async_give_up`` took the lock for one never set up. When the
    lock came back the PIN stayed on it, with no repair -- and the
    unmanaged-code sweep only ever runs once.
    """
    entry = current_entry("r2a-dead-boot", {1: "1234", 2: "5678"})
    entry.add_to_hass(hass)

    def _dead(lock):
        # Its integration is up, the node is dead: setup is deferred.
        fault = lock.fail(None, error=LockDisconnected)
        original = lock.async_is_integration_connected

        async def _connected() -> bool:
            if fault in lock.faults:
                return False
            return await original()

        lock.async_is_integration_connected = _connected
        return fault

    with when_mock_lock_built(_dead) as faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        lock = mock_lock(entry)
        assert not lock.provider_setup_succeeded
        assert lock.codes[2] == "5678"

        subentry = next(s for s in entry.subentries.values() if s.title == "user2")
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        await hass.async_block_till_done()

        # The lock comes back.
        faults[0].clear()
        await async_advance_clock(hass, freezer, 60 * 60)

    assert lock.provider_setup_succeeded
    assert 2 not in lock.codes or any(
        "slot_clear_failed" in i or "slot_suspended" in i for i in lcm_issue_ids(hass)
    )


async def test_a_duplicate_refused_unsent_is_not_the_lock_answering(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The duplicate check refuses a PIN before anything is sent.

    It went to the worker as a refusal "sent", so it counted as the lock
    answering: during an outage it paid the tries owed by slots whose syncs
    got no answer (an outage charged to a normal slot), reset the lock's
    no-answers and marked a never-reached lock as answered.
    """
    entry = current_entry("r2a-dup-unsent", {1: "1234", 2: "5678"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    # Somebody's own code, in a slot nobody here manages.
    lock.codes[7] = "9999"
    await worker.read()
    answers = worker._answers

    lock.fail(None, error=LockDisconnected)
    # Slot 1's sync gets no answer (owed); slot 2 asks for that code's PIN.
    worker.want(1, "1111")
    worker.want(2, "9999")
    await hass.async_block_till_done()

    assert worker.slots[1].tries == 0
    assert worker._answers == answers


# Long enough for any number of unanswered reads: the backoff is capped.
HOURS = 6 * 60 * 60


async def test_a_lock_is_reported_offline_once_reached_and_lost_until_it_is_back(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
) -> None:
    """
    A lock not reached since startup is never reported offline (#1257).

    Once reached and then lost it is, after it has gone unanswered long
    enough; the repair goes as soon as it answers again.
    """
    entry = current_entry("Offline Cycle", {1: "1234"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(
        lambda lock: lock.fail("get", error=LockDisconnected)
    ) as built:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    [starting] = built
    code = code_entity_id(hass, entry, 1)

    await async_advance_clock(hass, freezer, HOURS)
    assert starting.hits > OFFLINE_AFTER
    assert not lcm_issue_ids(hass, "lock_offline")
    assert hass.states.get(code).state == STATE_UNAVAILABLE

    starting.clear()
    await async_advance_clock(hass, freezer, BACKOFF_MAX + 60)
    assert hass.states.get(code).state == "1234"

    lost = mock_lock(entry).fail("get", error=LockDisconnected)
    await async_advance_clock(hass, freezer, HOURS)
    assert lcm_issue_ids(hass, "lock_offline")

    lost.clear()
    await async_advance_clock(hass, freezer, BACKOFF_MAX + 60)
    assert not lcm_issue_ids(hass, "lock_offline")
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_what_a_slot_wants_on_the_lock_follows_its_switch_and_pin(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An enabled slot with a PIN puts it on the lock; anything else clears it.

    The lock starts holding codes in slots 1 and 2. Slot 1 is enabled with
    no PIN and slot 2 is disabled with one: both are cleared, never set to an
    empty PIN. Slot 3 is enabled with a PIN, which is written.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "blank", CONF_PIN: "", CONF_ENABLED: True},
                2: {CONF_NAME: "off", CONF_PIN: "5678", CONF_ENABLED: False},
                3: {CONF_NAME: "on", CONF_PIN: "4242", CONF_ENABLED: True},
            },
        },
        unique_id="Desired Codes",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)

    assert lock.codes == {3: "4242"}
    assert [call[0] for call in lock.service_calls["set_usercode"]] == [3]
    for slot_num in (1, 2, 3):
        assert hass.states.get(in_sync_entity_id(hass, entry, slot_num)).state == (
            STATE_ON
        )

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_change_on_one_lock_reaches_only_that_locks_views(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A code changed on one lock is the business of its views, not the other lock's."""
    heard: list[str] = []
    unsubs = [
        async_dispatcher_connect(
            hass,
            signal_lock_changed(lock_entity_id),
            partial(heard.append, lock_entity_id),
        )
        for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    ]
    try:
        lock = mock_lock(lock_code_manager_config_entry, LOCK_1_ENTITY_ID)
        lock.codes[1] = "0000"
        lock._push_credential_update(1, SlotCredential.known("0000"))
        await hass.async_block_till_done()
    finally:
        for unsub in unsubs:
            unsub()

    assert heard
    assert set(heard) == {LOCK_1_ENTITY_ID}


async def test_a_slot_taken_on_one_lock_still_reaches_the_entrys_other_lock(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A PIN change goes to every lock the entry holds: one where another entry
    owns the slot refuses it, and the others still get it.
    """
    first = await async_setup_lcm_entry(hass, TWO_SLOTS_ON_LOCK_1, "first")
    second = await async_setup_lcm_entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
            CONF_SLOTS: {
                2: {CONF_NAME: "other2", CONF_PIN: "2222", CONF_ENABLED: True},
            },
        },
        "second",
    )
    assert list(second.runtime_data.locks) == [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]

    await async_set_slot_pin(hass, second, 2, "3333")
    await hass.async_block_till_done()

    assert mock_lock(second, LOCK_2_ENTITY_ID).codes[2] == "3333"
    assert mock_lock(first, LOCK_1_ENTITY_ID).codes[2] == "5678"


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
    entry = MockConfigEntry(
        domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="held_release"
    )
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

    The slot_clear_failed issue stays once no entry holds the lock.
    """
    entry = await async_setup_lcm_entry(hass)
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
    entry = await async_setup_lcm_entry(hass)  # migrated: the next setup does no sweep
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


async def test_a_held_release_acknowledged_after_a_restart_is_cleared(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Acknowledging slot_clear_failed clears the slot again.

    The issue is persistent, so it outlives a restart; what the integration
    remembers about slots given up does not. Acknowledged after a restart,
    the flow found nothing to resume, did nothing, and the repair went away
    with the code still on the lock.
    """
    entry = await async_setup_lcm_entry(hass)
    lock = mock_lock(entry)
    refusal = lock.refuse("clear", 1)
    await _remove_user_in_slot(hass, entry, 1)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    assert issue_id in lcm_issue_ids(hass, "slot_suspended")

    # A restart: the entry goes, and what was kept in memory with it.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data[DOMAIN][GIVEN_UP_KEY].clear()
    hass.data[DOMAIN][ISSUES_KEY].clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    assert lock.codes.get(1) == "1234"
    assert issue_id in lcm_issue_ids(hass, "slot_suspended")
    refusal.clear()

    # The flow gets what the repair kept, as Home Assistant hands it over.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    flow = await async_create_fix_flow(hass, issue_id, dict(issue.data or {}))
    flow.hass = hass
    await flow.async_step_init({})
    await hass.async_block_till_done()

    assert 1 not in lock.codes


async def test_acknowledging_a_held_clear_on_a_lock_that_left_every_entry(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Acknowledging clears it with a worker started for the purpose.

    The lock was taken out of the entry, and its release clear was refused.
    Acknowledging the repair starts a worker -- but nothing sets its provider
    up (no entry lists the lock), so the clear is never sent: the slot is held
    again and the same "did not clear after two tries" repair comes back.
    """
    two_locks = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        CONF_SLOTS: TWO_SLOTS_ON_LOCK_1[CONF_SLOTS],
    }
    entry = await async_setup_lcm_entry(hass, two_locks)
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


async def test_a_user_removed_from_a_lock_whose_setup_crashed_is_let_go(
    hass: HomeAssistant, mock_lock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A lock whose setup crashed is let go of, and the code left on it is logged.

    Nothing retries a setup that raised an unexpected error short of a reload,
    so a clear given to it would wait forever and keep the lock in service
    after its entry unloads.
    """
    entry = current_entry("Crashed setup", {1: "1234", 2: "5678"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(
        lambda lock: lock.fail("setup", error=RuntimeError("provider bug"))
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        subentry = next(s for s in entry.subentries.values() if s.title == "user2")
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        await hass.async_block_till_done()

    assert "Slot 2 on lock.test_1 was given up without clearing it" in caplog.text
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert lock_workers(hass) == []


@pytest.mark.parametrize(
    ("reading", "label"),
    [
        (Holds("1234"), "1234"),
        (Seen.EMPTY, SlotCode.EMPTY),
        (Seen.MASKED, SlotCode.UNREADABLE_CODE),
        (Seen.SILENT, SlotCode.UNANSWERED),
        (Seen.DISABLED, SlotCode.DISABLED),
    ],
    ids=["holds", "empty", "masked", "silent", "disabled"],
)
def test_the_views_are_told_exactly_what_the_lock_said(
    reading: Reading, label: str
) -> None:
    """
    Every reading reaches the views as itself (spec D, §12).

    A slot the lock answered nothing about, or holds a code for that is not in
    force, used to be left out, and the card drew it as holding nothing. A
    withheld code the worker trusts used to be sent as the configured PIN, as
    though the lock had reported it. The card decides the stand-in from
    ``managed`` and ``in_sync``; the label never does.
    """
    assert _label(reading) == label
