"""Tests for the credential validation core."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import (
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    MATCH_ALL,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    REASON_CONDITION_NOT_MET,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
)
from custom_components.lock_code_manager.domain.validation import (
    ValidationResult,
    validate_credential,
)

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


async def test_valid_code(hass: HomeAssistant, validation_entry):
    """An active slot's PIN validates and reports the configured user name."""
    result = validate_credential(validation_entry, "1234")
    assert result == ValidationResult(valid=True, user="alice", reason=None)


async def test_unknown_code(hass: HomeAssistant, validation_entry):
    """A code no slot holds is rejected as unknown."""
    result = validate_credential(validation_entry, "0000")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_UNKNOWN_CODE
    )


async def test_disabled_user(hass: HomeAssistant, validation_entry):
    """A disabled slot's PIN is rejected as user_disabled."""
    result = validate_credential(validation_entry, "5678")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_USER_DISABLED
    )


async def test_condition_not_met(hass: HomeAssistant, validation_entry):
    """A slot blocked only by its condition entity is rejected as condition_not_met."""
    result = validate_credential(validation_entry, "9999")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_CONDITION_NOT_MET
    )


async def test_disabled_and_condition_off(hass: HomeAssistant, validation_entry):
    """A slot both disabled and condition-gated reports the most restrictive reason."""
    result = validate_credential(validation_entry, "4321")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_USER_DISABLED
    )


async def test_duplicate_code_precedence(hass: HomeAssistant, validation_entry):
    """When two slots share a PIN, one disabled and one condition-gated, disabled wins."""
    result = validate_credential(validation_entry, "7777")
    assert result == ValidationResult(
        valid=False, user=None, reason=REASON_USER_DISABLED
    )


@pytest.mark.parametrize("submitted", [" 1234", "1234 ", "\n1234\t"])
async def test_padding_is_stripped_before_matching(
    hass: HomeAssistant, validation_entry, submitted: str
):
    """A padded code gets the same answer as the same code without padding."""
    result = validate_credential(validation_entry, submitted)
    assert result == ValidationResult(valid=True, user="alice", reason=None)


async def test_validation_leaves_the_bus_alone(hass: HomeAssistant, validation_entry):
    """
    Validating is a question, not an occurrence: nothing is published.

    Guards the absence of an event surface: validating a code must not be
    observable on the bus. A success event would have to be a deliberate
    re-addition rather than something that creeps back in behind a helper.
    """
    fired: list[str] = []

    @callback
    def record(event: Event) -> None:
        fired.append(event.event_type)

    hass.bus.async_listen(MATCH_ALL, record)

    assert validate_credential(validation_entry, "1234").valid is True
    assert validate_credential(validation_entry, "0000").valid is False
    await hass.async_block_till_done()

    assert [name for name in fired if name.startswith(DOMAIN)] == []
