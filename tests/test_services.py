"""Test services."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    STATE_ON,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CLEAR_CREDENTIALS,
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_FIRE_EVENTS,
    ATTR_LCM_CONFIG_ENTRY_ID,
    ATTR_LENGTH,
    ATTR_LOCK_ENTITY_ID,
    ATTR_REASON,
    ATTR_SLOT,
    ATTR_TEXT,
    ATTR_USER,
    ATTR_USERCODE,
    ATTR_VALID,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CODE_VALIDATION_FAILED,
    EVENT_LOCK_STATE_CHANGED,
    REASON_CONDITION_NOT_MET,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
    SERVICE_ADD_USER,
    SERVICE_CLEAR_SLOT_CONDITION,
    SERVICE_CLEAR_USERCODE,
    SERVICE_DELETE_USER,
    SERVICE_DEOBFUSCATE_LOG,
    SERVICE_GENERATE_PIN,
    SERVICE_SET_SLOT_CONDITION,
    SERVICE_SET_USERCODE,
    SERVICE_VALIDATE_CODE,
)
from custom_components.lock_code_manager.domain import services
from custom_components.lock_code_manager.domain.allocation import SlotAllocationError
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.pin_generator import is_unsafe_pin
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.services import async_set_usercode
from custom_components.lock_code_manager.domain.util import mask_pin

from .common import LOCK_1_ENTITY_ID


async def test_set_usercode_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test set_usercode service sets a code on the lock."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
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
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
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
    assert (
        get_entry_config(updated_entry).slot(1)[CONF_CONDITION] == condition_entity_id
    )


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
    assert CONF_CONDITION not in get_entry_config(updated_entry).slot(2)


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


async def test_async_set_usercode_domain_function_rejects_empty_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The domain-layer function itself rejects a blank usercode.

    The HA service schema for ``set_usercode`` already strips and enforces
    a minimum length, so a whitespace-only usercode never reaches
    ``async_set_usercode`` through that path. The websocket API's schema is
    looser (a bare ``str``), so the domain function's own guard is the last
    line of defense and must be exercised directly.
    """
    with pytest.raises(ServiceValidationError, match="must not be empty"):
        await async_set_usercode(hass, LOCK_1_ENTITY_ID, 3, "   ")


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


async def test_deobfuscate_log_service_round_trips_configured_pins(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """deobfuscate_log replaces tokens for currently-configured PINs and wraps with sentinels."""
    instance_id = hass.data[DOMAIN]["instance_id"]
    # BASE_CONFIG has slot 1 with PIN "1234" and slot 2 with PIN "5678".
    token_slot_1 = mask_pin("1234", 1, instance_id)
    token_slot_2 = mask_pin("5678", 2, instance_id)
    log_excerpt = (
        f"Setting usercode on lock.test_1 slot 1 (pin={token_slot_1})\n"
        f"Setting usercode on lock.test_2 slot 2 (pin={token_slot_2})\n"
        "Unrelated line with no token\n"
        "Stale token from a rotated PIN: pin#deadbeef"
    )

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_DEOBFUSCATE_LOG,
        {ATTR_TEXT: log_excerpt},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    text = response["deobfuscated_text"]
    assert "1234" in text
    assert "5678" in text
    assert token_slot_1 not in text
    assert token_slot_2 not in text
    # Stale token is preserved verbatim so the output remains comparable to the input.
    assert "pin#deadbeef" in text
    assert "BEGIN DEOBFUSCATED" in text and "END DEOBFUSCATED" in text

    summary = response["summary"]
    assert summary == {
        "total": 3,
        "matched": 2,
        "unmatched_tokens": ["pin#deadbeef"],
    }


async def test_add_user_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """add_user names a person and allocates them a slot number."""
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "Newcomer",
            CONF_PIN: "9876",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["Newcomer"][CONF_PIN] == "9876"
    assert config.users["Newcomer"][CONF_ENABLED] is True
    # Nobody who was already here moved: their credential is written at their
    # number, so renumbering them would rewrite it on every lock.
    assert config.assignment.slot("test1") == 1
    assert config.assignment.slot("test2") == 2
    assert config.assignment.slot("Newcomer") not in (1, 2)


async def test_add_user_service_returns_once_the_entities_exist(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    entity_registry: er.EntityRegistry,
) -> None:
    """
    When the call returns, the new user's entities are there.

    Deliberately no ``async_block_till_done`` between the call and the
    assertion. Home Assistant runs update listeners as a task rather than
    awaiting them, so a service that only writes returns before the entry
    has reacted -- a script that adds a user and then sets their PIN
    through the new text entity would find nothing to set, and the
    dashboard reloads the moment the call returns.

    This states the contract; it does not prove the wait carries it. The
    add path happens to finish inside the awaits this call already makes,
    so it passes either way. ``test_delete_user_service_returns_once_the_
    entities_are_gone`` is the one that fails without it.
    """
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "Newcomer",
            CONF_PIN: "9876",
        },
        blocking=True,
    )

    slot = get_entry_config(
        hass.config_entries.async_get_entry(entry.entry_id)
    ).assignment.slot("Newcomer")
    # The per-lock entities, not just the slot's own: they are created at
    # the very end of the pass, after it has awaited the locks, so they are
    # what actually distinguishes "the entry has reacted" from "the write
    # landed".
    assert entity_registry.async_get_entity_id(
        "binary_sensor",
        DOMAIN,
        build_slot_unique_id(entry.entry_id, slot, "in_sync", LOCK_1_ENTITY_ID),
    )


async def test_service_does_not_wait_when_the_write_changed_nothing(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A write that changes nothing runs no listener, so nothing would ever
    arrive to end the wait. Two identical calls landing before the entry
    settles is the way there: the second writes the options the first
    already wrote.
    """
    entry = lock_code_manager_config_entry
    slot = get_entry_config(entry).assignment.slot("test1")
    hass.states.async_set("binary_sensor.settle_probe", STATE_ON)

    with patch.object(
        hass.config_entries, "async_update_entry", return_value=False
    ) as update:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SLOT_CONDITION,
            {
                "config_entry_id": entry.entry_id,
                ATTR_SLOT: slot,
                CONF_ENTITY_ID: "binary_sensor.settle_probe",
            },
            blocking=True,
        )

    assert update.called


async def test_service_reports_an_entry_that_never_settles(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A pass that never finishes must not hold the caller forever.

    The write is already durable when the wait starts, so timing out is
    not a failure to hand back -- the entities arrive late rather than not
    at all, and saying otherwise would report a working add as broken.
    """
    entry = lock_code_manager_config_entry
    slot = get_entry_config(entry).assignment.slot("test1")
    hass.states.async_set("binary_sensor.settle_probe", STATE_ON)

    async def _never() -> None:
        await asyncio.sleep(10)

    with (
        patch.object(services, "_SETTLE_TIMEOUT", 0.01),
        patch.object(entry.runtime_data.settled, "wait", _never),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_SLOT_CONDITION,
            {
                "config_entry_id": entry.entry_id,
                ATTR_SLOT: slot,
                CONF_ENTITY_ID: "binary_sensor.settle_probe",
            },
            blocking=True,
        )

    assert "did not finish updating" in caplog.text
    # The write still landed; only the waiting gave up.
    assert (
        get_entry_config(hass.config_entries.async_get_entry(entry.entry_id)).slot(
            slot
        )[CONF_CONDITION]
        == "binary_sensor.settle_probe"
    )


async def test_delete_user_service_returns_once_the_entities_are_gone(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The same promise on the way out, for the same reason."""
    entry = lock_code_manager_config_entry
    slot = get_entry_config(entry).assignment.slot("test1")
    unique_id = f"{entry.entry_id}|{slot}|{CONF_NAME}"
    assert entity_registry.async_get_entity_id("text", DOMAIN, unique_id)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE_USER,
        {"config_entry_id": entry.entry_id, CONF_NAME: "test1"},
        blocking=True,
    )

    assert entity_registry.async_get_entity_id("text", DOMAIN, unique_id) is None


async def test_add_user_service_rejects_a_duplicate_name(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Two names meaning one person would collapse into a single key."""
    with pytest.raises(ServiceValidationError, match="already exists"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: " TEST1 ",
                CONF_PIN: "9876",
            },
            blocking=True,
        )


async def test_add_user_service_rejects_enabled_without_a_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A slot cannot be programmed without something to program."""
    with pytest.raises(ServiceValidationError, match="without a PIN"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "Newcomer",
            },
            blocking=True,
        )


async def test_add_user_service_allows_a_disabled_user_with_no_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Someone can be set up now and given a PIN later."""
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "Later",
            CONF_ENABLED: False,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["Later"][CONF_ENABLED] is False
    assert CONF_PIN not in config.users["Later"]


async def test_delete_user_service(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """delete_user removes the person and releases their slot on the locks."""
    entry = lock_code_manager_config_entry
    released: list[int] = []
    for lock in entry.runtime_data.locks.values():
        lock.async_release_managed_slot = AsyncMock(side_effect=released.append)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE_USER,
        {"config_entry_id": entry.entry_id, CONF_NAME: "test2"},
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert "test2" not in config.users
    assert config.assignment.slot("test1") == 1
    assert released == [2, 2]


async def test_delete_user_service_can_hand_the_credential_over(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """clear_credentials=False stops managing the slot without clearing it."""
    entry = lock_code_manager_config_entry
    for lock in entry.runtime_data.locks.values():
        lock.async_release_managed_slot = AsyncMock()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE_USER,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "test2",
            ATTR_CLEAR_CREDENTIALS: False,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert "test2" not in config.users
    for lock in entry.runtime_data.locks.values():
        lock.async_release_managed_slot.assert_not_called()
    # Drained, so the next occupant of that number still gets its cleanup.
    assert not entry.runtime_data.retained_pairs


async def test_delete_user_service_unknown_name(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Deleting somebody who is not there says so rather than doing nothing."""
    with pytest.raises(ServiceValidationError, match="No user named"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_DELETE_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "nobody",
            },
            blocking=True,
        )


async def test_add_user_service_with_a_condition(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A condition entity can be attached as the user is created."""
    entry = lock_code_manager_config_entry
    condition_entity_id = "binary_sensor.newcomer_home"
    hass.states.async_set(condition_entity_id, STATE_ON)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "Newcomer",
            CONF_PIN: "9876",
            CONF_CONDITION: condition_entity_id,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["Newcomer"][CONF_CONDITION] == condition_entity_id


async def test_add_user_service_rejects_a_missing_condition(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A condition entity that does not exist would never turn the PIN on."""
    with pytest.raises(ServiceValidationError, match="not found"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "Newcomer",
                CONF_PIN: "9876",
                CONF_CONDITION: "binary_sensor.nonexistent",
            },
            blocking=True,
        )


async def test_add_user_service_rejects_an_excluded_condition_platform(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The refusal the editor gives, on the route that goes around the editor."""
    excluded = entity_registry.async_get_or_create(
        "binary_sensor", "scheduler", "excluded_condition"
    )
    hass.states.async_set(excluded.entity_id, STATE_ON)

    with pytest.raises(ServiceValidationError, match="scheduler"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "Newcomer",
                CONF_PIN: "9876",
                CONF_CONDITION: excluded.entity_id,
            },
            blocking=True,
        )


async def test_add_user_service_rejects_a_blank_name(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A user with no name has no identity to be stored under."""
    with pytest.raises(ServiceValidationError, match="name_required"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "   ",
                CONF_PIN: "9876",
            },
            blocking=True,
        )


async def test_add_user_service_reports_an_allocation_refusal(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Allocation's refusal reaches the caller as a translated action error."""
    with patch(
        "custom_components.lock_code_manager.domain.services.async_allocate_for",
        side_effect=SlotAllocationError(
            "occupancy_unknown", {"locks": LOCK_1_ENTITY_ID}
        ),
    ):
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_ADD_USER,
                {
                    "config_entry_id": lock_code_manager_config_entry.entry_id,
                    CONF_NAME: "Newcomer",
                    CONF_PIN: "9876",
                },
                blocking=True,
            )

    # Carried as a key, not a rendered string: the same refusal is worded once
    # and reaches the config flow and the action picker alike.
    assert raised.value.translation_key == "occupancy_unknown"
    assert raised.value.translation_placeholders == {"locks": LOCK_1_ENTITY_ID}


async def test_services_accept_a_config_entry_title(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """An action can name its entry the way a card does, by title."""
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "config_entry_title": entry.title,
            CONF_NAME: "Newcomer",
            CONF_PIN: "9876",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert (
        "Newcomer"
        in get_entry_config(hass.config_entries.async_get_entry(entry.entry_id)).users
    )


async def test_services_match_a_title_slugified(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The websocket API matches titles slugified, so an action does too."""
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE_USER,
        {"config_entry_title": "mock-title", CONF_NAME: "test2"},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert (
        "test2"
        not in get_entry_config(
            hass.config_entries.async_get_entry(entry.entry_id)
        ).users
    )


async def test_services_reject_an_unknown_title(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A title naming no entry says so, rather than falling back to an ID."""
    with pytest.raises(ServiceValidationError, match="with title"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_title": "no such entry",
                CONF_NAME: "Newcomer",
                CONF_PIN: "9876",
            },
            blocking=True,
        )


async def test_services_reject_both_identifiers(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Supplying both is refused rather than one silently winning."""
    entry = lock_code_manager_config_entry
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": entry.entry_id,
                "config_entry_title": entry.title,
                CONF_NAME: "Newcomer",
                CONF_PIN: "9876",
            },
            blocking=True,
        )


async def test_services_require_one_identifier(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Naming no entry at all is refused by the schema, before any work."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {CONF_NAME: "Newcomer", CONF_PIN: "9876"},
            blocking=True,
        )


async def test_slot_condition_services_accept_a_title(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The pre-existing entry-taking services take a title too."""
    entry = lock_code_manager_config_entry
    condition_entity_id = "binary_sensor.by_title"
    hass.states.async_set(condition_entity_id, STATE_ON)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_SLOT_CONDITION,
        {
            "config_entry_title": entry.title,
            ATTR_SLOT: 1,
            CONF_ENTITY_ID: condition_entity_id,
        },
        blocking=True,
    )
    await hass.async_block_till_done()
    assert (
        get_entry_config(hass.config_entries.async_get_entry(entry.entry_id)).slot(1)[
            CONF_CONDITION
        ]
        == condition_entity_id
    )

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_SLOT_CONDITION,
        {"config_entry_title": entry.title, ATTR_SLOT: 1},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert CONF_CONDITION not in get_entry_config(
        hass.config_entries.async_get_entry(entry.entry_id)
    ).slot(1)


VALIDATE_LOCK_ENTITY_ID = "lock.virtual_validate_service"
VALIDATE_CONDITION_ENTITY_ID = "input_boolean.validate_service_gate"
READER_ENTITY_ID = "sensor.keypad_code"

# alice validates, bob is disabled, carol waits on a condition that is off.
VALIDATE_CONFIG = {
    CONF_LOCKS: [VALIDATE_LOCK_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "alice", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {CONF_NAME: "bob", CONF_PIN: "5678", CONF_ENABLED: False},
        3: {
            CONF_NAME: "carol",
            CONF_PIN: "9999",
            CONF_ENABLED: True,
            CONF_CONDITION: VALIDATE_CONDITION_ENTITY_ID,
        },
    },
}

# Second entry sharing the same lock entity: betty's code is valid here and
# unknown to the first entry; bob's disabled code is unknown here.
SECOND_VALIDATE_CONFIG = {
    CONF_LOCKS: [VALIDATE_LOCK_ENTITY_ID],
    CONF_SLOTS: {
        11: {CONF_NAME: "betty", CONF_PIN: "2468", CONF_ENABLED: True},
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


async def _call_validate_code(
    hass: HomeAssistant, data: dict, *, return_response: bool = True
) -> dict | None:
    """Call the validate_code service and flush the event bus."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_VALIDATE_CODE,
        data,
        blocking=True,
        return_response=return_response,
    )
    await hass.async_block_till_done()
    return response


@pytest.fixture(name="validate_entry")
async def validate_entry_fixture(hass: HomeAssistant):
    """Set up a full LCM config entry managing a virtual lock."""
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "virtual",
        "virtual_validate_service",
        suggested_object_id="virtual_validate_service",
        config_entry=virtual_entry,
    )
    assert lock_entity.entity_id == VALIDATE_LOCK_ENTITY_ID
    hass.states.async_set(VALIDATE_LOCK_ENTITY_ID, "locked")
    hass.states.async_set(VALIDATE_CONDITION_ENTITY_ID, "off")

    lcm_entry = MockConfigEntry(
        domain=DOMAIN, data=VALIDATE_CONFIG, unique_id="test_validate_service"
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


@pytest.fixture(name="second_validate_entry")
async def second_validate_entry_fixture(hass: HomeAssistant, validate_entry):
    """Set up a second LCM config entry managing the same virtual lock."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data=SECOND_VALIDATE_CONFIG,
        unique_id="test_validate_service_2",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_validate_code_valid(hass: HomeAssistant, validate_entry) -> None:
    """A valid code returns the user with no failure reason."""
    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: "1234"},
    )
    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}


async def test_validate_code_unknown(hass: HomeAssistant, validate_entry) -> None:
    """A code no slot holds is rejected as unknown."""
    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: "0000"},
    )
    assert response == {
        ATTR_VALID: False,
        ATTR_USER: None,
        ATTR_REASON: REASON_UNKNOWN_CODE,
    }


@pytest.mark.parametrize(
    ("code", "reason"),
    [("5678", REASON_USER_DISABLED), ("9999", REASON_CONDITION_NOT_MET)],
)
async def test_validate_code_failure_reasons(
    hass: HomeAssistant, validate_entry, code: str, reason: str
) -> None:
    """A matched-but-inactive code reports why it did not validate."""
    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: code},
    )
    assert response == {ATTR_VALID: False, ATTR_USER: None, ATTR_REASON: reason}


async def test_validate_code_fires_success_event(
    hass: HomeAssistant, validate_entry
) -> None:
    """With fire_events defaulted on, a valid code fires one usage event."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: "1234"},
    )

    assert len(state_events) == 1
    assert state_events[0].data[ATTR_CODE_SLOT] == 1
    assert not failure_events


async def test_validate_code_fires_failure_event(
    hass: HomeAssistant, validate_entry
) -> None:
    """With fire_events defaulted on, an invalid code fires one failure event."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: "0000"},
    )

    assert len(failure_events) == 1
    assert failure_events[0].data[ATTR_REASON] == REASON_UNKNOWN_CODE
    assert not state_events


async def test_validate_code_fire_events_false(
    hass: HomeAssistant, validate_entry
) -> None:
    """With fire_events off neither outcome fires any event."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    success = await _call_validate_code(
        hass,
        {
            ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID,
            ATTR_CODE: "1234",
            ATTR_FIRE_EVENTS: False,
        },
    )
    failure = await _call_validate_code(
        hass,
        {
            ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID,
            ATTR_CODE: "0000",
            ATTR_FIRE_EVENTS: False,
        },
    )

    assert success[ATTR_VALID] is True
    assert failure[ATTR_REASON] == REASON_UNKNOWN_CODE
    assert not state_events
    assert not failure_events


async def test_validate_code_unmanaged_entity(
    hass: HomeAssistant, validate_entry
) -> None:
    """An entity no loaded entry manages raises a validation error."""
    with pytest.raises(ServiceValidationError, match="not managed"):
        await _call_validate_code(
            hass,
            {ATTR_LOCK_ENTITY_ID: "lock.nonexistent", ATTR_CODE: "1234"},
        )


async def test_validate_code_without_return_response(
    hass: HomeAssistant, validate_entry
) -> None:
    """The service works fire-and-forget, leaving only its events behind."""
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: "1234"},
        return_response=False,
    )

    assert response is None
    assert len(state_events) == 1


async def test_validate_code_multi_entry_valid_in_second(
    hass: HomeAssistant, validate_entry, second_validate_entry
) -> None:
    """A code valid only in the second entry validates with one success event.

    Exactly one success and zero failure events pins the compute-then-fire
    design: firing per entry would also emit a failure from the first entry.
    """
    state_events = _capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: "2468"},
    )

    assert response == {ATTR_VALID: True, ATTR_USER: "betty", ATTR_REASON: None}
    assert len(state_events) == 1
    # betty's slot number proves the second entry is the one that fired.
    assert state_events[0].data[ATTR_CODE_SLOT] == 11
    assert not failure_events


@pytest.mark.parametrize(
    ("code", "reason"),
    [("5678", REASON_USER_DISABLED), ("9999", REASON_CONDITION_NOT_MET)],
)
async def test_validate_code_multi_entry_aggregate_reason(
    hass: HomeAssistant, validate_entry, second_validate_entry, code: str, reason: str
) -> None:
    """A code matched-but-inactive in one entry and unknown in the other.

    The entry that recognized the code explains the failure: a match in any
    entry outranks unknown everywhere.
    """
    failure_events = _capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: VALIDATE_LOCK_ENTITY_ID, ATTR_CODE: code},
    )

    assert response == {ATTR_VALID: False, ATTR_USER: None, ATTR_REASON: reason}
    assert len(failure_events) == 1
    assert failure_events[0].data[ATTR_REASON] == reason
    # The entry that recognized the code is the one that fired.
    assert failure_events[0].data[ATTR_LCM_CONFIG_ENTRY_ID] == validate_entry.entry_id


async def test_validate_code_reader_entity(hass: HomeAssistant) -> None:
    """The service accepts a reader anchor entity, not just lock-domain ones."""
    esphome_entry = MockConfigEntry(domain="esphome")
    esphome_entry.add_to_hass(hass)
    reader_entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "keypad_code",
        suggested_object_id="keypad_code",
        config_entry=esphome_entry,
    )
    assert reader_entity.entity_id == READER_ENTITY_ID
    hass.states.async_set(READER_ENTITY_ID, "")

    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [READER_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "alice", CONF_PIN: "1234", CONF_ENABLED: True},
            },
        },
        unique_id="test_validate_reader",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    response = await _call_validate_code(
        hass,
        {ATTR_LOCK_ENTITY_ID: READER_ENTITY_ID, ATTR_CODE: "1234"},
    )
    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}

    await hass.config_entries.async_unload(lcm_entry.entry_id)
    # A mock provider entry left LOADED would make hass teardown genuinely
    # try to unload esphome, which was never set up.
    if esphome_entry.state is not ConfigEntryState.NOT_LOADED:
        esphome_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
