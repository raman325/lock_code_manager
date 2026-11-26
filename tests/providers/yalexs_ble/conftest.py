"""Yale Access Bluetooth provider test fixtures."""

from collections.abc import AsyncIterator
from unittest.mock import patch

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
from .fake_service import FakeYaleXSBLELock

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


# ---------------------------------------------------------------------------
# E2E fixtures: a full Lock Code Manager entry on a fake Yale Access Bluetooth
# lock, set up through the real async_setup_entry path.
# ---------------------------------------------------------------------------


@pytest.fixture
async def yalexs_ble_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Create a Yale Access Bluetooth entry, a lock device and its main lock entity."""
    yale_entry = MockConfigEntry(domain=YALEXS_BLE_DOMAIN)
    yale_entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=yale_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF")},
        name="Front door",
    )
    lock_entity = er.async_get(hass).async_get_or_create(
        "lock",
        YALEXS_BLE_DOMAIN,
        "test_yale_lock",
        config_entry=yale_entry,
        device_id=device.id,
    )
    hass.states.async_set(lock_entity.entity_id, "locked")
    return lock_entity


@pytest.fixture
async def fake_yale_lock(
    hass: HomeAssistant, yalexs_ble_lock_entity: er.RegistryEntry
) -> FakeYaleXSBLELock:
    """Answer the credential actions from a stateful lock (``fake_service.py``)."""
    lock = FakeYaleXSBLELock(
        hass, yalexs_ble_lock_entity.entity_id, yalexs_ble_lock_entity.device_id
    )
    await lock.async_install()
    return lock


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    yalexs_ble_lock_entity: er.RegistryEntry,
    fake_yale_lock: FakeYaleXSBLELock,
) -> AsyncIterator[MockConfigEntry]:
    """
    Set up a full Lock Code Manager entry managing the Yale lock, slots 1 and 2.

    The Yale Access Bluetooth entry is a mock that never loads, so the
    integration is reported connected for as long as the entry is in use.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [yalexs_ble_lock_entity.entity_id],
            CONF_SLOTS: YALEXS_BLE_LCM_CONFIG_SLOTS,
        },
        unique_id="test_yalexs_ble_e2e",
    )
    entry.add_to_hass(hass)
    with patch.object(
        YaleXSBLELock, "async_is_integration_connected", return_value=True
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
