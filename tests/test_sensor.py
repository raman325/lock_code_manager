"""Test sensor platform."""

import logging

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test sensor entity shows lock code values.

    Uses a config without calendar to test pure sensor functionality.
    All slots are active, so codes remain synced and sensors show expected values.
    """
    # Config without calendar - both slots active
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "test2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Sensor", title="Test LCM"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    for code_slot, pin in ((1, "1234"), (2, "5678")):
        state = hass.states.get(f"sensor.test_1_code_slot_{code_slot}")
        assert state
        assert state.state == pin
        state = hass.states.get(f"sensor.test_2_code_slot_{code_slot}")
        assert state
        assert state.state == pin

    await hass.config_entries.async_unload(config_entry.entry_id)
