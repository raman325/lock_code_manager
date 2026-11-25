"""Yale Access Bluetooth provider test fixtures."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.providers.yalexs_ble import (
    YALEXS_BLE_DOMAIN,
    YaleXSBLELock,
)

from ...common import user_subentries

LOCK_ENTITY_ID = "lock.yalexs_ble_test_yale_lock"

YALEXS_BLE_LCM_CONFIG_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
}


@pytest.fixture
async def yalexs_ble_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Yale Access Bluetooth config entry."""
    entry = MockConfigEntry(domain=YALEXS_BLE_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def yalexs_ble_lock(
    hass: HomeAssistant, yalexs_ble_config_entry: MockConfigEntry
) -> YaleXSBLELock:
    """Create a YaleXSBLELock with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        YALEXS_BLE_DOMAIN,
        "test_yale_lock",
        config_entry=yalexs_ble_config_entry,
    )
    return YaleXSBLELock(
        hass,
        dr.async_get(hass),
        entity_reg,
        yalexs_ble_config_entry,
        lock_entity,
    )


@pytest.fixture
async def simple_lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Lock Code Manager entry that manages slots 1 and 2."""
    config = {
        CONF_LOCKS: [LOCK_ENTITY_ID],
        CONF_SLOTS: YALEXS_BLE_LCM_CONFIG_SLOTS,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="test_yalexs_ble_lcm",
        subentries_data=user_subentries(config[CONF_SLOTS]),
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def provider_lock(yalexs_ble_lock: YaleXSBLELock) -> YaleXSBLELock:
    """Alias yalexs_ble_lock for shared test mixins."""
    return yalexs_ble_lock


@pytest.fixture
def provider_config_entry(yalexs_ble_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias yalexs_ble_config_entry for shared test mixins."""
    return yalexs_ble_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return YALEXS_BLE_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[YaleXSBLELock]:
    """Return the provider lock class."""
    return YaleXSBLELock
