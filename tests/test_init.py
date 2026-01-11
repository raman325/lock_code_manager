"""Test init module."""

import copy
import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.components.lovelace.const import CONF_RESOURCE_TYPE_WS
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    ATTR_CODE,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    Platform,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
    EVENT_PIN_USED,
    SERVICE_HARD_REFRESH_USERCODES,
    STRATEGY_PATH,
)

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    LOCK_DATA,
)

_LOGGER = logging.getLogger(__name__)


@pytest.mark.parametrize("config", [{}])
async def test_entry_setup_and_unload(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test entry setup and unload."""
    mock_lock_entry_id = mock_lock_config_entry.entry_id
    lcm_entry_id = lock_code_manager_config_entry.entry_id
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        device = dev_reg.async_get_device({(DOMAIN, entity_id)})
        assert device
        assert device.config_entries == {mock_lock_entry_id, lcm_entry_id}

    unique_ids = set()
    for slot in range(1, 3):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|2|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 2
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 4
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 2
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 4

    ll_data = hass.data[LL_DOMAIN]
    assert ll_data
    resources = ll_data.resources
    assert resources
    assert resources.loaded
    assert any(data[CONF_URL] == STRATEGY_PATH for data in resources.async_items())

    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        assert not hass.data[LOCK_DATA][lock_entity_id]["service_calls"][
            "hard_refresh_codes"
        ]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["hard_refresh_codes"]
    assert not hass.data[LOCK_DATA][LOCK_2_ENTITY_ID]["service_calls"][
        "hard_refresh_codes"
    ]

    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][1][CONF_NUMBER_OF_USES] = 5
    new_config[CONF_SLOTS][2].pop(CONF_NUMBER_OF_USES)
    new_config[CONF_SLOTS][3] = {
        CONF_NAME: "test3",
        ATTR_CODE: "4321",
        CONF_ENABLED: True,
    }

    assert hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    unique_ids = set()
    for slot in range(1, 4):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|1|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
        if hass.states.get(entity.entity_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 9
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 3
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 3
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 6

    new_config = copy.deepcopy(new_config)
    new_config[CONF_SLOTS].pop(3)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]

    assert hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    # Validate that the config entry is removed from the device associated with the
    # lock that was removed from the config entry
    device = dev_reg.async_get_device({(DOMAIN, LOCK_2_ENTITY_ID)})
    assert device
    assert device.config_entries == {mock_lock_entry_id}

    unique_ids = set()
    for slot in range(1, 3):
        for key in (ATTR_CODE, ATTR_IN_SYNC):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{LOCK_1_ENTITY_ID}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|1|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 4
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 2
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 2
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 2
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 4


async def test_reauth(hass: HomeAssistant, lock_code_manager_config_entry):
    """Test reauth."""
    assert (
        len(
            [
                flow
                for flow in lock_code_manager_config_entry.async_get_active_flows(
                    hass, {SOURCE_REAUTH}
                )
            ]
        )
        == 1
    )


@pytest.mark.parametrize("config", [{}])
async def test_resource_already_loaded_ui(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test when strategy resource is already loaded in UI mode."""
    resources = hass.data[LL_DOMAIN].resources
    assert resources
    await resources.async_load()

    await resources.async_create_item(
        {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: STRATEGY_PATH}
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    items = [
        item for item in resources.async_items() if item[CONF_URL] == STRATEGY_PATH
    ]
    assert len(items) == 1

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "config",
    [{"mode": "yaml", "resources": [{"type": "module", CONF_URL: STRATEGY_PATH}]}],
)
async def test_resource_already_loaded_yaml(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test when strategy resource is already loaded in YAML mode."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    resources = hass.data[LL_DOMAIN].resources
    assert resources
    items = [
        item for item in resources.async_items() if item[CONF_URL] == STRATEGY_PATH
    ]
    assert len(items) == 1

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "config",
    [{"mode": "yaml", "resources": [{"type": "module", CONF_URL: "fake_module.js"}]}],
)
async def test_resource_not_loaded_yaml(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Test when strategy resource is not loaded in YAML mode shows warning."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    resources = hass.data[LL_DOMAIN].resources
    assert resources
    assert not any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    # Verify warning about manual YAML registration was logged
    assert "can't automatically be registered" in caplog.text
    assert "running in YAML mode" in caplog.text

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "config",
    [{"mode": "yaml", "resources": [{"type": "module", CONF_URL: STRATEGY_PATH}]}],
)
async def test_resource_unload_skips_yaml_mode(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Ensure resource removal is skipped when resources are managed via YAML."""
    caplog.set_level(logging.DEBUG)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Simulate auto-registration bookkeeping being set from a prior run
    hass.data[DOMAIN]["resources"] = True

    resources = hass.data[LL_DOMAIN].resources
    assert resources
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    await hass.config_entries.async_unload(config_entry.entry_id)

    # Resource should remain because YAML mode can't be modified automatically
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    # Verify the YAML mode skip messages were logged
    assert "already in YAML resources" in caplog.text
    assert "skipping automatic removal" in caplog.text


async def test_two_entries_same_locks(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test two entries that use same locks but different slots set up successfully."""
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS] = {3: {CONF_ENABLED: False, CONF_PIN: "0123"}}
    new_entry = MockConfigEntry(
        domain=DOMAIN, data=new_config, unique_id="Mock Title 2", title="Mock Title 2"
    )
    new_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(new_entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 9
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 3
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 3
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 6


@pytest.mark.parametrize("config", [{}])
async def test_resource_not_loaded_on_unload(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test when strategy resource is not loaded when unloading config entry."""
    resources = hass.data[LL_DOMAIN].resources
    assert resources
    await resources.async_load()

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    await resources.async_delete_item(
        next(
            item["id"]
            for item in resources.async_items()
            if item[CONF_URL] == STRATEGY_PATH
        )
    )

    await hass.config_entries.async_unload(config_entry.entry_id)

    assert not any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert DOMAIN not in hass.data


@pytest.mark.parametrize("config", [{}])
async def test_resource_reregistered_after_unload_and_new_entry(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test resource is re-registered when new entry added after all entries removed."""
    resources = hass.data[LL_DOMAIN].resources
    assert resources
    await resources.async_load()

    # Set up first config entry
    config_entry_1 = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title 1", title="Mock Title 1"
    )
    config_entry_1.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry_1.entry_id)
    await hass.async_block_till_done()

    # Set up second config entry
    config_entry_2 = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title 2", title="Mock Title 2"
    )
    config_entry_2.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry_2.entry_id)
    await hass.async_block_till_done()

    # Verify resource is registered
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert hass.data[DOMAIN]["resources"] is True

    # Remove first entry - resource should still exist
    await hass.config_entries.async_remove(config_entry_1.entry_id)
    await hass.async_block_till_done()
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    # Remove second entry - resource should be cleaned up
    await hass.config_entries.async_remove(config_entry_2.entry_id)
    await hass.async_block_till_done()
    assert not any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert DOMAIN not in hass.data

    # Set up a new config entry - resource should be re-registered
    config_entry_3 = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title 3", title="Mock Title 3"
    )
    config_entry_3.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry_3.entry_id)
    await hass.async_block_till_done()

    # Verify resource is re-registered
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert hass.data[DOMAIN]["resources"] is True

    # Clean up
    await hass.config_entries.async_remove(config_entry_3.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_entry_setup_and_unload_before_ha_started(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test entry setup before HA started and safe_unsub on unload."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Startup"
    )
    config_entry.add_to_hass(hass)

    # Setup while HA is "starting" - exercises the startup listener code path
    with patch.object(hass, "state", CoreState.starting):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    # Unload immediately (before started event fires)
    # This exercises the _safe_unsub path that catches ValueError
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_remove(config_entry.entry_id)
