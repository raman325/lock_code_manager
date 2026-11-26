"""Full lifecycle E2E tests for the Yale Access Bluetooth lock provider."""

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_PIN, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.providers.yalexs_ble import (
    YALEXS_BLE_DOMAIN,
    YaleXSBLELock,
)
from tests.common import (
    async_advance_sync_ticks,
    async_set_slot_pin,
    in_sync_entity_id,
    lcm_issue_ids,
)

from .conftest import YALEXS_BLE_LCM_CONFIG_SLOTS
from .fake_service import FakeYaleXSBLELock


async def test_provider_discovered_as_yalexs_ble(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    yalexs_ble_lock_entity: er.RegistryEntry,
) -> None:
    """LCM discovers the lock and creates a YaleXSBLELock for it."""
    lock = lcm_config_entry.runtime_data.locks.get(yalexs_ble_lock_entity.entity_id)
    assert isinstance(lock, YaleXSBLELock)
    assert lock.domain == YALEXS_BLE_DOMAIN
    assert lock.supports_code_slot_events is True
    assert lock.usercode_scan_interval == timedelta(minutes=15)


async def test_users_are_written_with_their_names_and_reported_in_sync(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lcm_config_entry: MockConfigEntry,
    yalexs_ble_lock_entity: er.RegistryEntry,
    fake_yale_lock: FakeYaleXSBLELock,
) -> None:
    """Each enabled user's PIN lands in its slot, named, and the slot reads in sync."""
    await async_advance_sync_ticks(hass, freezer, 10)

    assert fake_yale_lock.pins == {
        slot: config[CONF_PIN] for slot, config in YALEXS_BLE_LCM_CONFIG_SLOTS.items()
    }
    assert set(fake_yale_lock.names) == set(YALEXS_BLE_LCM_CONFIG_SLOTS)
    for slot in YALEXS_BLE_LCM_CONFIG_SLOTS:
        state = hass.states.get(
            in_sync_entity_id(
                hass, lcm_config_entry, slot, yalexs_ble_lock_entity.entity_id
            )
        )
        assert state.state == STATE_ON
    assert lcm_issue_ids(hass, "slot_suspended") == []


async def test_a_new_pin_replaces_the_old_one(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lcm_config_entry: MockConfigEntry,
    fake_yale_lock: FakeYaleXSBLELock,
) -> None:
    """Changing a user's PIN rewrites the slot on the lock."""
    await async_advance_sync_ticks(hass, freezer, 10)

    await async_set_slot_pin(hass, lcm_config_entry, 1, "4321")
    await async_advance_sync_ticks(hass, freezer, 10)

    assert fake_yale_lock.pins[1] == "4321"
    assert lcm_issue_ids(hass, "slot_suspended") == []


async def test_a_pin_another_code_holds_is_refused_not_retried(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    yalexs_ble_lock_entity: er.RegistryEntry,
    fake_yale_lock: FakeYaleXSBLELock,
) -> None:
    """
    The lock refusing slot 1's PIN, held by a code LCM does not manage, disables slot 1.

    The refusal (``pin_already_in_use``) is the lock's answer, so the write is
    not sent again on every retry, and the foreign code is left alone.
    """
    fake_yale_lock.pins[9] = YALEXS_BLE_LCM_CONFIG_SLOTS[1][CONF_PIN]
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [yalexs_ble_lock_entity.entity_id],
            CONF_SLOTS: YALEXS_BLE_LCM_CONFIG_SLOTS,
        },
        unique_id="test_yalexs_ble_refused",
    )
    entry.add_to_hass(hass)
    with patch.object(
        YaleXSBLELock, "async_is_integration_connected", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            await async_advance_sync_ticks(hass, freezer, 30)

            slot_1_sets = [
                data
                for service, data in fake_yale_lock.calls
                if service == "set_lock_credential" and data["credential_index"] == 1
            ]
            assert len(slot_1_sets) == 1
            assert lcm_issue_ids(hass, "slot_disabled")
            assert fake_yale_lock.pins[2] == YALEXS_BLE_LCM_CONFIG_SLOTS[2][CONF_PIN]
            assert fake_yale_lock.pins[9] == YALEXS_BLE_LCM_CONFIG_SLOTS[1][CONF_PIN]
            assert not any(
                service == "clear_lock_credential" and data["credential_index"] == 9
                for service, data in fake_yale_lock.calls
            )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


async def test_a_keypad_unlock_is_recorded_for_the_user(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lcm_config_entry: MockConfigEntry,
    fake_yale_lock: FakeYaleXSBLELock,
) -> None:
    """The activity event of a keypad unlock fires LCM's credential-used event."""
    await async_advance_sync_ticks(hass, freezer, 10)
    used = []
    hass.bus.async_listen("lock_code_manager_credential_used", used.append)

    fake_yale_lock.keypad_unlock(2)
    await hass.async_block_till_done()

    assert len(used) == 1
    assert used[0].data["name"] == YALEXS_BLE_LCM_CONFIG_SLOTS[2]["name"]
