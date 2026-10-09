"""End-to-end tests for the ZHA lock provider through the real LCM entry."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    SERVICE_TURN_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.const import (
    ATTR_SYNC_STATUS,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zha import ZHALock
from tests.common import in_sync_entity_id, slot_entity_id
from tests.conftest import async_advance_time

from .conftest import ZHA_LCM_CONFIG_SLOTS, UnconfirmingDoorLockTable

CONFIGURED_PINS = {slot: config["pin"] for slot, config in ZHA_LCM_CONFIG_SLOTS.items()}


def _zha_lock(lcm_config_entry: MockConfigEntry) -> ZHALock:
    """Return the provider the loaded LCM entry built for the lock."""
    (lock,) = lcm_config_entry.runtime_data.locks.values()
    assert isinstance(lock, ZHALock)
    return lock


async def test_an_unconfirmed_set_ends_in_sync(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
) -> None:
    """
    A set reply with no status is confirmed by the read that follows.

    Nothing is pushed on the strength of the write, so the slot reads in sync
    only because the confirmation read saw the lock hold the code.
    """
    # One tick per slot to write it, plus two for the read-back to land.
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)

    lock = _zha_lock(lcm_config_entry)
    assert unconfirming_lock_table.codes == CONFIGURED_PINS
    for slot_num, pin in CONFIGURED_PINS.items():
        assert lock.coordinator.data.get(pin_address(slot_num)) == (
            SlotCredential.known(pin)
        )
        in_sync = hass.states.get(
            in_sync_entity_id(hass, lcm_config_entry, slot_num, lock.lock.entity_id)
        )
        assert in_sync is not None
        assert in_sync.state == STATE_ON


async def test_an_unconfirmed_clear_ends_in_sync_without_being_reissued(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
) -> None:
    """
    A clear reply with no status is read back, then left alone.

    The slot must end empty and in sync, and the clear must not repeat while
    the coordinator still shows the old code.
    """
    # One tick per slot to write it, plus two for the read-back to land.
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)
    assert unconfirming_lock_table.codes == CONFIGURED_PINS

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {
            ATTR_ENTITY_ID: slot_entity_id(
                hass, SWITCH_DOMAIN, lcm_config_entry, 1, CONF_ENABLED
            )
        },
        blocking=True,
    )
    # The clear, its read-back, and slack to show it is not reissued.
    for _ in range(len(CONFIGURED_PINS) + 4):
        await async_advance_time(hass, TICK_INTERVAL)

    lock = _zha_lock(lcm_config_entry)
    assert unconfirming_lock_table.codes == {2: CONFIGURED_PINS[2]}
    assert lock.coordinator.data.get(pin_address(1)) == SlotCredential.empty()
    in_sync = hass.states.get(
        in_sync_entity_id(hass, lcm_config_entry, 1, lock.lock.entity_id)
    )
    assert in_sync is not None
    assert in_sync.state == STATE_ON
    assert unconfirming_lock_table.clears.count(1) == 1


async def test_a_dropped_set_does_not_read_in_sync(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
) -> None:
    """
    A set the lock drops is not reported as landed.

    The reply carries no status, so nothing says the code was applied; the
    read-back finds the slot empty and the slot must not read in sync.
    """
    unconfirming_lock_table.applies = False
    # Enough ticks for every slot to be written and read back at least once.
    for _ in range(len(CONFIGURED_PINS) + 4):
        await async_advance_time(hass, TICK_INTERVAL)

    lock = _zha_lock(lcm_config_entry)
    assert unconfirming_lock_table.codes == {}
    in_sync = hass.states.get(
        in_sync_entity_id(hass, lcm_config_entry, 1, lock.lock.entity_id)
    )
    assert in_sync is not None
    assert in_sync.state != STATE_ON


async def test_a_dropped_clear_does_not_read_empty(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
) -> None:
    """
    A clear the lock drops is not reported as done.

    The reply carries no status, so the slot is not pushed empty: the
    read-back finds the old code still held and the slot must neither read
    empty nor in sync.
    """
    # One tick per slot to write it, plus two for the read-back to land.
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)
    assert unconfirming_lock_table.codes == CONFIGURED_PINS

    unconfirming_lock_table.applies = False
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {
            ATTR_ENTITY_ID: slot_entity_id(
                hass, SWITCH_DOMAIN, lcm_config_entry, 1, CONF_ENABLED
            )
        },
        blocking=True,
    )
    # The clear and its read-back, with a tick or two to spare.
    for _ in range(3):
        await async_advance_time(hass, TICK_INTERVAL)

    lock = _zha_lock(lcm_config_entry)
    assert unconfirming_lock_table.clears.count(1) >= 1
    assert lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
        CONFIGURED_PINS[1]
    )
    in_sync = hass.states.get(
        in_sync_entity_id(hass, lcm_config_entry, 1, lock.lock.entity_id)
    )
    assert in_sync is not None
    assert in_sync.state != STATE_ON


async def test_a_clear_the_lock_keeps_ignoring_suspends_the_slot(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
) -> None:
    """
    A clear the read-back contradicts counts against the slot, so it stops.

    Each clear is answered without a status and read back still holding the
    old code. Charging each one once suspends the slot after the third,
    instead of clearing again every tick for as long as the lock ignores it.
    """
    # One tick per slot to write it, plus two for the read-back to land.
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)
    assert unconfirming_lock_table.codes == CONFIGURED_PINS

    unconfirming_lock_table.applies = False
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {
            ATTR_ENTITY_ID: slot_entity_id(
                hass, SWITCH_DOMAIN, lcm_config_entry, 1, CONF_ENABLED
            )
        },
        blocking=True,
    )
    # Per clear: a tick to issue it and one to charge its read-back; then the
    # tick that suspends. Doubled for slack.
    for _ in range(2 * (2 * MAX_SYNC_ATTEMPTS + 1)):
        await async_advance_time(hass, TICK_INTERVAL)

    lock = _zha_lock(lcm_config_entry)
    assert unconfirming_lock_table.clears.count(1) == MAX_SYNC_ATTEMPTS
    in_sync = hass.states.get(
        in_sync_entity_id(hass, lcm_config_entry, 1, lock.lock.entity_id)
    )
    assert in_sync is not None
    assert in_sync.attributes[ATTR_SYNC_STATUS] == "suspended"
    issue = async_get_issue_registry(hass).async_get_issue(
        DOMAIN,
        f"slot_suspended_{lcm_config_entry.entry_id}_{lock.lock.entity_id}_1",
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    reason = issue.translation_placeholders["reason"]
    # The code the user revoked is still on the lock: the repair must say so,
    # not ask the lock to accept a code the slot no longer has.
    assert "still open the door" in reason
    assert "accepts the code" not in reason

    for _ in range(10):
        await async_advance_time(hass, TICK_INTERVAL)
    assert unconfirming_lock_table.clears.count(1) == MAX_SYNC_ATTEMPTS


@pytest.mark.parametrize("applies", [True, False], ids=["applied", "ignored"])
async def test_a_clear_on_an_unmanaged_slot_is_read_back_but_never_charged(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
    applies: bool,
) -> None:
    """
    A clear to a slot nothing manages is read back, but cannot be charged.

    The read is what stops the slot showing a code it may no longer hold. A
    failure recorded there would wait for whichever slot is next given the
    number, and be charged to it for a write it never made.
    """
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)
    lock = _zha_lock(lcm_config_entry)
    unmanaged_slot = max(CONFIGURED_PINS) + 1
    address = pin_address(unmanaged_slot)
    await lock.async_internal_set_usercode(unmanaged_slot, "9999")
    await async_advance_time(hass, TICK_INTERVAL)
    assert lock.coordinator.data.get(address) == SlotCredential.known("9999")

    unconfirming_lock_table.applies = applies
    await lock.async_internal_clear_usercode(unmanaged_slot)
    await async_advance_time(hass, TICK_INTERVAL)

    assert unconfirming_lock_table.clears.count(unmanaged_slot) == 1
    assert lock.coordinator.data.get(address) == (
        SlotCredential.empty() if applies else SlotCredential.known("9999")
    )
    assert lock.coordinator.has_pending_write(address) is False
    assert lock.coordinator.take_failed_write(address) is False


async def test_a_reply_about_another_user_does_not_read_as_empty(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    unconfirming_lock_table: UnconfirmingDoorLockTable,
) -> None:
    """
    A reply naming another user leaves the slot unreadable, not empty.

    The lock holds slot 1's code, but the reply to its read names an unused
    user. Taken at its word the coordinator would record the slot empty and
    sync would write the code again.
    """
    # One tick per slot to write it, plus two for the read-back to land.
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)
    assert unconfirming_lock_table.codes == CONFIGURED_PINS
    lock = _zha_lock(lcm_config_entry)

    unused_user = max(CONFIGURED_PINS) + 1
    unconfirming_lock_table.misattributed[1] = unused_user
    await lock.coordinator.async_refresh()

    assert lock.coordinator.data.get(pin_address(1)) is SlotCredential.unreadable()
    assert lock.coordinator.data.get(pin_address(2)) == SlotCredential.known(
        CONFIGURED_PINS[2]
    )
