"""Tests for the credential validation core."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lock import LockState
from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_REASON,
    ATTR_TO,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CODE_VALIDATION_FAILED,
    EVENT_LOCK_STATE_CHANGED,
    REASON_CONDITION_NOT_MET,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
)
from custom_components.lock_code_manager.domain.validation import (
    ValidationResult,
    async_validate_credential,
)
from custom_components.lock_code_manager.providers import BaseLock

CONDITION_ENTITY_ID = "input_boolean.validation_gate"
LOCK_ENTITY_ID = "lock.virtual_validation_virtual"

# Slot layout exercised below:
# 1 -> active; 2 -> disabled; 3 -> condition off; 4 -> disabled AND condition
# off; 5+6 -> same PIN, condition-gated first and disabled second so the
# disabled verdict can only come from scanning past the first match.
VALIDATION_CONFIG = {
    CONF_LOCKS: [LOCK_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "alice", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {CONF_NAME: "bob", CONF_PIN: "5678", CONF_ENABLED: False},
        3: {
            CONF_NAME: "carol",
            CONF_PIN: "9999",
            CONF_ENABLED: True,
            CONF_CONDITION: CONDITION_ENTITY_ID,
        },
        4: {
            CONF_NAME: "dave",
            CONF_PIN: "4321",
            CONF_ENABLED: False,
            CONF_CONDITION: CONDITION_ENTITY_ID,
        },
        5: {
            CONF_NAME: "erin",
            CONF_PIN: "7777",
            CONF_ENABLED: True,
            CONF_CONDITION: CONDITION_ENTITY_ID,
        },
        6: {CONF_NAME: "frank", CONF_PIN: "7777", CONF_ENABLED: False},
    },
}


def _capture_events(hass: HomeAssistant, event_name: str) -> list[Event]:
    """Capture events of the given type on the hass event bus."""
    events: list[Event] = []

    @callback
    def capture(event: Event) -> None:
        events.append(event)

    hass.bus.async_listen(event_name, capture)
    return events


@pytest.fixture(name="validation_entry")
async def validation_entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a full LCM config entry managing a virtual lock."""
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "virtual",
        "validation_virtual",
        config_entry=virtual_entry,
    )
    assert lock_entity.entity_id == LOCK_ENTITY_ID
    hass.states.async_set(LOCK_ENTITY_ID, "locked")
    hass.states.async_set(CONDITION_ENTITY_ID, "off")

    lcm_entry = MockConfigEntry(
        domain=DOMAIN, data=VALIDATION_CONFIG, unique_id="test_validation"
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


@pytest.fixture(name="virtual_lock")
def virtual_lock_fixture(validation_entry: MockConfigEntry) -> BaseLock:
    """Extract the provider lock from the LCM config entry."""
    return validation_entry.runtime_data.locks[LOCK_ENTITY_ID]


async def _validate(
    hass: HomeAssistant,
    validation_entry: MockConfigEntry,
    virtual_lock: BaseLock,
    code: str,
    **kwargs: Any,
) -> ValidationResult:
    """Validate a code and flush the event bus."""
    result = await async_validate_credential(
        hass, validation_entry, virtual_lock, code, **kwargs
    )
    await hass.async_block_till_done()
    return result


async def test_valid_code(hass: HomeAssistant, validation_entry, virtual_lock):
    """An active slot's PIN validates and reports the configured user name."""
    result = await _validate(hass, validation_entry, virtual_lock, "1234")
    assert result == ValidationResult(valid=True, user="alice", reason=None)


async def test_unknown_code(hass: HomeAssistant, validation_entry, virtual_lock):
    """A code no slot holds is rejected as unknown, with an opaque masked token."""
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    result = await _validate(hass, validation_entry, virtual_lock, "0000")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_UNKNOWN_CODE
    )

    # No matched slot to salt with: the token is the slot-0 one, which the
    # deobfuscation map cannot (and should not) reverse.
    assert len(failure_events) == 1
    assert failure_events[0].data[ATTR_CODE] == virtual_lock.mask_pin("0000")


async def test_disabled_user(hass: HomeAssistant, validation_entry, virtual_lock):
    """A disabled slot's PIN is rejected as user_disabled."""
    result = await _validate(hass, validation_entry, virtual_lock, "5678")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_USER_DISABLED
    )


async def test_condition_not_met(hass: HomeAssistant, validation_entry, virtual_lock):
    """A slot blocked only by its condition entity is rejected as condition_not_met."""
    result = await _validate(hass, validation_entry, virtual_lock, "9999")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_CONDITION_NOT_MET
    )


async def test_disabled_and_condition_off(
    hass: HomeAssistant, validation_entry, virtual_lock
):
    """A slot both disabled and condition-gated reports the most restrictive reason."""
    result = await _validate(hass, validation_entry, virtual_lock, "4321")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_USER_DISABLED
    )


async def test_duplicate_code_precedence(
    hass: HomeAssistant, validation_entry, virtual_lock
):
    """When two slots share a PIN, one disabled and one condition-gated, disabled wins."""
    result = await _validate(hass, validation_entry, virtual_lock, "7777")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_USER_DISABLED
    )


async def test_success_fires_lock_state_changed(
    hass: HomeAssistant, validation_entry, virtual_lock
):
    """A successful validation fires exactly one lock state changed event."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    result = await _validate(hass, validation_entry, virtual_lock, "1234")
    assert result.valid is True

    assert len(state_events) == 1
    assert state_events[0].data[ATTR_CODE_SLOT] == 1
    # Presented as an unlock transition so the credential_used event surface
    # treats a validation exactly like a physical PIN unlock.
    assert state_events[0].data[ATTR_TO] == LockState.UNLOCKED
    assert not failure_events


async def test_failure_fires_validation_failed(
    hass: HomeAssistant, validation_entry, virtual_lock
):
    """A failed validation fires exactly one failure event with a masked code."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    result = await _validate(hass, validation_entry, virtual_lock, "5678")
    assert result.valid is False

    assert len(failure_events) == 1
    data = failure_events[0].data
    assert data[ATTR_REASON] == REASON_USER_DISABLED
    assert "5678" not in data.values()
    # The code matched slot 2, so the token is salted with that slot -- the
    # deobfuscation map can reverse it -- rather than the opaque slot-0 token.
    assert data[ATTR_CODE] == virtual_lock.mask_pin("5678", 2)
    assert data[ATTR_CODE] != virtual_lock.mask_pin("5678")
    assert not state_events


async def test_fire_events_false(hass: HomeAssistant, validation_entry, virtual_lock):
    """With fire_events disabled neither outcome fires any event."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    success = await _validate(
        hass, validation_entry, virtual_lock, "1234", fire_events=False
    )
    failure = await _validate(
        hass, validation_entry, virtual_lock, "0000", fire_events=False
    )

    assert success.valid is True
    assert failure.reason == REASON_UNKNOWN_CODE
    assert not state_events
    assert not failure_events
