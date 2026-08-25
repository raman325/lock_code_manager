"""Test the reader provider."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    ATTR_REASON,
    EVENT_CODE_VALIDATION_FAILED,
    EVENT_LOCK_STATE_CHANGED,
    REASON_UNKNOWN_CODE,
)
from custom_components.lock_code_manager.providers.reader import ReaderLock

from .conftest import READER_ENTITY_ID

# Per-user credential_used event entity: entry title slug + user name + key.
SLOT_1_EVENT_ENTITY = "event.mock_title_alice_credential_used"


async def test_valid_code_fires_usage_event(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """A submitted code matching an active slot fires exactly one usage event."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert len(usage_events) == 1
    assert usage_events[0].data[ATTR_CODE_SLOT] == 1
    assert not failure_events


async def test_unknown_code_fires_failure_event(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """A code matching no slot fires exactly one failure event with its reason."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    hass.states.async_set(READER_ENTITY_ID, "0000")
    await hass.async_block_till_done()

    assert len(failure_events) == 1
    assert failure_events[0].data[ATTR_REASON] == REASON_UNKNOWN_CODE
    assert not usage_events


async def test_blank_states_are_ignored(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Empty, unknown, unavailable, and removed states never reach validation."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    # Each transition changes state (the anchor starts on ""), so the
    # listener fires and the guard alone keeps validation out.
    for state in (STATE_UNKNOWN, "", STATE_UNAVAILABLE, ""):
        hass.states.async_set(READER_ENTITY_ID, state)
        await hass.async_block_till_done()

    # Removal delivers a None new state to the listener.
    hass.states.async_remove(READER_ENTITY_ID)
    await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events


async def test_repeated_code_after_clear(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """A keypad that clears its state between submissions can repeat a code."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    for state in ("1234", "", "1234"):
        hass.states.async_set(READER_ENTITY_ID, state)
        await hass.async_block_till_done()

    assert len(usage_events) == 2
    assert all(event.data[ATTR_CODE_SLOT] == 1 for event in usage_events)
    assert not failure_events


async def test_unloaded_entry_stops_validating(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """After the entry unloads, anchor state changes fire no events."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    assert await hass.config_entries.async_unload(lcm_config_entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events


async def test_event_entity_available_for_reader_only_entry(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """
    The credential_used event entity is available with only a reader attached.

    Availability requires at least one lock that supports code slot events;
    a reader must count as one, or reader-only entries ship a permanently
    unavailable event entity.
    """
    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNAVAILABLE


async def test_valid_code_updates_credential_used_entity(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """A valid submission surfaces on the slot's credential_used event entity."""
    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.state == STATE_UNKNOWN

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN
    # The event type is the source lock (here, the reader anchor) entity ID.
    assert state.attributes["event_type"] == READER_ENTITY_ID


async def test_provider_reload_after_unload_does_not_rearm(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    esphome_config_entry: MockConfigEntry,
) -> None:
    """
    A provider reload after LCM unload must not re-arm the reader.

    The base config-entry-state listener watches the anchor's provider
    entry; if unload left it in place, the LOADED transition would re-run
    provider setup against the unloaded LCM entry and every subsequent
    submission would crash on its missing runtime data.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    assert await hass.config_entries.async_unload(lcm_config_entry.entry_id)
    await hass.async_block_till_done()

    esphome_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events


async def test_resetup_does_not_duplicate_validation(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    esphome_config_entry: MockConfigEntry,
) -> None:
    """
    A provider reconnect replaces the anchor listener instead of stacking one.

    The anchor's provider entry reaching LOADED drives the base reconnect
    path, which re-invokes ``async_setup``; a stacked listener would
    validate each submission once per reload.
    """
    esphome_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()

    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert len(usage_events) == 1


async def test_dispatched_end_to_end_as_reader(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """The real setup path instantiates a ReaderLock for the anchor entity."""
    lock = lcm_config_entry.runtime_data.locks[READER_ENTITY_ID]
    assert isinstance(lock, ReaderLock)
    assert lock.domain == "esphome"
