"""Config flow tests."""

import copy
import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    DOMAIN,
)

from .common import BASE_CONFIG


@pytest.fixture(name="bypass_entry_setup", autouse=True)
def bypass_entry_setup_fixture():
    """Bypass config entry setup."""
    with patch(
        "custom_components.lock_code_manager.async_setup_entry", return_value=True
    ):
        yield


async def _start_config_flow(hass: HomeAssistant):
    """Start a config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: ["lock.test"]}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"

    return flow_id


async def _start_ui_config_flow(hass: HomeAssistant):
    """Start a UI based config flow."""
    flow_id = await _start_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "ui"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert not result["last_step"]

    return flow_id


async def _start_yaml_config_flow(hass: HomeAssistant):
    """Start a YAML based config flow."""
    flow_id = await _start_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"

    return flow_id


async def test_config_flow_ui(hass: HomeAssistant):
    """Test UI based config flow."""
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "1234"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert result["last_step"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "5678"}
    )

    assert result["type"] == "create_entry"
    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: ["lock.test"],
        CONF_SLOTS: {
            1: {CONF_ENABLED: True, CONF_PIN: "1234"},
            2: {CONF_ENABLED: True, CONF_PIN: "5678"},
        },
    }


async def test_config_flow_ui_error(hass: HomeAssistant):
    """Test error in UI based config flow."""
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: ""}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert not result["last_step"]
    assert result["errors"] == {CONF_PIN: "missing_pin_if_enabled"}


async def test_config_flow_yaml(hass: HomeAssistant):
    """Test YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_SLOTS: {
                1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                2: {CONF_ENABLED: True, CONF_PIN: "5678"},
            }
        },
    )

    assert result["type"] == "create_entry"
    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: ["lock.test"],
        CONF_SLOTS: {
            1: {CONF_ENABLED: True, CONF_PIN: "1234"},
            2: {CONF_ENABLED: True, CONF_PIN: "5678"},
        },
    }


async def test_config_flow_yaml_error(hass: HomeAssistant):
    """Test YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: ""}}},
    )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"
    assert result["errors"] == {"base": "invalid_config"}


async def test_options_flow(hass: HomeAssistant):
    """Test options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][3] = {CONF_ENABLED: True, CONF_PIN: ""}
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    flow_id = result["flow_id"]

    result = await hass.config_entries.options.async_configure(
        flow_id, user_input=new_config
    )

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "invalid_config"}

    new_config[CONF_SLOTS][3] = {CONF_ENABLED: True, CONF_PIN: "1234"}
    result = await hass.config_entries.options.async_configure(
        flow_id, user_input=new_config
    )

    logging.getLogger(__name__).error(new_config)
    logging.getLogger(__name__).error(result)

    assert result["type"] == "create_entry"
    assert result["data"] == new_config
