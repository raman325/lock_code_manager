"""Test websockets."""

import asyncio
from datetime import datetime
import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    SERVICE_GET_EVENTS,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CALENDAR,
    ATTR_CALENDAR_ACTIVE,
    ATTR_CALENDAR_END_TIME,
    ATTR_CALENDAR_SUMMARY,
    ATTR_CODE,
    ATTR_CODE_LENGTH,
    ATTR_CODE_SLOT,
    ATTR_CONDITION_ENTITY_DOMAIN,
    ATTR_CONDITION_ENTITY_ID,
    ATTR_CONDITION_ENTITY_NAME,
    ATTR_CONDITION_ENTITY_STATE,
    ATTR_LOCK_ENTITY_ID,
    ATTR_PIN_LENGTH,
    ATTR_SCHEDULE,
    ATTR_SCHEDULE_NEXT_EVENT,
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
from custom_components.lock_code_manager.providers import BaseLock
from custom_components.lock_code_manager.websocket import (
    _find_config_entry_by_title,
    _get_bool_state,
    _get_condition_entity_data,
    _get_last_changed,
    _get_next_calendar_event,
    _get_number_state,
    _get_slot_condition_entity_id,
    _get_text_state,
)

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

_LOGGER = logging.getLogger(__name__)

# Test entity IDs
CALENDAR_TEST_ENTITY_ID = f"{CALENDAR_DOMAIN}.test_cal"
BINARY_SENSOR_TEST_ENTITY_ID = "binary_sensor.test_motion"
SWITCH_TEST_ENTITY_ID = "switch.test_switch"
INPUT_BOOLEAN_TEST_ENTITY_ID = "input_boolean.test_toggle"
SCHEDULE_TEST_ENTITY_ID = "schedule.test_schedule"


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
    hass.states.async_set(enabled_entity_id, STATE_OFF)
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
    hass.states.async_set(enabled_entity_id, STATE_OFF)
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
            if lock_data[ATTR_ENTITY_ID] == LOCK_1_ENTITY_ID
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


async def test_subscribe_code_slot_with_event_type(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test event_type attribute is set correctly after firing code slot event.

    This verifies the event entity state has the event_type attribute set to
    the lock entity ID, which the websocket uses to look up last_used_lock_name.
    """
    ws_client = await hass_ws_client(hass)

    # First, subscribe before any event - last_used should be None
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
    data = event["event"]
    assert data[ATTR_SLOT_NUM] == 2
    # No event fired yet, so last_used should be None
    assert data.get("last_used") is None
    assert data.get("last_used_lock_name") is None

    # Fire a code slot event
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.async_fire_code_slot_event(2, False, "test", Event("zwave_js_notification"))
    await hass.async_block_till_done()

    # Check event entity state to verify event_type is set (this is what
    # websocket code reads to determine last_used_lock_name)
    event_state = hass.states.get("event.mock_title_code_slot_2")
    assert event_state is not None
    assert event_state.state not in ("unknown", "unavailable")
    assert event_state.attributes.get("event_type") == LOCK_1_ENTITY_ID

    # Verify lock state exists and has friendly_name (websocket looks this up)
    lock_state = hass.states.get(LOCK_1_ENTITY_ID)
    assert lock_state is not None
    assert "friendly_name" in lock_state.attributes

    # Should receive websocket update with last_used populated
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    data = updated["event"]
    assert data[ATTR_SLOT_NUM] == 2
    # last_used should have the timestamp from the event
    assert data.get("last_used") is not None


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
        hass.states.async_set("text.test", STATE_UNKNOWN)
        assert _get_text_state(hass, "text.test") is None

    async def test_returns_none_for_unavailable(self, hass: HomeAssistant) -> None:
        """Test returns None for unavailable state."""
        hass.states.async_set("text.test", STATE_UNAVAILABLE)
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
        hass.states.async_set("switch.test", STATE_ON)
        assert _get_bool_state(hass, "switch.test") is True

    async def test_returns_false_for_off(self, hass: HomeAssistant) -> None:
        """Test returns False for 'off' state."""
        hass.states.async_set("switch.test", STATE_OFF)
        assert _get_bool_state(hass, "switch.test") is False

    async def test_returns_none_for_unknown(self, hass: HomeAssistant) -> None:
        """Test returns None for unknown state."""
        hass.states.async_set("switch.test", STATE_UNKNOWN)
        assert _get_bool_state(hass, "switch.test") is None

    async def test_returns_none_for_unavailable(self, hass: HomeAssistant) -> None:
        """Test returns None for unavailable state."""
        hass.states.async_set("switch.test", STATE_UNAVAILABLE)
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
        hass.states.async_set("number.test", STATE_UNKNOWN)
        assert _get_number_state(hass, "number.test") is None

    async def test_returns_none_for_unavailable(self, hass: HomeAssistant) -> None:
        """Test returns None for unavailable state."""
        hass.states.async_set("number.test", STATE_UNAVAILABLE)
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
        hass.states.async_set("sensor.test", STATE_UNKNOWN)
        assert _get_last_changed(hass, "sensor.test", require_valid_state=True) is None

    async def test_require_valid_state_filters_unavailable(
        self, hass: HomeAssistant
    ) -> None:
        """Test require_valid_state=True returns None for unavailable state."""
        hass.states.async_set("sensor.test", STATE_UNAVAILABLE)
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


class TestGetSlotConditionEntityId:
    """Tests for _get_slot_condition_entity_id helper."""

    async def test_returns_entity_for_slot_with_condition(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns condition entity ID for slot with condition configured."""
        # Slot 2 has an entity_id configured in the test fixtures
        entity_id = _get_slot_condition_entity_id(lock_code_manager_config_entry, 2)
        assert entity_id == "calendar.test_1"

    async def test_returns_none_for_slot_without_condition(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns None for slot without condition entity."""
        # Slot 1 has no condition entity configured
        entity_id = _get_slot_condition_entity_id(lock_code_manager_config_entry, 1)
        assert entity_id is None

    async def test_returns_none_for_nonexistent_slot(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test returns None for nonexistent slot."""
        entity_id = _get_slot_condition_entity_id(lock_code_manager_config_entry, 999)
        assert entity_id is None

    async def test_handles_string_slot_keys(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Test handles slot config with string keys."""
        # The config uses string keys internally, test that int lookup works
        entity_id = _get_slot_condition_entity_id(lock_code_manager_config_entry, 2)
        assert entity_id == "calendar.test_1"


class TestGetConditionEntityData:
    """Tests for _get_condition_entity_data helper."""

    async def test_returns_none_for_none_entity_id(self, hass: HomeAssistant) -> None:
        """Test returns None when entity_id is None."""
        result = _get_condition_entity_data(hass, None)
        assert result is None

    async def test_returns_none_for_nonexistent_entity(
        self, hass: HomeAssistant
    ) -> None:
        """Test returns None when entity doesn't exist."""
        result = _get_condition_entity_data(hass, "binary_sensor.nonexistent")
        assert result is None

    async def test_binary_sensor_entity_on(self, hass: HomeAssistant) -> None:
        """Test binary_sensor entity with ON state."""
        hass.states.async_set(
            BINARY_SENSOR_TEST_ENTITY_ID,
            STATE_ON,
            {"friendly_name": "Test Motion Sensor"},
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, BINARY_SENSOR_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_ID] == BINARY_SENSOR_TEST_ENTITY_ID
        assert result[ATTR_CONDITION_ENTITY_DOMAIN] == "binary_sensor"
        assert result[ATTR_CONDITION_ENTITY_STATE] == STATE_ON
        assert result[ATTR_CONDITION_ENTITY_NAME] == "Test Motion Sensor"
        # Binary sensors don't have calendar or schedule data
        assert ATTR_CALENDAR not in result
        assert ATTR_SCHEDULE not in result

    async def test_binary_sensor_entity_off(self, hass: HomeAssistant) -> None:
        """Test binary_sensor entity with OFF state."""
        hass.states.async_set(
            BINARY_SENSOR_TEST_ENTITY_ID,
            STATE_OFF,
            {"friendly_name": "Test Motion Sensor"},
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, BINARY_SENSOR_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_STATE] == STATE_OFF

    async def test_switch_entity(self, hass: HomeAssistant) -> None:
        """Test switch entity."""
        hass.states.async_set(
            SWITCH_TEST_ENTITY_ID,
            STATE_ON,
            {"friendly_name": "Test Switch"},
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, SWITCH_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_ID] == SWITCH_TEST_ENTITY_ID
        assert result[ATTR_CONDITION_ENTITY_DOMAIN] == "switch"
        assert result[ATTR_CONDITION_ENTITY_STATE] == STATE_ON
        assert result[ATTR_CONDITION_ENTITY_NAME] == "Test Switch"

    async def test_input_boolean_entity(self, hass: HomeAssistant) -> None:
        """Test input_boolean entity."""
        hass.states.async_set(
            INPUT_BOOLEAN_TEST_ENTITY_ID,
            STATE_OFF,
            {"friendly_name": "Test Toggle"},
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, INPUT_BOOLEAN_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_ID] == INPUT_BOOLEAN_TEST_ENTITY_ID
        assert result[ATTR_CONDITION_ENTITY_DOMAIN] == "input_boolean"
        assert result[ATTR_CONDITION_ENTITY_STATE] == STATE_OFF

    async def test_calendar_entity_active(self, hass: HomeAssistant) -> None:
        """Test calendar entity with active event (ON state)."""
        hass.states.async_set(
            CALENDAR_TEST_ENTITY_ID,
            STATE_ON,
            {
                "friendly_name": "Test Calendar",
                "message": "Team Meeting",
                "end_time": "2024-01-15T10:00:00",
            },
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, CALENDAR_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_ID] == CALENDAR_TEST_ENTITY_ID
        assert result[ATTR_CONDITION_ENTITY_DOMAIN] == "calendar"
        assert result[ATTR_CONDITION_ENTITY_STATE] == STATE_ON
        # Calendar entities have rich event data
        assert ATTR_CALENDAR in result
        assert result[ATTR_CALENDAR][ATTR_CALENDAR_ACTIVE] is True
        assert result[ATTR_CALENDAR][ATTR_CALENDAR_SUMMARY] == "Team Meeting"
        assert result[ATTR_CALENDAR][ATTR_CALENDAR_END_TIME] == "2024-01-15T10:00:00"

    async def test_calendar_entity_inactive(self, hass: HomeAssistant) -> None:
        """Test calendar entity with no active event (OFF state)."""
        hass.states.async_set(
            CALENDAR_TEST_ENTITY_ID,
            STATE_OFF,
            {"friendly_name": "Test Calendar"},
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, CALENDAR_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_STATE] == STATE_OFF
        assert ATTR_CALENDAR in result
        assert result[ATTR_CALENDAR][ATTR_CALENDAR_ACTIVE] is False
        # No summary or end_time when inactive
        assert ATTR_CALENDAR_SUMMARY not in result[ATTR_CALENDAR]
        assert ATTR_CALENDAR_END_TIME not in result[ATTR_CALENDAR]

    async def test_schedule_entity_with_next_event(self, hass: HomeAssistant) -> None:
        """Test schedule entity with next_event attribute."""
        next_event = datetime(2024, 1, 15, 8, 0, 0)
        hass.states.async_set(
            SCHEDULE_TEST_ENTITY_ID,
            STATE_OFF,
            {
                "friendly_name": "Test Schedule",
                "next_event": next_event,
            },
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, SCHEDULE_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_ID] == SCHEDULE_TEST_ENTITY_ID
        assert result[ATTR_CONDITION_ENTITY_DOMAIN] == "schedule"
        assert ATTR_SCHEDULE in result
        assert ATTR_SCHEDULE_NEXT_EVENT in result[ATTR_SCHEDULE]

    async def test_schedule_entity_without_next_event(
        self, hass: HomeAssistant
    ) -> None:
        """Test schedule entity without next_event attribute."""
        hass.states.async_set(
            SCHEDULE_TEST_ENTITY_ID,
            STATE_ON,
            {"friendly_name": "Test Schedule"},
        )
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, SCHEDULE_TEST_ENTITY_ID)

        assert result is not None
        assert result[ATTR_CONDITION_ENTITY_DOMAIN] == "schedule"
        # No schedule data when next_event is not present
        assert ATTR_SCHEDULE not in result

    async def test_entity_without_friendly_name(self, hass: HomeAssistant) -> None:
        """Test entity without friendly_name attribute."""
        hass.states.async_set("switch.unnamed", STATE_ON, {})
        await hass.async_block_till_done()

        result = _get_condition_entity_data(hass, "switch.unnamed")

        assert result is not None
        assert ATTR_CONDITION_ENTITY_NAME not in result


class TestGetNextCalendarEvent:
    """Tests for _get_next_calendar_event helper.

    These tests register a mock calendar.get_events service to test the
    _get_next_calendar_event function through HA's service system.
    """

    @staticmethod
    def _make_service_handler(response: dict):
        """Create a service handler that returns the given response."""

        async def handler(call):
            entity_id = call.data.get(ATTR_ENTITY_ID)
            if isinstance(entity_id, list):
                entity_id = entity_id[0] if entity_id else None
            return {entity_id: response} if entity_id else response

        return handler

    async def test_returns_next_event_data(self, hass: HomeAssistant) -> None:
        """Test returns next event data when events exist."""
        calendar_entity_id = CALENDAR_TEST_ENTITY_ID

        mock_events = {
            "events": [
                {
                    "start": "2024-01-15T09:00:00",
                    "end": "2024-01-15T10:00:00",
                    "summary": "Team Standup",
                },
                {
                    "start": "2024-01-15T14:00:00",
                    "end": "2024-01-15T15:00:00",
                    "summary": "1:1 Meeting",
                },
            ]
        }

        hass.services.async_register(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            self._make_service_handler(mock_events),
            supports_response=True,
        )

        try:
            result = await _get_next_calendar_event(hass, calendar_entity_id)

            assert result is not None
            assert result["start_time"] == "2024-01-15T09:00:00"
            assert result["summary"] == "Team Standup"
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_returns_none_when_no_events(self, hass: HomeAssistant) -> None:
        """Test returns None when no events in response."""
        calendar_entity_id = CALENDAR_TEST_ENTITY_ID

        hass.services.async_register(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            self._make_service_handler({"events": []}),
            supports_response=True,
        )

        try:
            result = await _get_next_calendar_event(hass, calendar_entity_id)
            assert result is None
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_returns_none_when_calendar_not_in_response(
        self, hass: HomeAssistant
    ) -> None:
        """Test returns None when calendar entity not in response."""

        async def handler(call):
            # Return response keyed to a different entity
            return {"other_calendar": {"events": []}}

        hass.services.async_register(
            CALENDAR_DOMAIN, SERVICE_GET_EVENTS, handler, supports_response=True
        )

        try:
            result = await _get_next_calendar_event(hass, CALENDAR_TEST_ENTITY_ID)
            assert result is None
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_returns_none_when_service_returns_none(
        self, hass: HomeAssistant
    ) -> None:
        """Test returns None when service returns None."""

        async def handler(call):
            return None

        hass.services.async_register(
            CALENDAR_DOMAIN, SERVICE_GET_EVENTS, handler, supports_response=True
        )

        try:
            result = await _get_next_calendar_event(hass, CALENDAR_TEST_ENTITY_ID)
            assert result is None
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_returns_none_on_exception(self, hass: HomeAssistant) -> None:
        """Test returns None when service call raises exception."""

        async def handler(call):
            raise ValueError("Service unavailable")

        hass.services.async_register(
            CALENDAR_DOMAIN, SERVICE_GET_EVENTS, handler, supports_response=True
        )

        try:
            result = await _get_next_calendar_event(hass, CALENDAR_TEST_ENTITY_ID)
            assert result is None
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_handles_event_without_summary(self, hass: HomeAssistant) -> None:
        """Test handles event that has start but no summary."""
        calendar_entity_id = CALENDAR_TEST_ENTITY_ID

        mock_events = {
            "events": [
                {
                    "start": "2024-01-15T09:00:00",
                    "end": "2024-01-15T10:00:00",
                    # No summary field
                }
            ]
        }

        hass.services.async_register(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            self._make_service_handler(mock_events),
            supports_response=True,
        )

        try:
            result = await _get_next_calendar_event(hass, calendar_entity_id)

            assert result is not None
            assert result["start_time"] == "2024-01-15T09:00:00"
            assert "summary" not in result
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_handles_event_without_start(self, hass: HomeAssistant) -> None:
        """Test handles event that has summary but no start."""
        calendar_entity_id = CALENDAR_TEST_ENTITY_ID

        mock_events = {
            "events": [
                {
                    "end": "2024-01-15T10:00:00",
                    "summary": "Mystery Event",
                    # No start field
                }
            ]
        }

        hass.services.async_register(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            self._make_service_handler(mock_events),
            supports_response=True,
        )

        try:
            result = await _get_next_calendar_event(hass, calendar_entity_id)

            assert result is not None
            assert "start_time" not in result
            assert result["summary"] == "Mystery Event"
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)

    async def test_returns_none_for_empty_event(self, hass: HomeAssistant) -> None:
        """Test returns None when event has neither start nor summary."""
        calendar_entity_id = CALENDAR_TEST_ENTITY_ID

        mock_events = {
            "events": [
                {
                    "end": "2024-01-15T10:00:00",
                    # No start or summary
                }
            ]
        }

        hass.services.async_register(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            self._make_service_handler(mock_events),
            supports_response=True,
        )

        try:
            result = await _get_next_calendar_event(hass, calendar_entity_id)
            assert result is None
        finally:
            hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)


class TestUpdateSlotCondition:
    """Tests for update_slot_condition websocket command."""

    async def test_update_entity_id(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test updating the condition entity_id for a slot."""
        ws_client = await hass_ws_client(hass)

        # Create a test entity
        hass.states.async_set(
            BINARY_SENSOR_TEST_ENTITY_ID, STATE_ON, {"friendly_name": "Test Sensor"}
        )
        await hass.async_block_till_done()

        # Update slot 1's entity_id
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": BINARY_SENSOR_TEST_ENTITY_ID,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]
        assert msg["result"]["success"] is True

        # Verify config entry was updated
        assert (
            lock_code_manager_config_entry.data[CONF_SLOTS][1]["entity_id"]
            == BINARY_SENSOR_TEST_ENTITY_ID
        )

    async def test_clear_entity_id(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test clearing the condition entity_id for a slot."""
        ws_client = await hass_ws_client(hass)

        # Slot 2 has a calendar entity configured
        assert "entity_id" in lock_code_manager_config_entry.data[CONF_SLOTS][2]

        # Clear slot 2's entity_id by passing null
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 2,
                "entity_id": None,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Verify entity_id was removed from config
        assert "entity_id" not in lock_code_manager_config_entry.data[CONF_SLOTS][2]

    async def test_update_number_of_uses(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test updating number_of_uses for a slot."""
        ws_client = await hass_ws_client(hass)

        # Update slot 1's number_of_uses
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "number_of_uses": 10,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Verify config entry was updated
        assert (
            lock_code_manager_config_entry.data[CONF_SLOTS][1]["number_of_uses"] == 10
        )

    async def test_clear_number_of_uses(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test clearing number_of_uses for a slot (disables tracking)."""
        ws_client = await hass_ws_client(hass)

        # First set number_of_uses
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "number_of_uses": 5,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Now clear it
        await ws_client.send_json(
            {
                "id": 2,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "number_of_uses": None,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Verify number_of_uses was removed
        assert (
            "number_of_uses" not in lock_code_manager_config_entry.data[CONF_SLOTS][1]
        )

    async def test_update_both_conditions(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test updating both entity_id and number_of_uses in one call."""
        ws_client = await hass_ws_client(hass)

        # Create a test entity
        hass.states.async_set(
            SCHEDULE_TEST_ENTITY_ID, STATE_ON, {"friendly_name": "Test Schedule"}
        )
        await hass.async_block_till_done()

        # Update both conditions for slot 1
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": SCHEDULE_TEST_ENTITY_ID,
                "number_of_uses": 3,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Verify both were updated
        slot_config = lock_code_manager_config_entry.data[CONF_SLOTS][1]
        assert slot_config["entity_id"] == SCHEDULE_TEST_ENTITY_ID
        assert slot_config["number_of_uses"] == 3

    async def test_invalid_slot(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test error when slot doesn't exist."""
        ws_client = await hass_ws_client(hass)

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 999,
                "entity_id": BINARY_SENSOR_TEST_ENTITY_ID,
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert "not found" in msg["error"]["message"].lower()

    async def test_invalid_entity_domain(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test error when entity has unsupported domain."""
        ws_client = await hass_ws_client(hass)

        # Create a sensor entity (not a supported condition domain)
        hass.states.async_set("sensor.temperature", "22.5", {})
        await hass.async_block_till_done()

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": "sensor.temperature",
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert "does not belong to domain" in msg["error"]["message"].lower()

    async def test_nonexistent_entity(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test error when entity doesn't exist."""
        ws_client = await hass_ws_client(hass)

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": "binary_sensor.nonexistent",
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert "not found" in msg["error"]["message"].lower()

    async def test_invalid_number_of_uses(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test error when number_of_uses is not positive."""
        ws_client = await hass_ws_client(hass)

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "number_of_uses": 0,
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert "at least 1" in msg["error"]["message"].lower()

    async def test_with_config_entry_title(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test using config_entry_title instead of config_entry_id."""
        ws_client = await hass_ws_client(hass)

        # Create a test entity
        hass.states.async_set(
            INPUT_BOOLEAN_TEST_ENTITY_ID, STATE_ON, {"friendly_name": "Test Toggle"}
        )
        await hass.async_block_till_done()

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_title": lock_code_manager_config_entry.title,
                "slot": 1,
                "entity_id": INPUT_BOOLEAN_TEST_ENTITY_ID,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Verify update worked
        assert (
            lock_code_manager_config_entry.data[CONF_SLOTS][1]["entity_id"]
            == INPUT_BOOLEAN_TEST_ENTITY_ID
        )

    async def test_all_supported_domains(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test that all supported domains are accepted."""
        ws_client = await hass_ws_client(hass)

        # Test each supported domain
        test_entities = [
            (CALENDAR_TEST_ENTITY_ID, "calendar"),
            (SCHEDULE_TEST_ENTITY_ID, "schedule"),
            (BINARY_SENSOR_TEST_ENTITY_ID, "binary_sensor"),
            (SWITCH_TEST_ENTITY_ID, "switch"),
            (INPUT_BOOLEAN_TEST_ENTITY_ID, "input_boolean"),
        ]

        for entity_id, domain in test_entities:
            # Create the test entity
            hass.states.async_set(
                entity_id, STATE_ON, {"friendly_name": f"Test {domain}"}
            )
            await hass.async_block_till_done()

        # Test each domain
        for i, (entity_id, domain) in enumerate(test_entities):
            await ws_client.send_json(
                {
                    "id": i + 1,
                    "type": "lock_code_manager/update_slot_condition",
                    "config_entry_id": lock_code_manager_config_entry.entry_id,
                    "slot": 1,
                    "entity_id": entity_id,
                }
            )
            msg = await ws_client.receive_json()
            assert msg["success"], f"Failed for domain {domain}: {msg}"

    async def test_number_entity_created_on_add(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test that number entity is created when adding number_of_uses."""
        ws_client = await hass_ws_client(hass)
        ent_reg = er.async_get(hass)
        entry_id = lock_code_manager_config_entry.entry_id

        # Entity ID uses the config entry title (slugified)
        number_entity_id = "number.mock_title_code_slot_1_number_of_uses"

        # Verify no number entity exists for slot 1 before the update
        assert ent_reg.async_get(number_entity_id) is None

        # Add number_of_uses to slot 1
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/update_slot_condition",
                "config_entry_id": entry_id,
                "slot": 1,
                "number_of_uses": 10,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Wait for entity creation
        await hass.async_block_till_done()

        # Verify number entity was created
        entity_entry = ent_reg.async_get(number_entity_id)
        assert entity_entry is not None, "Number entity was not created"
        assert entity_entry.config_entry_id == entry_id

        # Verify entity has the correct state
        state = hass.states.get(number_entity_id)
        assert state is not None, "Number entity state not found"
        assert float(state.state) == 10
