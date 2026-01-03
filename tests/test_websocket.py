"""Test websockets."""

import asyncio
import logging

import pytest
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN

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
        CONF_SLOTS: {"1": None, "2": "calendar.test_1"},
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
        CONF_SLOTS: {"1": None, "2": "calendar.test_1"},
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

    # Unload the entry
    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)

    # Try API call with unloaded entry ID - should fail
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

    result = msg["result"]
    config_entry = result["config_entry"]
    assert config_entry["entry_id"] == lock_code_manager_config_entry.entry_id
    assert config_entry["title"] == "Mock Title"
    assert len(result["entities"]) == 19


async def test_subscribe_lock_slot_data(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_lock_slot_data WS API."""
    ws_client = await hass_ws_client(hass)

    # Subscribe with reveal=True to get actual codes
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_slot_data",
            "lock_entity_id": LOCK_1_ENTITY_ID,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"]["lock_entity_id"] == LOCK_1_ENTITY_ID

    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    lock.coordinator.push_update({1: "9999"})
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert {slot["slot"]: slot["code"] for slot in updated["event"]["slots"]} == {
        1: "9999",
        2: "5678",
    }


async def test_subscribe_lock_slot_data_masked(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_lock_slot_data WS API with masked codes."""
    ws_client = await hass_ws_client(hass)

    # Default (reveal=False) returns masked codes
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_slot_data",
            "lock_entity_id": LOCK_1_ENTITY_ID,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    # Codes are masked
    for slot in event["event"]["slots"]:
        assert slot["code"] is None
        assert slot["code_length"] == 4


async def test_subscribe_lock_slot_data_entity_state_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that LCM entity state changes trigger websocket updates."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot data
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_slot_data",
            "lock_entity_id": LOCK_1_ENTITY_ID,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event
    event = await ws_client.receive_json()
    assert event["type"] == "event"

    # Change an LCM entity state (enabled switch for slot 1)
    enabled_entity_id = "switch.mock_title_code_slot_1_enabled"
    hass.states.async_set(enabled_entity_id, "off")
    await hass.async_block_till_done()

    # Should receive an update event due to entity state change
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"]["lock_entity_id"] == LOCK_1_ENTITY_ID


async def test_subscribe_lock_slot_data_ignores_metadata_changes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that metadata-only state changes don't trigger websocket updates."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot data
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_slot_data",
            "lock_entity_id": LOCK_1_ENTITY_ID,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event
    event = await ws_client.receive_json()
    assert event["type"] == "event"

    # Change only metadata (same state value, different attributes)
    enabled_entity_id = "switch.mock_title_code_slot_1_enabled"
    current_state = hass.states.get(enabled_entity_id)
    hass.states.async_set(
        enabled_entity_id,
        current_state.state,  # Same state
        {"updated_attr": "new_value"},  # Different attributes
    )
    await hass.async_block_till_done()

    # Should NOT receive an update (metadata-only change filtered out)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ws_client.receive_json(), timeout=0.1)


async def test_get_locks(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test get_locks WS API."""
    ws_client = await hass_ws_client(hass)

    # Get all locks (no params)
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/get_locks",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    result = msg["result"]
    lock_entity_ids = {lock["entity_id"] for lock in result["locks"]}
    assert LOCK_1_ENTITY_ID in lock_entity_ids
    assert LOCK_2_ENTITY_ID in lock_entity_ids
    # Verify name is included
    lock_1 = next(
        lock for lock in result["locks"] if lock["entity_id"] == LOCK_1_ENTITY_ID
    )
    assert "name" in lock_1

    # Get locks scoped to config entry by ID
    await ws_client.send_json(
        {
            "id": 2,
            "type": "lock_code_manager/get_locks",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    result = msg["result"]
    lock_entity_ids = {lock["entity_id"] for lock in result["locks"]}
    assert LOCK_1_ENTITY_ID in lock_entity_ids
    assert LOCK_2_ENTITY_ID in lock_entity_ids

    # Get locks scoped to config entry by title
    await ws_client.send_json(
        {
            "id": 3,
            "type": "lock_code_manager/get_locks",
            "config_entry_title": "mock-title",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    result = msg["result"]
    lock_entity_ids = {lock["entity_id"] for lock in result["locks"]}
    assert LOCK_1_ENTITY_ID in lock_entity_ids
    assert LOCK_2_ENTITY_ID in lock_entity_ids

    # Get locks with invalid config entry ID returns empty list
    await ws_client.send_json(
        {
            "id": 4,
            "type": "lock_code_manager/get_locks",
            "config_entry_id": "fake_entry_id",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"]["locks"] == []
