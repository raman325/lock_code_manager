"""Test the reader provider."""

from __future__ import annotations

import logging

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_STATE, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_FIRE_EVENTS,
    ATTR_LOCK_ENTITY_ID,
    ATTR_REASON,
    ATTR_USER,
    ATTR_VALID,
    DOMAIN,
    EVENT_CODE_VALIDATION_FAILED,
    EVENT_LOCK_STATE_CHANGED,
    REASON_UNKNOWN_CODE,
    REDACTED,
    SERVICE_VALIDATE_CODE,
)
from custom_components.lock_code_manager.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.lock_code_manager.providers.reader import ReaderLock

from .conftest import READER_ENTITY_ID, SECOND_READER_ENTITY_ID

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


@pytest.mark.parametrize("interrupted_by", [STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_republished_code_after_an_outage_is_not_a_submission(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry, interrupted_by: str
) -> None:
    """
    A keypad restoring its last value after an outage has submitted nothing.

    A Wi-Fi blip, an integration reload, or a Home Assistant restart drops
    the anchor to unavailable or unknown and then republishes whatever it
    last held. Treating that as a press runs the user's "on credential_used,
    open the door" automation with nobody standing there.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    for state in (interrupted_by, "1234"):
        hass.states.async_set(READER_ENTITY_ID, state)
        await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events


async def test_first_state_after_registration_is_not_a_submission(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """An anchor appearing with a value already set has submitted nothing."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    hass.states.async_remove(READER_ENTITY_ID)
    await hass.async_block_till_done()
    hass.states.async_set(READER_ENTITY_ID, "1234")
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
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After the entry unloads, anchor state changes fire no events and no errors."""
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    assert await hass.config_entries.async_unload(lcm_config_entry.entry_id)
    await hass.async_block_till_done()

    caplog.clear()
    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events
    # A still-armed listener would not reach the event assertions: its crash
    # against the unloaded entry is caught and logged by the state-change
    # dispatcher, so only the log shows it.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


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
    caplog: pytest.LogCaptureFixture,
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

    caplog.clear()
    esphome_config_entry.mock_state(hass, ConfigEntryState.LOADED)
    await hass.async_block_till_done()

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events
    # A re-armed reader's crash on the unloaded entry surfaces only as a
    # logged error: the state-change dispatcher catches it before it can
    # fail the event assertions.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


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


async def test_success_event_never_carries_the_submitted_code(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """
    A valid submission's usage event redacts the anchor's state.

    For an ordinary lock the anchor state is locked/unlocked; for a reader
    it is the credential that was just typed, and the usage event lands on
    the bus, in the recorder, and in the event entity's attributes.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    assert len(usage_events) == 1
    event_data = usage_events[0].data
    assert event_data[ATTR_STATE] == REDACTED
    assert "1234" not in event_data.values()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.attributes[ATTR_STATE] == REDACTED
    assert "1234" not in state.attributes.values()


async def test_validates_against_every_entry_sharing_the_anchor(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    second_lcm_config_entry: MockConfigEntry,
) -> None:
    """
    A PIN from either entry sharing the anchor validates.

    The BaseLock instance is shared, so only the first entry ever ran
    provider setup; a reader that validated against that entry alone would
    reject every credential belonging to the second one.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    for code in ("1234", "", "9999"):
        hass.states.async_set(READER_ENTITY_ID, code)
        await hass.async_block_till_done()

    assert [event.data[ATTR_CODE_SLOT] for event in usage_events] == [1, 3]
    assert not failure_events


async def test_second_entry_still_validates_after_first_unloads(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    second_lcm_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Unloading the entry that set the reader up leaves the other one working.

    The shared instance survives the unload because another entry still
    manages the anchor; a reader holding that entry's reference would then
    crash on its released runtime data on every submission.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    assert await hass.config_entries.async_unload(lcm_config_entry.entry_id)
    await hass.async_block_till_done()

    caplog.clear()
    hass.states.async_set(READER_ENTITY_ID, "9999")
    await hass.async_block_till_done()

    assert [event.data[ATTR_CODE_SLOT] for event in usage_events] == [3]
    assert not failure_events
    # The state-change dispatcher swallows a listener's exception, so a
    # crash would show up only here.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_padded_code_validates_on_both_paths(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """
    Surrounding whitespace is stripped whichever path submits the code.

    The service schema stripped its own argument, so a keypad emitting a
    trailing newline validated through the service and failed at the
    reader -- two answers from what is meant to be one validation.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    hass.states.async_set(READER_ENTITY_ID, " 1234 ")
    await hass.async_block_till_done()

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_VALIDATE_CODE,
        {
            ATTR_LOCK_ENTITY_ID: READER_ENTITY_ID,
            ATTR_CODE: " 1234 ",
            ATTR_FIRE_EVENTS: False,
        },
        blocking=True,
        return_response=True,
    )

    assert [event.data[ATTR_CODE_SLOT] for event in usage_events] == [1]
    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}


async def test_diagnostics_never_contain_the_submitted_code(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """
    The anchor's state is redacted in the diagnostics bundle.

    Diagnostics redact by unique id, which only ever matches this
    integration's own entities; the anchor belongs to esphome, so its state
    -- the last credential typed -- would ride along into the file users
    attach to bug reports.
    """
    hass.states.async_set(READER_ENTITY_ID, "1234")
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, lcm_config_entry)

    anchor = next(
        entity
        for entity in diagnostics["locks"][READER_ENTITY_ID]["entities"]
        if entity["entity_id"] == READER_ENTITY_ID
    )
    assert anchor["state"] == REDACTED
    assert "1234" not in str(diagnostics)


async def test_dispatched_end_to_end_as_reader(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """The real setup path instantiates a ReaderLock for the anchor entity."""
    lock = lcm_config_entry.runtime_data.locks[READER_ENTITY_ID]
    assert isinstance(lock, ReaderLock)
    assert lock.domain == "esphome"


async def test_diagnostics_redact_every_anchor_sharing_a_device(
    hass: HomeAssistant, two_anchor_lcm_config_entry: MockConfigEntry
) -> None:
    """
    Neither anchor's code survives a bundle built for one of them.

    The per-lock section dumps every entity on the lock's device, so a
    device carrying two anchors -- a keypad exposing more than one
    credential entity, or a lock device that also hosts one -- puts the
    sibling's state in a bundle whose redaction set was scoped to a single
    lock.
    """
    hass.states.async_set(READER_ENTITY_ID, "4321")
    hass.states.async_set(SECOND_READER_ENTITY_ID, "8888")
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(
        hass, two_anchor_lcm_config_entry
    )

    anchors = [
        entity
        for lock_diag in diagnostics["locks"].values()
        for entity in lock_diag["entities"]
        if entity["entity_id"] in (READER_ENTITY_ID, SECOND_READER_ENTITY_ID)
    ]
    assert len(anchors) == 4
    assert all(anchor["state"] == REDACTED for anchor in anchors)
    assert "4321" not in str(diagnostics)
    assert "8888" not in str(diagnostics)


async def test_device_diagnostics_redact_every_anchor_sharing_a_device(
    hass: HomeAssistant,
    two_anchor_lcm_config_entry: MockConfigEntry,
    second_reader_entity: er.RegistryEntry,
) -> None:
    """A bundle downloaded from the shared device redacts both anchors."""
    hass.states.async_set(READER_ENTITY_ID, "4321")
    hass.states.async_set(SECOND_READER_ENTITY_ID, "8888")
    await hass.async_block_till_done()

    assert second_reader_entity.device_id
    device = dr.async_get(hass).async_get(second_reader_entity.device_id)
    assert device

    diagnostics = await async_get_device_diagnostics(
        hass, two_anchor_lcm_config_entry, device
    )

    anchors = [
        entity
        for entity in diagnostics["entities"]
        if entity["entity_id"] in (READER_ENTITY_ID, SECOND_READER_ENTITY_ID)
    ]
    assert len(anchors) == 2
    assert all(anchor["state"] == REDACTED for anchor in anchors)
    assert "4321" not in str(diagnostics)
    assert "8888" not in str(diagnostics)


async def test_whitespace_only_state_is_blank(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """
    A state that is empty once stripped has submitted nothing.

    Validation strips before it matches, so a keypad that clears itself to
    a space or a newline matches no slot and would report a failure nobody
    caused.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    for state in ("   ", "\n", "\t "):
        hass.states.async_set(READER_ENTITY_ID, state)
        await hass.async_block_till_done()

    assert not usage_events
    assert not failure_events


async def test_padded_stored_pin_validates_at_the_reader(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """
    A PIN saved with surrounding whitespace still opens the keypad.

    The reader strips what is submitted but can only compare it against
    what was stored, so padding kept on the stored side makes a credential
    that nothing a user can type will ever match.
    """
    usage_events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_pin_update(" 4321 ")
    await hass.async_block_till_done()

    hass.states.async_set(READER_ENTITY_ID, "4321")
    await hass.async_block_till_done()

    assert [event.data[ATTR_CODE_SLOT] for event in usage_events] == [1]
    assert not failure_events
