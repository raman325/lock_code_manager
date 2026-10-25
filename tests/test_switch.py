"""Test switch platform."""

import logging

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TOGGLE
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    EVENT_STATE_CHANGED,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)

from .common import (
    LOCK_1_ENTITY_ID,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_PIN_ENTITY,
    async_advance_sync_ticks,
    async_switch_slot,
    async_tick_until,
    in_sync_entity_id,
    mock_lock,
    slot_entity_id,
)

_LOGGER = logging.getLogger(__name__)


async def test_switch_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test switch entity."""
    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Toggle switch off
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TOGGLE,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    # Attempt to toggle switch on. This should raise because the PIN value is empty
    with pytest.raises(HomeAssistantError, match="Set a PIN or password"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TOGGLE,
            target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
            blocking=True,
        )

    # Should create a repair issue
    issue_registry = async_get_issue_registry(hass)
    issue_id = f"credential_required_{lock_code_manager_config_entry.entry_id}_2"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == "warning"
    assert issue.is_fixable is True

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF

    # Set PIN value so we can toggle switch on
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "1234"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TOGGLE,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_ON

    # The credential_required repair issue should be auto-deleted on successful turn on
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is None


_FIVE_SLOTS = {
    slot_num: {CONF_NAME: f"user{slot_num}", CONF_PIN: pin, CONF_ENABLED: True}
    for slot_num, pin in {1: "1234", 2: "5678", 3: "3333", 4: "4444", 5: "5555"}.items()
}


async def test_toggling_one_slot_leaves_every_other_slot_alone(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #199 and #209: a slot's switch moves that slot and no other.

    Turning one slot off was reported to turn another slot's PIN active, or
    the slot's own the wrong way round. Slot 3 is turned off and back on;
    no other slot's active or in-sync sensor changes, and only slot 3's code
    leaves the lock and comes back.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_SLOTS: _FIVE_SLOTS},
        unique_id="Five Slots",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    in_syncs = {
        slot_num: in_sync_entity_id(hass, entry, slot_num) for slot_num in _FIVE_SLOTS
    }
    actives = {
        slot_num: slot_entity_id(
            hass, BINARY_SENSOR_DOMAIN, entry, slot_num, ATTR_ACTIVE
        )
        for slot_num in _FIVE_SLOTS
    }
    all_codes = {slot_num: slot[CONF_PIN] for slot_num, slot in _FIVE_SLOTS.items()}

    def _in_sync(slot_nums) -> bool:
        return all(
            hass.states.get(in_syncs[slot]).state == STATE_ON for slot in slot_nums
        )

    await async_tick_until(
        hass,
        freezer,
        lambda: _in_sync(_FIVE_SLOTS),
        max_ticks=8,
        what="every slot in sync",
    )
    assert lock.codes == all_codes
    others = [
        entity_id
        for slot_num in _FIVE_SLOTS
        if slot_num != 3
        for entity_id in (in_syncs[slot_num], actives[slot_num])
    ]
    written: list[State] = []

    @callback
    def _record(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            written.append(state)

    unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    # Only what the toggles write, not the setup that wrote slots 3 to 5.
    lock.service_calls.clear()

    await async_switch_slot(hass, entry, 3, SERVICE_TURN_OFF)
    assert hass.states.get(actives[3]).state == STATE_OFF
    await async_tick_until(
        hass, freezer, lambda: 3 not in lock.codes, max_ticks=3, what="slot 3 cleared"
    )
    await async_advance_sync_ticks(hass, freezer, 3)
    assert lock.codes == {slot: pin for slot, pin in all_codes.items() if slot != 3}
    assert _in_sync(_FIVE_SLOTS)

    await async_switch_slot(hass, entry, 3, SERVICE_TURN_ON)
    assert hass.states.get(actives[3]).state == STATE_ON
    await async_tick_until(
        hass,
        freezer,
        lambda: lock.codes.get(3) == "3333",
        max_ticks=3,
        what="slot 3 set",
    )
    await async_advance_sync_ticks(hass, freezer, 3)
    unsub()

    assert lock.codes == all_codes
    assert _in_sync(_FIVE_SLOTS)
    assert [state for state in written if state.entity_id in others] == []
    assert lock.service_calls["clear_usercode"] == [(3,)]
    assert lock.service_calls["set_usercode"] == [(3, "3333", "user3")]
    assert all(hass.states.get(actives[slot]).state == STATE_ON for slot in _FIVE_SLOTS)

    await hass.config_entries.async_unload(entry.entry_id)
