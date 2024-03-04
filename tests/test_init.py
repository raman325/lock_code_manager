"""Test init module."""

import copy
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.components.lovelace.const import CONF_RESOURCE_TYPE_WS
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_CODE,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_PIN_SYNCED_TO_LOCKS,
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
    MockLCMLock,
)

_LOGGER = logging.getLogger(__name__)


async def test_entry_setup_and_unload(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test entry setup and unload."""
    lcm_entry_id = lock_code_manager_config_entry.entry_id
    ent_reg = er.async_get(hass)

    unique_ids = set()
    for slot in range(1, 3):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{CONF_CODE}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_PIN_SYNCED_TO_LOCKS,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|2|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 2
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 2
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 4
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 2
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 4

    resources = hass.data[LL_DOMAIN].get("resources")
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
        CONF_CODE: "4321",
        CONF_ENABLED: True,
    }

    assert hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    unique_ids = set()
    for slot in range(1, 4):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{CONF_CODE}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_PIN_SYNCED_TO_LOCKS,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|1|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 3
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

    unique_ids = set()
    for slot in range(1, 3):
        unique_ids.add(f"{lcm_entry_id}|{slot}|{CONF_CODE}|{LOCK_1_ENTITY_ID}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_PIN_SYNCED_TO_LOCKS,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|1|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 2
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
                    hass, {SOURCE_REAUTH, SOURCE_USER}
                )
            ]
        )
        == 1
    )


async def test_resource_already_loaded(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test when strategy resource is already loaded."""
    resources = hass.data[LL_DOMAIN].get("resources")
    assert resources
    await resources.async_load()

    await resources.async_create_item(
        {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: STRATEGY_PATH}
    )
    monkeypatch.setattr(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock},
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert "already registered" in caplog.text

    await hass.config_entries.async_unload(config_entry.entry_id)
