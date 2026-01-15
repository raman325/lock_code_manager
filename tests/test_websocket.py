"""Test websockets."""

import asyncio
from datetime import datetime
import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_CODE_LENGTH,
    ATTR_CODE_SLOT,
    ATTR_LOCK_ENTITY_ID,
    ATTR_PIN_LENGTH,
    ATTR_SLOT,
    ATTR_SLOT_NUM,
    ATTR_USERCODE,
    CONF_CONDITIONS,
    CONF_CONFIG_ENTRY,
    CONF_ENABLED,
    CONF_ENTITIES,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.websocket import (
    _find_config_entry_by_title,
    _get_bool_state,
    _get_last_changed,
    _get_number_state,
    _get_slot_calendar_entity_id,
    _get_text_state,
)

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_get_config_entry_data(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test get_config_entry_data WS API."""
    ws_client = await hass_ws_client(hass)

    # Try API call with entry ID
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/get_config_entry_data",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    result = msg["result"]

    # Verify config_entry
    config_entry = result[CONF_CONFIG_ENTRY]
    assert config_entry["entry_id"] == lock_code_manager_config_entry.entry_id
    assert config_entry["title"] == "Mock Title"

    # Verify entities
    assert len(result[CONF_ENTITIES]) == 19

    # Verify locks (now objects with entity_id and name)
    lock_entity_ids = {lock[ATTR_ENTITY_ID] for lock in result[CONF_LOCKS]}
    assert LOCK_1_ENTITY_ID in lock_entity_ids
    assert LOCK_2_ENTITY_ID in lock_entity_ids
    # Verify name is included
    lock_1 = next(
        lock for lock in result[CONF_LOCKS] if lock[ATTR_ENTITY_ID] == LOCK_1_ENTITY_ID
    )
    assert CONF_NAME in lock_1

    # Verify slots
    assert result[CONF_SLOTS] == {"1": None, "2": "calendar.test_1"}

    # Try API call with entry title
    await ws_client.send_json(
        {
            "id": 2,
            "type": "lock_code_manager/get_config_entry_data",
            "config_entry_title": "mock-title",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert result[CONF_SLOTS] == {"1": None, "2": "calendar.test_1"}

    # Try API call with invalid entry ID
    await ws_client.send_json(
        {
            "id": 3,
            "type": "lock_code_manager/get_config_entry_data",
            "config_entry_id": "fake_entry_id",
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]

    # Try API call without entry title or ID
    await ws_client.send_json(
        {"id": 4, "type": "lock_code_manager/get_config_entry_data"}
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]

    # Unload the entry
    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)

    # Try API call with unloaded entry ID - should fail
    await ws_client.send_json(
        {
            "id": 5,
            "type": "lock_code_manager/get_config_entry_data",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]


async def test_subscribe_lock_codes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_lock_codes WS API."""
    ws_client = await hass_ws_client(hass)

    # Subscribe with reveal=True to get actual codes
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_codes",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][ATTR_LOCK_ENTITY_ID] == LOCK_1_ENTITY_ID

    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    lock.coordinator.push_update({1: "9999"})
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert {
        slot[ATTR_SLOT]: slot[ATTR_CODE] for slot in updated["event"][CONF_SLOTS]
    } == {
        1: "9999",
        2: "5678",
    }


async def test_subscribe_lock_codes_masked(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_lock_codes WS API with masked codes."""
    ws_client = await hass_ws_client(hass)

    # Default (reveal=False) returns masked codes
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_codes",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    # Codes are masked
    for slot in event["event"][CONF_SLOTS]:
        assert slot[ATTR_CODE] is None
        assert slot[ATTR_CODE_LENGTH] == 4


async def test_subscribe_lock_codes_entity_state_change(
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
            "type": "lock_code_manager/subscribe_lock_codes",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
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
    assert updated["event"][ATTR_LOCK_ENTITY_ID] == LOCK_1_ENTITY_ID


async def test_subscribe_lock_codes_ignores_metadata_changes(
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
            "type": "lock_code_manager/subscribe_lock_codes",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
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


async def test_subscribe_code_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot WS API."""
    ws_client = await hass_ws_client(hass)

    # Subscribe with reveal=True to get actual PIN
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"], msg

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    data = event["event"]
    assert data[ATTR_SLOT_NUM] == 1
    assert data[CONF_NAME] == "test1"
    assert data[CONF_PIN] == "1234"
    assert data[CONF_ENABLED] is True
    assert data[CONF_ENTITIES][CONF_PIN]
    assert data[CONF_ENTITIES][CONF_ENABLED]
    assert CONF_LOCKS in data
    assert CONF_CONDITIONS in data


async def test_subscribe_code_slot_masked(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot WS API with masked PIN."""
    ws_client = await hass_ws_client(hass)

    # Default (reveal=False) returns masked PIN
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    data = event["event"]
    assert data[CONF_PIN] is None
    assert data[ATTR_PIN_LENGTH] == 4


async def test_subscribe_code_slot_invalid_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot WS API with invalid slot number."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 999,
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_set_lock_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_lock_usercode WS API for setting a code."""
    ws_client = await hass_ws_client(hass)

    # Set a usercode
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/set_lock_usercode",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "9999",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"]["success"] is True


async def test_set_lock_usercode_clear(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_lock_usercode WS API for clearing a code."""
    ws_client = await hass_ws_client(hass)

    # Clear a usercode (empty string)
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/set_lock_usercode",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"]["success"] is True


async def test_set_lock_usercode_clear_no_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_lock_usercode WS API clears when usercode not provided."""
    ws_client = await hass_ws_client(hass)

    # Clear a usercode (no usercode key provided)
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/set_lock_usercode",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"]["success"] is True


async def test_set_lock_usercode_lock_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_lock_usercode WS API with invalid lock entity ID."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/set_lock_usercode",
            ATTR_LOCK_ENTITY_ID: "lock.nonexistent",
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "1234",
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_subscribe_lock_codes_lock_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_lock_codes WS API with invalid lock entity ID."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_codes",
            ATTR_LOCK_ENTITY_ID: "lock.nonexistent",
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_subscribe_code_slot_state_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot triggers updates on entity state changes."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event
    event = await ws_client.receive_json()
    assert event["type"] == "event"

    # Change an entity state (enabled switch for slot 1)
    enabled_entity_id = "switch.mock_title_code_slot_1_enabled"
    hass.states.async_set(enabled_entity_id, "off")
    await hass.async_block_till_done()

    # Should receive an update event
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][ATTR_SLOT_NUM] == 1
    assert updated["event"][CONF_ENABLED] is False


async def test_subscribe_code_slot_coordinator_update(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot triggers updates on coordinator changes."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    initial_locks = event["event"][CONF_LOCKS]
    assert len(initial_locks) > 0

    # Update the lock coordinator
    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    lock.coordinator.push_update({1: "9999"})
    await hass.async_block_till_done()

    # Should receive an update event with new code
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    # Find lock 1 in the locks data
    lock_1_data = next(
        (
            lock_data
            for lock_data in updated["event"][CONF_LOCKS]
            if lock_data["entity_id"] == LOCK_1_ENTITY_ID
        ),
        None,
    )
    assert lock_1_data is not None
    assert lock_1_data[ATTR_CODE] == "9999"


async def test_subscribe_code_slot_ignores_metadata_changes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot ignores metadata-only state changes."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
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


async def test_subscribe_code_slot_with_title(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot WS API with config entry title."""
    ws_client = await hass_ws_client(hass)

    # Subscribe using config entry title
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_title": "mock-title",
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][ATTR_SLOT_NUM] == 1


async def test_subscribe_code_slot_invalid_title(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot WS API with invalid config entry title."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_title": "nonexistent-title",
            ATTR_SLOT: 1,
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"
    assert "title" in msg["error"]["message"]


async def test_get_config_entry_data_invalid_title(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test get_config_entry_data WS API with invalid config entry title."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/get_config_entry_data",
            "config_entry_title": "nonexistent-title",
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"
    assert "title" in msg["error"]["message"]


async def test_subscribe_code_slot_slot_2_with_calendar(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_code_slot WS API for slot 2 which has a calendar."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 2 (has calendar configured)
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            "config_entry_id": lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 2,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    data = event["event"]
    assert data[ATTR_SLOT_NUM] == 2
    # Slot 2 should have calendar in conditions
    assert CONF_CONDITIONS in data


async def test_subscribe_lock_codes_slot_metadata(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test subscribe_lock_codes includes slot metadata."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_lock_codes",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"

    # Check that slots have metadata
    slots = event["event"][CONF_SLOTS]
    assert len(slots) >= 2

    # Find slot 1 - should have name and be managed
    slot_1 = next((s for s in slots if s[ATTR_SLOT] == 1), None)
    assert slot_1 is not None
    assert slot_1.get(CONF_NAME) == "test1"
    assert slot_1.get("managed") is True


async def test_set_lock_usercode_operation_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_lock_usercode WS API when operation fails."""
    ws_client = await hass_ws_client(hass)

    # Mock the lock's set_usercode to raise an exception
    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    with patch.object(
        lock,
        "async_internal_set_usercode",
        AsyncMock(side_effect=Exception("Test error")),
    ):
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_lock_usercode",
                ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                ATTR_CODE_SLOT: 3,
                ATTR_USERCODE: "1234",
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "unknown_error"
        assert "Test error" in msg["error"]["message"]


async def test_set_lock_usercode_clear_operation_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_lock_usercode WS API when clear operation fails."""
    ws_client = await hass_ws_client(hass)

    # Mock the lock's clear_usercode to raise an exception
    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    with patch.object(
        lock,
        "async_internal_clear_usercode",
        AsyncMock(side_effect=Exception("Clear failed")),
    ):
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_lock_usercode",
                ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                ATTR_CODE_SLOT: 3,
                ATTR_USERCODE: "",  # Empty string triggers clear
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "unknown_error"
        assert "Clear failed" in msg["error"]["message"]


# =============================================================================
# Tests for helper functions
# =============================================================================


class TestGetTextState:
    """Tests for _get_text_state helper."""

    async def test_returns_valid_state(self, hass: HomeAssistant) -> None:
        """Test returns state value for valid entity."""
        hass.states.async_set("text.test", "hello")
        assert _get_text_state(hass, "text.test") == "hello"

    async def test_returns_none_for_unknown(self, hass: HomeAssistant) -> None:
        """Test returns None for unknown state."""
        hass.states.async_set("text.test", "unknown")
        assert _get_text_state(hass, "text.test") is None

    async def test_returns_none_for_unavailable(self, hass: HomeAssistant) -> None:
        """Test returns None for unavailable state."""
        hass.states.async_set("text.test", "unavailable")
        assert _get_text_state(hass, "text.test") is None

    async def test_returns_none_for_nonexistent(self, hass: HomeAssistant) -> None:
        """Test returns None for nonexistent entity."""
        assert _get_text_state(hass, "text.nonexistent") is None

    async def test_returns_none_for_none_entity_id(self, hass: HomeAssistant) -> None:
        """Test returns None when entity_id is None."""
        assert _get_text_state(hass, None) is None


class TestGetBoolState:
    """Tests for _get_bool_state helper."""

    async def test_returns_true_for_on(self, hass: HomeAssistant) -> None:
        """Test returns True for 'on' state."""
        hass.states.async_set("switch.test", "on")
        assert _get_bool_state(hass, "switch.test") is True

    async def test_returns_false_for_off(self, hass: HomeAssistant) -> None:
        """Test returns False for 'off' state."""
        hass.states.async_set("switch.test", "off")
        assert _get_bool_state(hass, "switch.test") is False

    async def test_returns_none_for_unknown(self, hass: HomeAssistant) -> None:
        """Test returns None for unknown state."""
        hass.states.async_set("switch.test", "unknown")
        assert _get_bool_state(hass, "switch.test") is None

    async def test_returns_none_for_unavailable(self, hass: HomeAssistant) -> None:
        """Test returns None for unavailable state."""
        hass.states.async_set("switch.test", "unavailable")
        assert _get_bool_state(hass, "switch.test") is None

    async def test_returns_none_for_nonexistent(self, hass: HomeAssistant) -> None:
        """Test returns None for nonexistent entity."""
        assert _get_bool_state(hass, "switch.nonexistent") is None

    async def test_returns_none_for_none_entity_id(self, hass: HomeAssistant) -> None:
        """Test returns None when entity_id is None."""
        assert _get_bool_state(hass, None) is None


class TestGetNumberState:
    """Tests for _get_number_state helper."""

    async def test_returns_integer(self, hass: HomeAssistant) -> None:
        """Test returns integer for valid number."""
        hass.states.async_set("number.test", "42")
        assert _get_number_state(hass, "number.test") == 42

    async def test_returns_integer_from_float(self, hass: HomeAssistant) -> None:
        """Test converts float to integer."""
        hass.states.async_set("number.test", "3.14")
        assert _get_number_state(hass, "number.test") == 3

    async def test_returns_none_for_invalid(self, hass: HomeAssistant) -> None:
        """Test returns None for non-numeric value."""
        hass.states.async_set("number.test", "not_a_number")
        assert _get_number_state(hass, "number.test") is None

    async def test_returns_none_for_unknown(self, hass: HomeAssistant) -> None:
        """Test returns None for unknown state."""
        hass.states.async_set("number.test", "unknown")
        assert _get_number_state(hass, "number.test") is None

    async def test_returns_none_for_unavailable(self, hass: HomeAssistant) -> None:
        """Test returns None for unavailable state."""
        hass.states.async_set("number.test", "unavailable")
        assert _get_number_state(hass, "number.test") is None

    async def test_returns_none_for_none_entity_id(self, hass: HomeAssistant) -> None:
        """Test returns None when entity_id is None."""
        assert _get_number_state(hass, None) is None


class TestGetLastChanged:
    """Tests for _get_last_changed helper."""

    async def test_returns_iso_timestamp(self, hass: HomeAssistant) -> None:
        """Test returns ISO timestamp for valid entity."""
        hass.states.async_set("sensor.test", "value")
        result = _get_last_changed(hass, "sensor.test")
        assert result is not None
        # Should be a valid ISO format string
        datetime.fromisoformat(result)

    async def test_returns_none_for_nonexistent(self, hass: HomeAssistant) -> None:
        """Test returns None for nonexistent entity."""
        assert _get_last_changed(hass, "sensor.nonexistent") is None

    async def test_returns_none_for_none_entity_id(self, hass: HomeAssistant) -> None:
        """Test returns None when entity_id is None."""
        assert _get_last_changed(hass, None) is None

    async def test_require_valid_state_filters_unknown(
        self, hass: HomeAssistant
    ) -> None:
        """Test require_valid_state=True returns None for unknown state."""
        hass.states.async_set("sensor.test", "unknown")
        assert _get_last_changed(hass, "sensor.test", require_valid_state=True) is None

    async def test_require_valid_state_filters_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """Test require_valid_state=True returns None for unavailable state."""
        hass.states.async_set("sensor.test", "unavailable")
        assert _get_last_changed(hass, "sensor.test", require_valid_state=True) is None

    async def test_require_valid_state_allows_valid(self, hass: HomeAssistant) -> None:
        """Test require_valid_state=True returns timestamp for valid state."""
        hass.states.async_set("sensor.test", "valid_value")
        result = _get_last_changed(hass, "sensor.test", require_valid_state=True)
        assert result is not None


class TestFindConfigEntryByTitle:
    """Tests for _find_config_entry_by_title helper."""

    async def test_finds_entry_by_exact_title(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test finds config entry by exact title."""
        entry = _find_config_entry_by_title(hass, "Mock Title")
        assert entry is not None
        assert entry.entry_id == lock_code_manager_config_entry.entry_id

    async def test_finds_entry_by_slugified_title(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test finds config entry by slugified title match."""
        # "mock-title" should match "Mock Title" after slugification
        entry = _find_config_entry_by_title(hass, "mock-title")
        assert entry is not None
        assert entry.entry_id == lock_code_manager_config_entry.entry_id

    async def test_returns_none_for_nonexistent(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns None for nonexistent title."""
        entry = _find_config_entry_by_title(hass, "nonexistent-title")
        assert entry is None


class TestGetSlotCalendarEntityId:
    """Tests for _get_slot_calendar_entity_id helper."""

    async def test_returns_calendar_for_slot_with_calendar(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns calendar entity ID for slot with calendar configured."""
        # Slot 2 has a calendar configured in the test fixtures
        calendar_id = _get_slot_calendar_entity_id(lock_code_manager_config_entry, 2)
        assert calendar_id == "calendar.test_1"

    async def test_returns_none_for_slot_without_calendar(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns None for slot without calendar."""
        # Slot 1 has no calendar configured
        calendar_id = _get_slot_calendar_entity_id(lock_code_manager_config_entry, 1)
        assert calendar_id is None

    async def test_returns_none_for_nonexistent_slot(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns None for nonexistent slot."""
        calendar_id = _get_slot_calendar_entity_id(lock_code_manager_config_entry, 999)
        assert calendar_id is None

    async def test_handles_string_slot_keys(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test handles slot config with string keys."""
        # The config uses string keys internally, test that int lookup works
        calendar_id = _get_slot_calendar_entity_id(lock_code_manager_config_entry, 2)
        assert calendar_id == "calendar.test_1"
