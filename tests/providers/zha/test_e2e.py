"""End-to-end tests for the ZHA lock provider through the real LCM entry."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    SERVICE_TURN_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import TICK_INTERVAL
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
    assert 1 <= unconfirming_lock_table.clears.count(1) <= 2
