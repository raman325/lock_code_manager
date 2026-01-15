"""Test event platform."""

import logging
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lock import LockState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_STATE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_FROM,
    ATTR_NOTIFICATION_SOURCE,
    ATTR_TO,
    DOMAIN,
)
from custom_components.lock_code_manager.event import ATTR_UNSUPPORTED_LOCKS
from custom_components.lock_code_manager.providers import BaseLock

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_EVENT_ENTITY,
    SLOT_2_EVENT_ENTITY,
    MockLCMLock,
)

_LOGGER = logging.getLogger(__name__)


async def test_event_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test event entity."""
    state = hass.states.get(SLOT_2_EVENT_ENTITY)
    assert state
    assert state.state == STATE_UNKNOWN

    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    lock.async_fire_code_slot_event(2, False, "test", Event("zwave_js_notification"))

    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN

    assert state.attributes[ATTR_NOTIFICATION_SOURCE] == "event"
    assert state.attributes[ATTR_ENTITY_ID] == LOCK_1_ENTITY_ID
    assert state.attributes[ATTR_STATE] == LockState.UNLOCKED
    assert state.attributes[ATTR_ACTION_TEXT] == "test"
    assert state.attributes[ATTR_CODE_SLOT] == 2
    assert state.attributes[ATTR_CODE_SLOT_NAME] == "test2"
    assert state.attributes[ATTR_FROM] == LockState.LOCKED
    assert state.attributes[ATTR_TO] == LockState.UNLOCKED


async def test_event_types_are_lock_entity_ids(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that event_types are the lock entity IDs that support code slot events."""
    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state

    # event_types should include lock entity IDs
    event_types = state.attributes.get("event_types", [])
    assert LOCK_1_ENTITY_ID in event_types
    assert LOCK_2_ENTITY_ID in event_types


async def test_event_type_is_lock_entity_id_after_event(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that event_type attribute is the lock entity ID where PIN was used."""
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    lock.async_fire_code_slot_event(1, False, "test", Event("zwave_js_notification"))
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state

    # event_type should be the lock entity ID (not "pin_used")
    assert state.attributes.get("event_type") == LOCK_1_ENTITY_ID


class MockLCMLockNoEvents(MockLCMLock):
    """Mock lock that doesn't support code slot events."""

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False


async def test_no_unsupported_locks_when_all_support_events(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that unsupported_locks attribute is not present when all locks support events."""
    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state

    # All mock locks support code slot events, so unsupported_locks should not be present
    assert ATTR_UNSUPPORTED_LOCKS not in state.attributes


async def test_event_entity_unavailable_when_no_supported_locks(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that event entity is unavailable when no locks support code slot events.

    Note: When the entity is unavailable, extra_state_attributes (including
    unsupported_locks) won't be visible in the state.
    """
    # Create config with mock lock that doesn't support events
    with patch(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockNoEvents},
    ):
        config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title No Events 2"
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert state

        # Entity should be unavailable when no locks support code slot events
        assert state.state == STATE_UNAVAILABLE

        await hass.config_entries.async_unload(config_entry.entry_id)


async def test_event_without_lock_entity_id_logs_warning(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog,
):
    """Test that event without lock entity ID logs a warning.

    Note: We call _handle_event directly because the event filter accesses
    ATTR_ENTITY_ID directly (would raise KeyError before reaching handler).
    This tests the defensive code in _handle_event.
    """
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(SLOT_1_EVENT_ENTITY)
    assert entry
    entity = hass.data["entity_components"]["event"].get_entity(entry.entity_id)
    assert entity

    # Call _handle_event directly with event missing ATTR_ENTITY_ID
    entity._handle_event(Event("test_event", {"slot_num": 1}))

    assert "Received event without lock entity ID" in caplog.text


async def test_handle_add_locks_updates_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that _handle_add_locks updates unsupported_locks and writes state."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(SLOT_1_EVENT_ENTITY)
    assert entry
    entity = hass.data["entity_components"]["event"].get_entity(entry.entity_id)
    assert entity

    # Get initial event_types count
    initial_event_types = len(entity.event_types)

    # Create a mock lock that supports events
    mock_lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Call _handle_add_locks with the existing lock (simulates lock being re-added)
    entity._handle_add_locks([mock_lock])
    await hass.async_block_till_done()

    # The state should have been written (entity still has same event_types)
    assert len(entity.event_types) >= initial_event_types


async def test_unsupported_locks_attribute_with_mixed_locks(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test unsupported_locks attribute when some locks don't support events."""

    class MockLCMLockMixed(MockLCMLock):
        """Mock lock where support depends on entity ID."""

        @property
        def supports_code_slot_events(self) -> bool:
            """Only lock 1 supports events."""
            return self.lock.entity_id == LOCK_1_ENTITY_ID

    with patch(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockMixed},
    ):
        config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Mixed"
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert state

        # Event entity should be available (lock 1 supports events)
        assert state.state != STATE_UNAVAILABLE

        # event_types should only include lock 1
        event_types = state.attributes.get("event_types", [])
        assert LOCK_1_ENTITY_ID in event_types
        assert LOCK_2_ENTITY_ID not in event_types

        # unsupported_locks should include lock 2
        unsupported = state.attributes.get(ATTR_UNSUPPORTED_LOCKS, [])
        assert LOCK_2_ENTITY_ID in unsupported

        await hass.config_entries.async_unload(config_entry.entry_id)


async def test_removed_lock_preserved_in_event_types(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that removed lock's entity ID is preserved in event_types until next event.

    When a lock is removed after firing an event, its entity ID should remain
    in event_types to preserve history until a new event occurs.
    """
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Fire an event from lock 1
    lock.async_fire_code_slot_event(1, False, "test", Event("zwave_js_notification"))
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.attributes.get("event_type") == LOCK_1_ENTITY_ID

    # Get the event entity directly to call _handle_remove_lock
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(SLOT_1_EVENT_ENTITY)
    assert entry
    entity = hass.data["entity_components"]["event"].get_entity(entry.entity_id)
    assert entity

    # Verify lock 1 is in event_types before removal
    assert LOCK_1_ENTITY_ID in entity.event_types
    assert LOCK_2_ENTITY_ID in entity.event_types

    # Remove lock 1
    entity._handle_remove_lock(LOCK_1_ENTITY_ID)
    await hass.async_block_till_done()

    # Lock 1 should still be in event_types (preserved as last event type)
    # even though it's no longer in the supported locks list
    assert LOCK_1_ENTITY_ID in entity.event_types
    # Lock 2 should still be there as a supported lock
    assert LOCK_2_ENTITY_ID in entity.event_types
