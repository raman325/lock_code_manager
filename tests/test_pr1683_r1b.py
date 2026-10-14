"""Adversarial review of PR #1683 (round 1b): the Home Assistant side of the swap."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected

from .common import (
    LOCK_1_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_advance_clock,
    async_set_slot_pin,
    async_switch_slot,
    code_entity_id,
    in_sync_entity_id,
    lcm_issue_ids,
    mock_lock,
    when_mock_lock_built,
)


async def _entry(
    hass: HomeAssistant, data: dict = TWO_SLOTS_ON_LOCK_1, unique_id: str = "p1b"
) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_slot_left_to_another_entry_does_not_show_that_entry_s_code(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An entry whose slot another entry already manages on the lock is not in sync.

    Its user's PIN (2222) is not on the lock; the first entry's (5678) is. Its
    per-lock sensors mirrored the first entry's slot: "in sync", and the code
    sensor showed the other entry's user's PIN.
    """
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
        unique_id="p1b2",
    )
    assert mock_lock(second).codes[2] == "5678"

    in_sync = hass.states.get(in_sync_entity_id(hass, second, 2))
    code = hass.states.get(code_entity_id(hass, second, 2))
    assert in_sync.state != STATE_ON
    assert code.state != "5678"
    # Its own slot on the lock is still viewed.
    assert hass.states.get(in_sync_entity_id(hass, second, 3)).state == STATE_ON


async def test_reloading_beside_a_dead_lock_keeps_its_offline_repair(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """
    The repair goes when the lock answers, not when a new worker starts.

    A reload (the first thing a user tries) deleted it while the lock was
    still dead, and a worker that never reached the lock never raises it again.
    """
    entry = await _entry(hass)
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
    Decision D dropped the duplicate pre-check, citing a config-flow refusal
    that does not exist: two users ended up sharing one PIN, in sync, with no
    repair. The lock-side outcome the pre-check gave is kept: the slot is
    turned off and gets its repair.
    """
    entry = await _entry(hass)
    lock = mock_lock(entry)

    await async_set_slot_pin(hass, entry, 2, "1234")  # slot 1's PIN
    await hass.async_block_till_done()

    assert lock.codes.get(2) != "1234"
    assert lcm_issue_ids(hass, "slot_disabled")


async def test_moving_a_pin_from_a_slot_turned_off_is_not_a_duplicate(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A slot on its way out does not count against the PIN it held."""
    entry = await _entry(hass)
    lock = mock_lock(entry)

    await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
    await async_set_slot_pin(hass, entry, 2, "1234")
    await hass.async_block_till_done()

    assert lock.codes.get(2) == "1234"
    assert 1 not in lock.codes or not lock.codes[1]
    assert not lcm_issue_ids(hass, "slot_disabled")
