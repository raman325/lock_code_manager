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
    ATTR_ACTIVE,
    ATTR_CALENDAR,
    ATTR_CALENDAR_ACTIVE,
    ATTR_CALENDAR_END_TIME,
    ATTR_CALENDAR_NEXT_START,
    ATTR_CALENDAR_NEXT_SUMMARY,
    ATTR_CALENDAR_SUMMARY,
    ATTR_CODE,
    ATTR_CODE_LENGTH,
    ATTR_CODE_SLOT,
    ATTR_CONDITION_ENTITY,
    ATTR_CONDITION_ENTITY_DOMAIN,
    ATTR_CONDITION_ENTITY_ID,
    ATTR_CONDITION_ENTITY_NAME,
    ATTR_CONDITION_ENTITY_STATE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_IN_SYNC,
    ATTR_LAST_USED,
    ATTR_LAST_USED_LOCK,
    ATTR_LOCK_ENTITY_ID,
    ATTR_LOCK_NAME,
    ATTR_MANAGED,
    ATTR_PIN_LENGTH,
    ATTR_SCHEDULE,
    ATTR_SCHEDULE_NEXT_EVENT,
    ATTR_SLOT,
    ATTR_SLOT_NUM,
    ATTR_SYNC_STATUS,
    ATTR_USERCODE,
    CONF_CONDITIONS,
    CONF_CONFIG_ENTRY,
    CONF_ENABLED,
    CONF_ENTITIES,
    CONF_LOCKS,
    CONF_NAME,
    CONF_NUMBER_OF_USES,
    CONF_PIN,
    CONF_SLOTS,
)
from custom_components.lock_code_manager.exceptions import DuplicateCodeError
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers import BaseLock
from custom_components.lock_code_manager.websocket import (
    SlotEntities,
    _find_config_entry_by_title,
    _get_bool_state,
    _get_condition_entity_data,
    _get_last_changed,
    _get_next_calendar_event,
    _get_number_state,
    _get_slot_condition_entity_id,
    _get_slot_entity_data,
    _get_slot_state_entity_ids,
    _get_text_state,
    _serialize_slot,
)

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_PIN_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
            ATTR_CONFIG_ENTRY_TITLE: "mock-title",
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
            ATTR_CONFIG_ENTRY_ID: "fake_entry_id",
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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

    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
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


async def test_subscribe_lock_codes_sync_status(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test sync_status appears in subscribe_lock_codes when suspended."""
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

    # Initial event should not have sync_status
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert ATTR_SYNC_STATUS not in event["event"]

    # Suspend sync managers
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.coordinator.suspend_slot_sync_mgrs()
    await hass.async_block_till_done()

    # Updated event should have sync_status == "suspended"
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][ATTR_SYNC_STATUS] == "suspended"


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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 999,
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_set_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_usercode websocket command for setting a code."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/set_usercode",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "9999",
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"]["success"] is True


async def test_clear_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test clear_usercode websocket command for clearing a code."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/clear_usercode",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    assert msg["result"]["success"] is True


async def test_set_usercode_lock_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_usercode websocket command with invalid lock entity ID."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/set_usercode",
            ATTR_LOCK_ENTITY_ID: "lock.nonexistent",
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "1234",
        }
    )
    msg = await ws_client.receive_json()
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"


async def test_clear_usercode_lock_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test clear_usercode websocket command with invalid lock entity ID."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/clear_usercode",
            ATTR_LOCK_ENTITY_ID: "lock.nonexistent",
            ATTR_CODE_SLOT: 3,
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
            ATTR_CONFIG_ENTRY_TITLE: "mock-title",
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
            ATTR_CONFIG_ENTRY_TITLE: "nonexistent-title",
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
            ATTR_CONFIG_ENTRY_TITLE: "nonexistent-title",
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
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
    """
    Test event_type attribute is set correctly after firing code slot event.

    This verifies the event entity state has the event_type attribute set to
    the lock entity ID, which the websocket uses to look up last_used_lock_name.
    """
    ws_client = await hass_ws_client(hass)

    # First, subscribe before any event - last_used should be None
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
    assert data.get(ATTR_LAST_USED) is None
    assert data.get(ATTR_LAST_USED_LOCK) is None

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
    assert data.get(ATTR_LAST_USED) is not None


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
    assert slot_1.get(ATTR_MANAGED) is True


async def test_set_usercode_operation_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_usercode websocket command when operation fails."""
    ws_client = await hass_ws_client(hass)

    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    with patch.object(
        lock,
        "async_internal_set_usercode",
        AsyncMock(side_effect=Exception("Test error")),
    ):
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_usercode",
                ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                ATTR_CODE_SLOT: 3,
                ATTR_USERCODE: "1234",
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "unknown_error"
        assert "Test error" in msg["error"]["message"]


async def test_clear_usercode_operation_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test clear_usercode websocket command when operation fails."""
    ws_client = await hass_ws_client(hass)

    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    with patch.object(
        lock,
        "async_internal_clear_usercode",
        AsyncMock(side_effect=Exception("Clear failed")),
    ):
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/clear_usercode",
                ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                ATTR_CODE_SLOT: 3,
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "unknown_error"
        assert "Clear failed" in msg["error"]["message"]


async def test_set_usercode_duplicate_code_error(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test set_usercode websocket command returns error when duplicate code detected."""
    ws_client = await hass_ws_client(hass)

    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    with patch.object(
        lock,
        "async_internal_set_usercode",
        AsyncMock(
            side_effect=DuplicateCodeError(
                code_slot=3,
                conflicting_slot=7,
                conflicting_slot_managed=False,
                lock_entity_id=LOCK_1_ENTITY_ID,
            )
        ),
    ):
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_usercode",
                ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                ATTR_CODE_SLOT: 3,
                ATTR_USERCODE: "1234",
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert msg["error"]["code"] == "unknown_error"
        assert "duplicate" in msg["error"]["message"].lower()
        assert "slot 7" in msg["error"]["message"]


# =============================================================================
# Full Vertical Flow Tests
# =============================================================================


async def test_pin_set_via_service_reflects_in_subscribe_code_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that setting a PIN via service call is reflected in the subscription."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1 with reveal=True
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event with original PIN
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][CONF_PIN] == "1234"

    # Set a new PIN via service call
    await hass.services.async_call(
        "text",
        "set_value",
        {"value": "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Force coordinator refresh to pick up new code from mock lock
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    await lock.coordinator.async_refresh()
    await hass.async_block_till_done()

    # Collect WebSocket updates until we see the new PIN
    result = {"pin": None}

    async def _wait_for_pin() -> None:
        for _ in range(10):
            msg = await ws_client.receive_json()
            if msg.get("type") == "event":
                result["pin"] = msg["event"].get(CONF_PIN)
                if result["pin"] == "9999":
                    return

    try:
        await asyncio.wait_for(_wait_for_pin(), timeout=3.0)
    except TimeoutError:
        pass

    assert result["pin"] == "9999"


async def test_pin_clear_via_service_reflects_in_subscribe_code_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that clearing a PIN via service call is reflected in the subscription.

    The integration rejects clearing a PIN on an enabled slot, so we must
    disable the slot first before clearing.
    """
    ws_client = await hass_ws_client(hass)

    # Disable the slot first so the PIN can be cleared
    hass.states.async_set(SLOT_1_ENABLED_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    # Subscribe to slot 1 with reveal=True
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event with original PIN
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][CONF_PIN] == "1234"

    # Clear the PIN by setting empty value (allowed because slot is disabled)
    await hass.services.async_call(
        "text",
        "set_value",
        {"value": ""},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Force coordinator refresh
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    await lock.coordinator.async_refresh()
    await hass.async_block_till_done()

    # Collect WebSocket updates until we see PIN is None
    result = {"pin": "not_none_sentinel"}

    async def _wait_for_cleared_pin() -> None:
        for _ in range(10):
            msg = await ws_client.receive_json()
            if msg.get("type") == "event":
                result["pin"] = msg["event"].get(CONF_PIN)
                if result["pin"] is None:
                    return

    try:
        await asyncio.wait_for(_wait_for_cleared_pin(), timeout=3.0)
    except TimeoutError:
        pass

    assert result["pin"] is None


async def test_enable_toggle_reflects_in_subscribe_code_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that toggling the enabled switch is reflected in the subscription."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1 with reveal=True
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial event; enabled should be True
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][CONF_ENABLED] is True

    # Turn enabled switch OFF
    hass.states.async_set(SLOT_1_ENABLED_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][CONF_ENABLED] is False

    # Turn enabled switch back ON
    hass.states.async_set(SLOT_1_ENABLED_ENTITY, STATE_ON)
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][CONF_ENABLED] is True


async def test_coordinator_push_update_reflects_in_subscribe_lock_codes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that a coordinator push update is reflected in subscribe_lock_codes."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to lock 1 with reveal=True
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

    # Push coordinator update with new code for slot 1
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.coordinator.push_update({1: "9999"})
    await hass.async_block_till_done()

    # Receive WebSocket update and verify slot 1 has code "9999"
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    slots_by_num = {s[ATTR_SLOT]: s for s in updated["event"][CONF_SLOTS]}
    assert slots_by_num[1][ATTR_CODE] == "9999"


async def test_set_usercode_reflects_in_subscribe_lock_codes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that set_usercode for an unmanaged slot appears in subscribe_lock_codes."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to lock 1 with reveal=True
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

    # Set usercode on unmanaged slot 3 via WebSocket command
    await ws_client.send_json(
        {
            "id": 2,
            "type": "lock_code_manager/set_usercode",
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "7777",
        }
    )
    set_msg = await ws_client.receive_json()
    assert set_msg["success"]

    # Force coordinator refresh to pick up the new code
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    await lock.coordinator.async_refresh()
    await hass.async_block_till_done()

    # Receive WebSocket update and verify slot 3 appears with code "7777"
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    slots_by_num = {s[ATTR_SLOT]: s for s in updated["event"][CONF_SLOTS]}
    assert 3 in slots_by_num
    assert slots_by_num[3][ATTR_CODE] == "7777"
    assert slots_by_num[3][ATTR_MANAGED] is False


async def test_set_slot_condition_reflects_in_subscribe_code_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that updating a slot condition is visible in a new subscription.

    The subscribe_code_slot handler resolves tracked entities at subscription
    time, so a condition added after subscribing requires a new subscription
    to be tracked. This test verifies the full round-trip: set_slot_condition
    persists the condition, then a fresh subscription includes it.
    """
    ws_client = await hass_ws_client(hass)

    # Verify slot 1 initially has no condition
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][CONF_CONDITIONS] == {}

    # Create a binary sensor entity for the condition
    condition_entity_id = "binary_sensor.test_condition"
    hass.states.async_set(
        condition_entity_id,
        STATE_ON,
        {"friendly_name": "Test Condition"},
    )
    await hass.async_block_till_done()

    # Call set_slot_condition WebSocket command to set condition entity
    await ws_client.send_json(
        {
            "id": 2,
            "type": "lock_code_manager/set_slot_condition",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "entity_id": condition_entity_id,
        }
    )
    condition_msg = await ws_client.receive_json()
    assert condition_msg["success"]
    await hass.async_block_till_done()

    # Open a new subscription to get the updated condition data
    await ws_client.send_json(
        {
            "id": 3,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    conditions = event["event"][CONF_CONDITIONS]
    # The condition_entity key should contain the condition entity data
    assert ATTR_CONDITION_ENTITY in conditions
    assert (
        conditions[ATTR_CONDITION_ENTITY][ATTR_CONDITION_ENTITY_ID]
        == condition_entity_id
    )


# =============================================================================
# API Contract Shape Tests
# =============================================================================


async def test_subscribe_code_slot_response_shape(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that subscribe_code_slot response matches the frontend TypeScript contract."""
    ws_client = await hass_ws_client(hass)

    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    event = await ws_client.receive_json()
    assert event["type"] == "event"
    data = event["event"]

    # Matches SlotCardData interface in frontend types.
    expected_keys = {
        ATTR_SLOT_NUM,
        ATTR_CONFIG_ENTRY_ID,
        ATTR_CONFIG_ENTRY_TITLE,
        CONF_NAME,
        CONF_ENABLED,
        ATTR_ACTIVE,
        CONF_ENTITIES,
        CONF_LOCKS,
        CONF_CONDITIONS,
        CONF_PIN,
    }
    assert expected_keys.issubset(data.keys()), (
        f"Missing keys: {expected_keys - data.keys()}"
    )

    # Matches SlotCardEntities interface in frontend types.
    entities = data[CONF_ENTITIES]
    expected_entity_keys = {
        ATTR_ACTIVE,
        CONF_ENABLED,
        CONF_NAME,
        CONF_NUMBER_OF_USES,
        CONF_PIN,
    }
    assert expected_entity_keys == set(entities.keys())

    # Matches SlotCardLockStatus interface in frontend types.
    assert len(data[CONF_LOCKS]) > 0
    for lock_data in data[CONF_LOCKS]:
        assert ATTR_ENTITY_ID in lock_data
        assert CONF_NAME in lock_data
        assert ATTR_IN_SYNC in lock_data
        assert ATTR_CODE in lock_data

    # Type assertions mirror the TypeScript types in frontend types.
    assert isinstance(data[ATTR_SLOT_NUM], int)
    assert isinstance(data[CONF_NAME], str)
    assert isinstance(data[CONF_ENABLED], bool) or data[CONF_ENABLED] is None
    assert isinstance(data[ATTR_ACTIVE], bool) or data[ATTR_ACTIVE] is None
    assert isinstance(data[CONF_PIN], str) or data[CONF_PIN] is None


async def test_subscribe_lock_codes_response_shape(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that subscribe_lock_codes response matches the LockCoordinatorData contract."""
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
    data = event["event"]

    # Matches LockCoordinatorData interface in frontend types.
    assert isinstance(data[ATTR_LOCK_ENTITY_ID], str)
    assert isinstance(data[ATTR_LOCK_NAME], str)
    assert isinstance(data[CONF_SLOTS], list)

    # Matches LockCoordinatorSlotData interface in frontend types.
    assert len(data[CONF_SLOTS]) > 0
    for slot in data[CONF_SLOTS]:
        assert isinstance(slot[ATTR_SLOT], int)
        assert isinstance(slot[ATTR_CODE], str) or slot[ATTR_CODE] is None
        assert isinstance(slot.get(CONF_NAME, ""), str)
        assert isinstance(slot[ATTR_MANAGED], bool)

        # Managed slots also have active, enabled, and config_entry_id
        if slot[ATTR_MANAGED]:
            assert (
                isinstance(slot.get(ATTR_ACTIVE), bool) or slot.get(ATTR_ACTIVE) is None
            )
            assert (
                isinstance(slot.get(CONF_ENABLED), bool)
                or slot.get(CONF_ENABLED) is None
            )
            assert isinstance(slot.get(ATTR_CONFIG_ENTRY_ID), str)


async def test_subscribe_lock_codes_masked_shape_contract(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that masked subscribe_lock_codes response hides codes and provides code_length."""
    ws_client = await hass_ws_client(hass)

    # Subscribe with reveal=False (default) for masked codes
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

    # Each slot should have code=None and code_length as an integer
    for slot in event["event"][CONF_SLOTS]:
        assert slot[ATTR_CODE] is None
        assert isinstance(slot[ATTR_CODE_LENGTH], int)


# =============================================================================
# Tests for helper functions
# =============================================================================


class TestGetTextState:
    """Tests for _get_text_state helper."""

    @pytest.mark.parametrize(
        ("entity_id", "state_value", "expected"),
        [
            pytest.param("text.test", "hello", "hello", id="valid-state"),
            pytest.param("text.test", STATE_UNKNOWN, None, id="unknown"),
            pytest.param("text.test", STATE_UNAVAILABLE, None, id="unavailable"),
            pytest.param("text.nonexistent", None, None, id="nonexistent"),
            pytest.param(None, None, None, id="none-entity-id"),
        ],
    )
    async def test_get_text_state(
        self,
        hass: HomeAssistant,
        entity_id: str | None,
        state_value: str | None,
        expected: str | None,
    ) -> None:
        """Test _get_text_state for various inputs."""
        if entity_id is not None and state_value is not None:
            hass.states.async_set(entity_id, state_value)
        result = _get_text_state(hass, entity_id)
        assert result == expected


class TestGetBoolState:
    """Tests for _get_bool_state helper."""

    @pytest.mark.parametrize(
        ("entity_id", "state_value", "expected"),
        [
            pytest.param("switch.test", STATE_ON, True, id="on"),
            pytest.param("switch.test", STATE_OFF, False, id="off"),
            pytest.param("switch.test", STATE_UNKNOWN, None, id="unknown"),
            pytest.param("switch.test", STATE_UNAVAILABLE, None, id="unavailable"),
            pytest.param("switch.nonexistent", None, None, id="nonexistent"),
            pytest.param(None, None, None, id="none-entity-id"),
        ],
    )
    async def test_get_bool_state(
        self,
        hass: HomeAssistant,
        entity_id: str | None,
        state_value: str | None,
        expected: bool | None,
    ) -> None:
        """Test _get_bool_state for various inputs."""
        if entity_id is not None and state_value is not None:
            hass.states.async_set(entity_id, state_value)
        assert _get_bool_state(hass, entity_id) is expected


class TestGetNumberState:
    """Tests for _get_number_state helper."""

    @pytest.mark.parametrize(
        ("entity_id", "state_value", "expected"),
        [
            pytest.param("number.test", "42", 42, id="integer"),
            pytest.param("number.test", "3.14", 3, id="float-to-integer"),
            pytest.param("number.test", "not_a_number", None, id="invalid"),
            pytest.param("number.test", STATE_UNKNOWN, None, id="unknown"),
            pytest.param("number.test", STATE_UNAVAILABLE, None, id="unavailable"),
            pytest.param(None, None, None, id="none-entity-id"),
        ],
    )
    async def test_get_number_state(
        self,
        hass: HomeAssistant,
        entity_id: str | None,
        state_value: str | None,
        expected: int | None,
    ) -> None:
        """Test _get_number_state for various inputs."""
        if entity_id is not None and state_value is not None:
            hass.states.async_set(entity_id, state_value)
        assert _get_number_state(hass, entity_id) == expected


class TestGetLastChanged:
    """Tests for _get_last_changed helper."""

    async def test_returns_iso_timestamp(self, hass: HomeAssistant) -> None:
        """Test returns ISO timestamp for valid entity."""
        hass.states.async_set("sensor.test", "value")
        result = _get_last_changed(hass, "sensor.test")
        assert result is not None
        # Should be a valid ISO format string
        datetime.fromisoformat(result)

    @pytest.mark.parametrize(
        ("entity_id", "state_value", "require_valid_state", "expect_none"),
        [
            pytest.param("sensor.nonexistent", None, False, True, id="nonexistent"),
            pytest.param(None, None, False, True, id="none-entity-id"),
            pytest.param(
                "sensor.test", STATE_UNKNOWN, True, True, id="require-valid-unknown"
            ),
            pytest.param(
                "sensor.test",
                STATE_UNAVAILABLE,
                True,
                True,
                id="require-valid-unavailable",
            ),
            pytest.param(
                "sensor.test", "valid_value", True, False, id="require-valid-allows"
            ),
        ],
    )
    async def test_get_last_changed(
        self,
        hass: HomeAssistant,
        entity_id: str | None,
        state_value: str | None,
        require_valid_state: bool,
        expect_none: bool,
    ) -> None:
        """Test _get_last_changed for various inputs."""
        if entity_id is not None and state_value is not None:
            hass.states.async_set(entity_id, state_value)
        result = _get_last_changed(
            hass, entity_id, require_valid_state=require_valid_state
        )
        if expect_none:
            assert result is None
        else:
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
    """
    Tests for _get_next_calendar_event helper.

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
            assert result[ATTR_CALENDAR_NEXT_START] == "2024-01-15T09:00:00"
            assert result[ATTR_CALENDAR_NEXT_SUMMARY] == "Team Standup"
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
            assert result[ATTR_CALENDAR_NEXT_START] == "2024-01-15T09:00:00"
            assert ATTR_CALENDAR_NEXT_SUMMARY not in result
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
            assert ATTR_CALENDAR_NEXT_START not in result
            assert result[ATTR_CALENDAR_NEXT_SUMMARY] == "Mystery Event"
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


class TestSetSlotCondition:
    """Tests for set_slot_condition websocket command."""

    async def test_set_entity_id(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test setting the condition entity_id for a slot."""
        ws_client = await hass_ws_client(hass)

        # Create a test entity
        hass.states.async_set(
            BINARY_SENSOR_TEST_ENTITY_ID, STATE_ON, {"friendly_name": "Test Sensor"}
        )
        await hass.async_block_till_done()

        # Set slot 1's entity_id
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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

    async def test_invalid_slot(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test error when slot doesn't exist."""
        ws_client = await hass_ws_client(hass)

        hass.states.async_set(
            BINARY_SENSOR_TEST_ENTITY_ID, STATE_ON, {"friendly_name": "Test Sensor"}
        )
        await hass.async_block_till_done()

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
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
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": "binary_sensor.nonexistent",
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert "not found" in msg["error"]["message"].lower()

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
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_TITLE: lock_code_manager_config_entry.title,
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
                    "type": "lock_code_manager/set_slot_condition",
                    ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                    "slot": 1,
                    "entity_id": entity_id,
                }
            )
            msg = await ws_client.receive_json()
            assert msg["success"], f"Failed for domain {domain}: {msg}"

    async def test_reject_scheduler_condition_entity(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test that scheduler-component entities are rejected as conditions."""
        ws_client = await hass_ws_client(hass)

        # Create a mock scheduler entity in registry
        ent_reg = er.async_get(hass)
        ent_reg.async_get_or_create(
            "switch",
            "scheduler",  # platform
            "test_schedule",
            suggested_object_id="my_schedule",
        )
        hass.states.async_set("switch.my_schedule", "on")
        await hass.async_block_till_done()

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": "switch.my_schedule",
            }
        )
        result = await ws_client.receive_json()

        assert result["success"] is False
        assert result["error"]["code"] == "not_supported"
        assert "scheduler" in result["error"]["message"]

    async def test_allow_schedule_helper_condition_entity(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test that native schedule helper entities are allowed."""
        ws_client = await hass_ws_client(hass)

        # Create a mock schedule helper entity (native HA)
        ent_reg = er.async_get(hass)
        ent_reg.async_get_or_create(
            "schedule",
            "schedule",  # platform (native helper)
            "work_hours",
            suggested_object_id="work_hours",
        )
        hass.states.async_set("schedule.work_hours", "on")
        await hass.async_block_till_done()

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/set_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                "slot": 1,
                "entity_id": "schedule.work_hours",
            }
        )
        result = await ws_client.receive_json()

        assert result["success"] is True


class TestClearSlotCondition:
    """Tests for clear_slot_condition websocket command."""

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

        # Clear slot 2's entity_id
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/clear_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                "slot": 2,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

        # Verify entity_id was removed from config
        assert "entity_id" not in lock_code_manager_config_entry.data[CONF_SLOTS][2]

    async def test_clear_already_empty(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Test clearing a slot that has no condition entity is a no-op success."""
        ws_client = await hass_ws_client(hass)

        # Slot 1 has no entity_id configured
        assert "entity_id" not in lock_code_manager_config_entry.data[CONF_SLOTS][1]

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/clear_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                "slot": 1,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]

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
                "type": "lock_code_manager/clear_slot_condition",
                ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
                "slot": 999,
            }
        )
        msg = await ws_client.receive_json()
        assert not msg["success"]
        assert "not found" in msg["error"]["message"].lower()


# =============================================================================
# Tests for dynamic entity tracking refresh in WebSocket subscriptions
# =============================================================================


async def test_subscribe_lock_codes_entity_tracking_refreshes_on_update(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that subscribe_lock_codes re-subscribes when tracked entity set changes.

    Verifies the _refresh_lock_state_tracking path where new entities appear
    after the subscription was established (for example, during initial config
    setup when entities may not exist yet).
    """
    ws_client = await hass_ws_client(hass)

    # Get the real entity IDs that will be returned initially
    real_ids = _get_slot_state_entity_ids(hass, LOCK_1_ENTITY_ID)

    # Create a synthetic new entity that will appear on the second call
    new_entity_id = "switch.mock_title_code_slot_99_enabled"
    hass.states.async_set(new_entity_id, STATE_ON)
    await hass.async_block_till_done()

    counter = {"calls": 0}

    def _mock_get_slot_state_entity_ids(hass_arg, lock_entity_id_arg):
        """Return growing entity set to simulate entities appearing."""
        counter["calls"] += 1
        if counter["calls"] <= 1:
            # First call (initial setup): return real entity IDs
            return real_ids
        # Subsequent calls: include the new entity
        return [*real_ids, new_entity_id]

    with patch(
        "custom_components.lock_code_manager.websocket._get_slot_state_entity_ids",
        side_effect=_mock_get_slot_state_entity_ids,
    ):
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

        # Receive initial event (which calls _send_update -> _refresh_lock_state_tracking
        # with the expanded set)
        event = await ws_client.receive_json()
        assert event["type"] == "event"

    # The refresh happened during the initial _send_update call.
    # Now the new entity should be tracked. Verify by changing its state
    # and checking that a WS update is received.
    hass.states.async_set(new_entity_id, STATE_OFF)
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][ATTR_LOCK_ENTITY_ID] == LOCK_1_ENTITY_ID


async def test_subscribe_lock_codes_tracking_refresh_noop_when_unchanged(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that _refresh_lock_state_tracking is a no-op when entity set is unchanged.

    When the tracked entity set has not changed between updates, the refresh
    should return early without re-subscribing. This verifies the early-return
    branch in _refresh_lock_state_tracking.
    """
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

    # Receive initial event
    event = await ws_client.receive_json()
    assert event["type"] == "event"

    # Trigger a coordinator update - _send_update calls _refresh_lock_state_tracking
    # with the same entity set, so it should be a no-op (early return)
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.coordinator.push_update({1: "9999"})
    await hass.async_block_till_done()

    # Should still get the coordinator update event
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"

    # Verify state tracking still works (entity state change produces update)
    enabled_entity_id = "switch.mock_title_code_slot_1_enabled"
    hass.states.async_set(enabled_entity_id, STATE_OFF)
    await hass.async_block_till_done()

    updated2 = await ws_client.receive_json()
    assert updated2["type"] == "event"


async def test_subscribe_code_slot_entity_tracking_refreshes_on_update(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that subscribe_code_slot re-subscribes when tracked entity set changes.

    Verifies the _refresh_state_tracking path in subscribe_code_slot where
    new entities appear after the subscription was established.
    """
    ws_client = await hass_ws_client(hass)

    # Get real entity data for the initial call
    real_entity_data = _get_slot_entity_data(hass, lock_code_manager_config_entry, 1)

    # Create a synthetic new entity that will appear on subsequent calls
    new_entity_id = "text.mock_title_code_slot_1_extra"
    hass.states.async_set(new_entity_id, "test_value")
    await hass.async_block_till_done()

    counter = {"calls": 0}

    def _mock_get_slot_entity_data(hass_arg, config_entry_arg, slot_num_arg):
        """Return growing entity data to simulate entities appearing."""
        counter["calls"] += 1
        if counter["calls"] <= 1:
            return real_entity_data
        # Return entity data with the new entity added via name_entity_id
        # (using a new SlotEntities with an extra entity)
        return SlotEntities(
            slot_num=real_entity_data.slot_num,
            name_entity_id=new_entity_id,
            pin_entity_id=real_entity_data.pin_entity_id,
            enabled_entity_id=real_entity_data.enabled_entity_id,
            active_entity_id=real_entity_data.active_entity_id,
            number_of_uses_entity_id=real_entity_data.number_of_uses_entity_id,
            event_entity_id=real_entity_data.event_entity_id,
        )

    with patch(
        "custom_components.lock_code_manager.websocket._get_slot_entity_data",
        side_effect=_mock_get_slot_entity_data,
    ):
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

        # Receive initial event (triggers _send_update -> _refresh_state_tracking
        # with the expanded entity set)
        event = await ws_client.receive_json()
        assert event["type"] == "event"

    # The refresh happened during the initial _send_update.
    # Now the new entity should be tracked. Verify by changing its state.
    hass.states.async_set(new_entity_id, "new_value")
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][ATTR_SLOT_NUM] == 1


async def test_subscribe_code_slot_calendar_condition_state_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that calendar condition entity state changes trigger async calendar fetch.

    When a calendar entity that is the condition entity for a slot changes state,
    the _on_state_change handler should call _async_send_update_with_calendar
    which re-resolves the condition entity and fetches the next calendar event.
    """
    ws_client = await hass_ws_client(hass)

    # Register a mock calendar.get_events service for the async path
    async def mock_get_events(call):
        entity_id = call.data.get(ATTR_ENTITY_ID)
        if isinstance(entity_id, list):
            entity_id = entity_id[0] if entity_id else None
        return {
            entity_id: {
                "events": [
                    {
                        "start": "2024-01-15T09:00:00",
                        "end": "2024-01-15T10:00:00",
                        "summary": "Test Event",
                    }
                ]
            }
        }

    hass.services.async_register(
        CALENDAR_DOMAIN, SERVICE_GET_EVENTS, mock_get_events, supports_response=True
    )

    try:
        # Subscribe to slot 2 (which has calendar.test_1 as condition)
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

        # Receive initial event
        event = await ws_client.receive_json()
        assert event["type"] == "event"
        assert event["event"][ATTR_SLOT_NUM] == 2

        # Change the calendar condition entity state to trigger the calendar
        # async path in _on_state_change
        hass.states.async_set("calendar.test_1", STATE_ON, {"friendly_name": "Test 1"})
        await hass.async_block_till_done()

        # Should receive an update through the _async_send_update_with_calendar path
        updated = await ws_client.receive_json()
        assert updated["type"] == "event"
        assert updated["event"][ATTR_SLOT_NUM] == 2
    finally:
        hass.services.async_remove(CALENDAR_DOMAIN, SERVICE_GET_EVENTS)


async def test_subscribe_code_slot_condition_entity_tracked_after_addition(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that a newly added condition entity gets tracked after refresh.

    Subscribe to slot 1 (no condition), then simulate adding a condition
    entity via _resolve_entity_ids returning it on subsequent calls.
    Verify the condition entity state changes trigger websocket updates.
    """
    ws_client = await hass_ws_client(hass)

    # Create a condition entity
    condition_entity_id = "binary_sensor.test_condition"
    hass.states.async_set(
        condition_entity_id, STATE_ON, {"friendly_name": "Test Condition"}
    )
    await hass.async_block_till_done()

    counter = {"calls": 0}

    def _mock_get_condition(config_entry_arg, slot_num_arg):
        """Return None initially, then the condition entity on subsequent calls."""
        counter["calls"] += 1
        if counter["calls"] <= 1:
            return None
        return condition_entity_id

    with patch(
        "custom_components.lock_code_manager.websocket._get_slot_condition_entity_id",
        side_effect=_mock_get_condition,
    ):
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

        # Receive initial event (triggers _send_update -> _refresh_state_tracking
        # which now includes the condition entity)
        event = await ws_client.receive_json()
        assert event["type"] == "event"

    # The condition entity should now be tracked. Change its state.
    hass.states.async_set(
        condition_entity_id, STATE_OFF, {"friendly_name": "Test Condition"}
    )
    await hass.async_block_till_done()

    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][ATTR_SLOT_NUM] == 1


async def test_subscribe_lock_codes_unsub_all_with_empty_state_ref(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that _unsub_all handles empty unsub_state_ref gracefully.

    When no entities were tracked (empty unsub_state_ref), unsubscribing
    should not raise an error. This exercises the `if unsub_state_ref:` guard.
    """
    ws_client = await hass_ws_client(hass)

    # Mock _get_slot_state_entity_ids to return empty list so
    # unsub_state_ref stays empty
    with patch(
        "custom_components.lock_code_manager.websocket._get_slot_state_entity_ids",
        return_value=[],
    ):
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

    # Unload the config entry, which should trigger _unsub_all.
    # With empty unsub_state_ref, the guard should prevent an IndexError.
    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()


async def test_subscribe_code_slot_unsub_all_with_empty_state_ref(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Test that _unsub_all in subscribe_code_slot handles empty unsub_state_ref.

    When no entities were tracked (empty unsub_state_ref), unsubscribing
    should not raise an error. This exercises the `if unsub_state_ref:` guard.
    """
    ws_client = await hass_ws_client(hass)

    # Mock entity data to return empty entity data so unsub_state_ref stays empty
    empty_entity_data = SlotEntities(slot_num=1)

    with (
        patch(
            "custom_components.lock_code_manager.websocket._get_slot_entity_data",
            return_value=empty_entity_data,
        ),
        patch(
            "custom_components.lock_code_manager.websocket._get_slot_in_sync_entity_ids",
            return_value={},
        ),
        patch(
            "custom_components.lock_code_manager.websocket._get_slot_condition_entity_id",
            return_value=None,
        ),
    ):
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

    # Unload the config entry, which should trigger _unsub_all.
    # With empty unsub_state_ref, the guard should prevent an IndexError.
    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()


# =============================================================================
# _serialize_slot SlotCode tests
# =============================================================================


class TestSerializeSlotWithSlotCode:
    """Test _serialize_slot passes SlotCode sentinels through as strings."""

    def test_empty_code_passes_through(self) -> None:
        """SlotCode.EMPTY should serialize as the string "empty"."""
        result = _serialize_slot(1, SlotCode.EMPTY, reveal=False)
        assert result[ATTR_CODE] == "empty"
        assert ATTR_CODE_LENGTH not in result

    def test_empty_code_revealed_passes_through(self) -> None:
        """SlotCode.EMPTY with reveal=True should still be "empty"."""
        result = _serialize_slot(1, SlotCode.EMPTY, reveal=True)
        assert result[ATTR_CODE] == "empty"

    def test_unreadable_code_passes_through(self) -> None:
        """SlotCode.UNREADABLE_CODE should serialize as the string "unreadable_code"."""
        result = _serialize_slot(1, SlotCode.UNREADABLE_CODE, reveal=False)
        assert result[ATTR_CODE] == "unreadable_code"
        assert ATTR_CODE_LENGTH not in result

    def test_unreadable_code_includes_configured_code_when_revealed(self) -> None:
        """SlotCode.UNREADABLE_CODE with configured_code and reveal should include it."""
        result = _serialize_slot(
            1, SlotCode.UNREADABLE_CODE, reveal=True, configured_code="1234"
        )
        assert result[ATTR_CODE] == "unreadable_code"
        assert result["configured_code"] == "1234"

    def test_unreadable_code_includes_configured_code_length_when_masked(self) -> None:
        """SlotCode.UNREADABLE_CODE without reveal should include configured_code_length."""
        result = _serialize_slot(
            1, SlotCode.UNREADABLE_CODE, reveal=False, configured_code="1234"
        )
        assert result[ATTR_CODE] == "unreadable_code"
        assert result["configured_code_length"] == 4

    def test_regular_code_revealed(self) -> None:
        """Regular string code with reveal=True should include the code."""
        result = _serialize_slot(1, "1234", reveal=True)
        assert result[ATTR_CODE] == "1234"

    def test_regular_code_masked(self) -> None:
        """Regular string code with reveal=False should include code_length."""
        result = _serialize_slot(1, "1234", reveal=False)
        assert result[ATTR_CODE] is None
        assert result[ATTR_CODE_LENGTH] == 4

    def test_none_code(self) -> None:
        """None code should serialize as code=None."""
        result = _serialize_slot(1, None, reveal=False)
        assert result[ATTR_CODE] is None


async def test_subscribe_code_slot_receives_live_updates(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """subscribe_code_slot fires events when entity state changes via service call."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial snapshot
    event = await ws_client.receive_json()
    assert event["type"] == "event"
    assert event["event"][CONF_PIN] == "1234"

    # Change the PIN via service call (text.set_value on the PIN entity)
    await hass.services.async_call(
        "text",
        "set_value",
        {"value": "5555"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Receive websocket event with updated PIN
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"
    assert updated["event"][CONF_PIN] == "5555"


async def test_subscribe_code_slot_receives_coordinator_updates(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """subscribe_code_slot fires events when coordinator data changes."""
    ws_client = await hass_ws_client(hass)

    # Subscribe to slot 1
    await ws_client.send_json(
        {
            "id": 1,
            "type": "lock_code_manager/subscribe_code_slot",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_SLOT: 1,
            "reveal": True,
        }
    )
    msg = await ws_client.receive_json()
    assert msg["success"]

    # Receive initial snapshot
    event = await ws_client.receive_json()
    assert event["type"] == "event"

    # Push new coordinator data
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.coordinator.push_update({1: "new_code"})
    await hass.async_block_till_done()

    # Receive websocket event
    updated = await ws_client.receive_json()
    assert updated["type"] == "event"

    # Verify the lock status in the event shows the new code
    lock_1_data = next(
        (
            lock_data
            for lock_data in updated["event"][CONF_LOCKS]
            if lock_data[ATTR_ENTITY_ID] == LOCK_1_ENTITY_ID
        ),
        None,
    )
    assert lock_1_data is not None
    assert lock_1_data[ATTR_CODE] == "new_code"
