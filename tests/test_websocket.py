"""Test websockets."""

import logging

from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_get_slot_calendar_data(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test get_slot_calendar_data WS API."""
    ws_client = await hass_ws_client(hass)

    # Try API call with entry ID
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/get_slot_calendar_data",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        CONF_SLOTS: {"1": None, "2": "calendar.test"},
    }

    # Try API call with entry title
    await ws_client.send_json(
        {
            "id": 2,
            "type": "lock_code_manager/get_slot_calendar_data",
            "config_entry_title": "mock-title",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        CONF_SLOTS: {"1": None, "2": "calendar.test"},
    }

    # Try API call with invalid entry ID
    await ws_client.send_json(
        {
            "id": 3,
            "type": "lock_code_manager/get_slot_calendar_data",
            "config_entry_id": "fake_entry_id",
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]

    # Try API call without entry title or ID
    await ws_client.send_json(
        {"id": 4, "type": "lock_code_manager/get_slot_calendar_data"}
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]

    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)

    # Try API call with entry ID
    await ws_client.send_json(
        {
            "id": 5,
            "type": "lock_code_manager/get_slot_calendar_data",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]


async def test_get_config_entry_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test get_config_entry_entities WS API."""
    ws_client = await hass_ws_client(hass)

    # Try API call with entry ID
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/get_config_entry_entities",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    [entry_id, title, entities] = msg["result"]
    assert entry_id == lock_code_manager_config_entry.entry_id
    assert title == "Mock Title"
    assert len(entities) == 15
