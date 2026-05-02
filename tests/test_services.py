"""Test services."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from homeassistant.const import CONF_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    ATTR_LENGTH,
    ATTR_LOCK_ENTITY_ID,
    ATTR_SLOT,
    ATTR_USERCODE,
    CONF_LOCKS,
    DOMAIN,
    SERVICE_CLEAR_SLOT_CONDITION,
    SERVICE_CLEAR_USERCODE,
    SERVICE_GENERATE_PIN,
    SERVICE_SET_SLOT_CONDITION,
    SERVICE_SET_USERCODE,
)
from custom_components.lock_code_manager.pin_generator import is_unsafe_pin

from .common import LOCK_1_ENTITY_ID


async def test_set_usercode_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_usercode service sets a code on the lock."""
    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    lock.async_internal_set_usercode = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_USERCODE,
        {
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
            ATTR_USERCODE: "9999",
        },
        blocking=True,
    )

    lock.async_internal_set_usercode.assert_awaited_once_with(3, "9999")


async def test_set_usercode_service_lock_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_usercode service raises when lock is not managed."""
    with pytest.raises(ServiceValidationError, match="not managed"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_USERCODE,
            {
                ATTR_LOCK_ENTITY_ID: "lock.nonexistent",
                ATTR_CODE_SLOT: 3,
                ATTR_USERCODE: "1234",
            },
            blocking=True,
        )


async def test_clear_usercode_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test clear_usercode service clears a code on the lock."""
    lock = hass.data[DOMAIN][CONF_LOCKS][LOCK_1_ENTITY_ID]
    lock.async_internal_clear_usercode = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_USERCODE,
        {
            ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_CODE_SLOT: 3,
        },
        blocking=True,
    )

    lock.async_internal_clear_usercode.assert_awaited_once_with(3)


async def test_clear_usercode_service_lock_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test clear_usercode service raises when lock is not managed."""
    with pytest.raises(ServiceValidationError, match="not managed"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CLEAR_USERCODE,
            {
                ATTR_LOCK_ENTITY_ID: "lock.nonexistent",
                ATTR_CODE_SLOT: 3,
            },
            blocking=True,
        )


async def test_set_slot_condition_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_slot_condition service assigns a condition entity to a slot."""
    entry = lock_code_manager_config_entry
    condition_entity_id = "binary_sensor.test_condition"
    hass.states.async_set(condition_entity_id, STATE_ON)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SLOT_CONDITION,
        {
            "config_entry_id": entry.entry_id,
            ATTR_SLOT: 1,
            CONF_ENTITY_ID: condition_entity_id,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    # Verify the config entry was updated with the condition entity
    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    # After update, data is written via options then moved to data
    # Check both options and data for the condition entity
    slots = updated_entry.options.get("slots", updated_entry.data.get("slots", {}))
    slot_key = 1 if 1 in slots else "1"
    assert slots[slot_key][CONF_ENTITY_ID] == condition_entity_id


async def test_set_slot_condition_service_entry_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_slot_condition service raises when config entry not found."""
    hass.states.async_set("binary_sensor.test_condition", STATE_ON)

    with pytest.raises(ServiceValidationError, match="No lock code manager"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SLOT_CONDITION,
            {
                "config_entry_id": "nonexistent_entry",
                ATTR_SLOT: 1,
                CONF_ENTITY_ID: "binary_sensor.test_condition",
            },
            blocking=True,
        )


async def test_set_slot_condition_service_slot_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_slot_condition service raises when slot not found."""
    entry = lock_code_manager_config_entry
    hass.states.async_set("binary_sensor.test_condition", STATE_ON)

    with pytest.raises(ServiceValidationError, match="Slot.*not found"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SLOT_CONDITION,
            {
                "config_entry_id": entry.entry_id,
                ATTR_SLOT: 999,
                CONF_ENTITY_ID: "binary_sensor.test_condition",
            },
            blocking=True,
        )


async def test_set_slot_condition_service_entity_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_slot_condition service raises when condition entity not found."""
    entry = lock_code_manager_config_entry

    with pytest.raises(ServiceValidationError, match="not found"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SLOT_CONDITION,
            {
                "config_entry_id": entry.entry_id,
                ATTR_SLOT: 1,
                CONF_ENTITY_ID: "binary_sensor.nonexistent",
            },
            blocking=True,
        )


async def test_clear_slot_condition_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test clear_slot_condition service removes a condition entity from a slot."""
    entry = lock_code_manager_config_entry

    # Slot 2 has a condition entity (calendar.test_1) configured in BASE_CONFIG
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_SLOT_CONDITION,
        {
            "config_entry_id": entry.entry_id,
            ATTR_SLOT: 2,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    updated_entry = hass.config_entries.async_get_entry(entry.entry_id)
    slots = updated_entry.options.get("slots", updated_entry.data.get("slots", {}))
    slot_key = 2 if 2 in slots else "2"
    assert CONF_ENTITY_ID not in slots[slot_key]


async def test_clear_slot_condition_service_entry_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test clear_slot_condition service raises when config entry not found."""
    with pytest.raises(ServiceValidationError, match="No lock code manager"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CLEAR_SLOT_CONDITION,
            {
                "config_entry_id": "nonexistent_entry",
                ATTR_SLOT: 1,
            },
            blocking=True,
        )


async def test_clear_slot_condition_service_slot_not_found(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test clear_slot_condition service raises when slot not found."""
    entry = lock_code_manager_config_entry

    with pytest.raises(ServiceValidationError, match="Slot.*not found"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CLEAR_SLOT_CONDITION,
            {
                "config_entry_id": entry.entry_id,
                ATTR_SLOT: 999,
            },
            blocking=True,
        )


async def test_set_usercode_service_empty_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_usercode service raises when usercode is empty or whitespace."""
    for usercode in ["", "   ", "\t\n"]:
        with pytest.raises(
            (ServiceValidationError, vol.MultipleInvalid),
        ):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_USERCODE,
                {
                    ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID,
                    ATTR_CODE_SLOT: 3,
                    ATTR_USERCODE: usercode,
                },
                blocking=True,
            )


async def test_get_loaded_config_entry_wrong_domain(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test get_loaded_config_entry raises when entry belongs to another domain."""
    # mock_lock_config_entry is a config entry for the "test" domain, not LCM
    with pytest.raises(ServiceValidationError, match="No lock code manager"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SLOT_CONDITION,
            {
                "config_entry_id": mock_lock_config_entry.entry_id,
                ATTR_SLOT: 1,
                CONF_ENTITY_ID: "binary_sensor.test_condition",
            },
            blocking=True,
        )


async def test_generate_pin_service_default_length(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """generate_pin returns a 4-digit PIN by default."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GENERATE_PIN,
        {},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    assert "pin" in response
    pin = response["pin"]
    assert len(pin) == 4
    assert pin.isdigit()
    assert not is_unsafe_pin(pin)


async def test_generate_pin_service_custom_length(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """generate_pin honours the length field."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GENERATE_PIN,
        {ATTR_LENGTH: 6},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    pin = response["pin"]
    assert len(pin) == 6
    assert pin.isdigit()
    assert not is_unsafe_pin(pin)


@pytest.mark.parametrize("length", [3, 13, 0])
async def test_generate_pin_service_rejects_invalid_length(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    length: int,
) -> None:
    """generate_pin rejects out-of-range length values at the schema layer."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GENERATE_PIN,
            {ATTR_LENGTH: length},
            blocking=True,
            return_response=True,
        )
