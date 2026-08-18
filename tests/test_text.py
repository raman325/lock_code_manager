"""Test text platform."""

import logging

import pytest

from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN

from .common import (
    SLOT_1_NAME_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_NAME_ENTITY,
    SLOT_2_PIN_ENTITY,
)

_LOGGER = logging.getLogger(__name__)


async def test_text_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test text entities."""
    state = hass.states.get(SLOT_2_NAME_ENTITY)
    assert state
    assert state.state == "test2"

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "5678"

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "0987"

    # Clearing a PIN on an enabled slot should auto-disable the slot and clear the PIN
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == ""

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF


async def test_set_name_updates_slot_name(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Setting the name text entity's value updates the slot's configured name."""
    state = hass.states.get(SLOT_2_NAME_ENTITY)
    assert state
    assert state.state == "test2"

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "renamed"},
        target={ATTR_ENTITY_ID: SLOT_2_NAME_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_NAME_ENTITY)
    assert state
    assert state.state == "renamed"


async def test_whitespace_pin_normalized_to_empty(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that a whitespace-only PIN is normalized to empty and auto-disables the slot."""
    # First verify the slot is enabled and has a PIN
    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "5678"

    # Set a whitespace-only PIN — should normalize to "" and auto-disable
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "   "},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == ""

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF


@pytest.mark.parametrize(
    ("value", "translation_key"),
    [
        ("", "name_required"),
        ("   ", "name_required"),
        ("Ra|man", "name_has_separator"),
    ],
)
async def test_set_name_rejects_invalid_names(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    value: str,
    translation_key: str,
):
    """The name text entity enforces the name rules.

    This is the ordinary way to rename a slot in the frontend. If it did not
    validate, the "present, unique, encodable" invariant the migration
    establishes would not survive first contact with the UI.
    """
    original = hass.states.get(SLOT_2_NAME_ENTITY).state

    # Assert the translation key, not the English text: the message is
    # localized, and pinning the wording would make every translation edit a
    # test failure.
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: value},
            target={ATTR_ENTITY_ID: SLOT_2_NAME_ENTITY},
            blocking=True,
        )
    assert err.value.translation_key == translation_key

    assert hass.states.get(SLOT_2_NAME_ENTITY).state == original


async def test_set_name_rejects_another_slots_name(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Renaming a slot onto another slot's name is refused, ignoring case."""
    other = hass.states.get(SLOT_1_NAME_ENTITY).state

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: f"  {other.upper()}  "},
            target={ATTR_ENTITY_ID: SLOT_2_NAME_ENTITY},
            blocking=True,
        )
    assert err.value.translation_key == "name_not_unique"
    assert err.value.translation_placeholders["conflicting_slot"] == "1"


async def test_set_name_normalizes_whitespace(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A padded name is stored stripped, so it cannot shadow an existing one."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "  Raman  "},
        target={ATTR_ENTITY_ID: SLOT_2_NAME_ENTITY},
        blocking=True,
    )

    assert hass.states.get(SLOT_2_NAME_ENTITY).state == "Raman"


async def test_set_name_moves_registry_identifiers(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Renaming through the text entity moves the registry, not just config.

    This path works only because async_update_listener reaches its early
    return without suspending, so it captures the previous config before
    _write_config_fields' eager refresh overwrites it. Nothing enforces that;
    inserting a single await ahead of the rename block silently stops every
    text-entity rename from moving identifiers — and config then holds the new
    name while the registry holds the old, which is the destructive case.

    No test covered this path: the end-to-end rename tests all go through the
    options flow.
    """
    ent_reg = er.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id
    before = hass.states.get(SLOT_2_NAME_ENTITY).state
    pin_entity = ent_reg.async_get_entity_id(
        TEXT_DOMAIN, DOMAIN, f"{entry_id}|{before}|pin"
    )
    assert pin_entity is not None

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "Renamed User"},
        target={ATTR_ENTITY_ID: SLOT_2_NAME_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Same registry row — so the entity ID automations reference is unchanged.
    assert ent_reg.async_get(pin_entity).unique_id == f"{entry_id}|Renamed User|pin"
    assert (
        ent_reg.async_get_entity_id(TEXT_DOMAIN, DOMAIN, f"{entry_id}|{before}|pin")
        is None
    )
