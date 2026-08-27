"""Test services."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
import voluptuous as vol

from homeassistant.components.event import (
    ATTR_EVENT_TYPE,
    DOMAIN as EVENT_DOMAIN,
)
from homeassistant.const import (
    ATTR_NAME,
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    MATCH_ALL,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CLEAR_CREDENTIALS,
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_ENABLE_IF_DISABLED,
    ATTR_LENGTH,
    ATTR_LOCK_ENTITY_ID,
    ATTR_OPERATION,
    ATTR_REASON,
    ATTR_SLOT,
    ATTR_SOURCE,
    ATTR_TARGET,
    ATTR_TEXT,
    ATTR_USER,
    ATTR_USERCODE,
    ATTR_VALID,
    ATTR_VALUE,
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    EVENT_LOCK_STATE_CHANGED,
    REASON_CONDITION_NOT_MET,
    REASON_UNKNOWN_CODE,
    REASON_USER_DISABLED,
    SERVICE_ADD_USER,
    SERVICE_CLEAR_CREDENTIAL,
    SERVICE_CLEAR_SLOT_CONDITION,
    SERVICE_CLEAR_USERCODE,
    SERVICE_DELETE_USER,
    SERVICE_DEOBFUSCATE_LOG,
    SERVICE_GENERATE_PIN,
    SERVICE_SET_CREDENTIAL,
    SERVICE_SET_SLOT_CONDITION,
    SERVICE_SET_USERCODE,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.domain import services
from custom_components.lock_code_manager.domain.allocation import SlotAllocationError
from custom_components.lock_code_manager.domain.config import (
    build_slot_unique_id,
)
from custom_components.lock_code_manager.domain.credentials import CredentialType
from custom_components.lock_code_manager.domain.events import CredentialOperation
from custom_components.lock_code_manager.domain.pin_generator import is_unsafe_pin
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.services import async_set_usercode
from custom_components.lock_code_manager.domain.util import mask_pin
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
)
from tests.providers.helpers import register_mock_service

from .common import LOCK_1_ENTITY_ID, reading_for


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


async def test_add_user_service_reads_the_locks_for_its_own_entry(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    The entry named in the call is the one its lock reads are made for.

    What allocation makes of a lock is settled by the owning entry's
    configuration, so a read made for nobody would consult the wrong one --
    and the service and the editor would stop agreeing about which numbers
    are free.
    """
    with reading_for() as read_for:
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
        await hass.async_block_till_done()

    assert read_for, "the service allocated a number without reading a lock"
    assert all(read is lock_code_manager_config_entry for read in read_for)


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


async def test_add_user_service_stores_a_padded_pin_stripped(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A PIN handed to the service with padding is stored without it.

    The service writes the entry's users directly, the way both flows do,
    so it is the third door onto the same storage -- and a submitted code
    is stripped before it is matched.
    """
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "Newcomer",
            CONF_PIN: " 9876 ",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["Newcomer"][CONF_PIN] == "9876"


async def test_add_user_service_rejects_a_whitespace_only_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """An enabled user whose PIN is only whitespace has nothing to program."""
    with pytest.raises(ServiceValidationError, match="without a PIN"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "Newcomer",
                CONF_PIN: "   ",
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
VALIDATE_EVENT_ENTITY_ID = "event.validate_service_alice_credential_used"
# A Schlage lock: a real provider whose ``supports_code_slot_events`` is False.
EVENT_BLIND_LOCK_ENTITY_ID = "lock.schlage_use_credential"

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


# Stands for the keypad the credential was typed on: an entity Lock Code
# Manager knows nothing about, which is the whole point of the action.
VALIDATE_SOURCE_ENTITY_ID = "sensor.front_door_keypad"


async def _call_use_credential(
    hass: HomeAssistant, data: dict, *, return_response: bool = True
) -> dict | None:
    """Call the use_credential action, defaulting the attribution fields."""
    return await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            ATTR_SOURCE: VALIDATE_SOURCE_ENTITY_ID,
            ATTR_TARGET: VALIDATE_LOCK_ENTITY_ID,
            **data,
        },
        blocking=True,
        return_response=return_response,
    )


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
        domain=DOMAIN,
        title="Validate Service",
        data=VALIDATE_CONFIG,
        unique_id="test_validate_service",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


def _slot_event_entity_id(
    hass: HomeAssistant, config_entry: MockConfigEntry, slot_num: int
) -> str:
    """Resolve a slot's credential-used entity by unique ID."""
    entity_id = er.async_get(hass).async_get_entity_id(
        EVENT_DOMAIN,
        DOMAIN,
        build_slot_unique_id(config_entry.entry_id, slot_num, EVENT_CREDENTIAL_USED),
    )
    assert entity_id
    return entity_id


@pytest.fixture(name="validate_entry_with_event_blind_lock")
async def validate_entry_with_event_blind_lock_fixture(hass: HomeAssistant):
    """
    Set up an entry holding one event-capable lock and one that is not.

    The blind one is a real Schlage lock rather than a doctored capability:
    ``supports_code_slot_events`` is the provider's own answer, and a
    monkeypatched one would only prove this test's idea of it.
    """
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)
    schlage_entry = MockConfigEntry(domain=SCHLAGE_DOMAIN)
    schlage_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "virtual",
        "virtual_blind_pair",
        suggested_object_id="virtual_validate_service",
        config_entry=virtual_entry,
    )
    assert lock_entity.entity_id == VALIDATE_LOCK_ENTITY_ID
    schlage_entity = ent_reg.async_get_or_create(
        "lock",
        SCHLAGE_DOMAIN,
        "schlage_blind_pair",
        suggested_object_id="schlage_use_credential",
        config_entry=schlage_entry,
    )
    assert schlage_entity.entity_id == EVENT_BLIND_LOCK_ENTITY_ID
    hass.states.async_set(VALIDATE_LOCK_ENTITY_ID, "locked")
    hass.states.async_set(EVENT_BLIND_LOCK_ENTITY_ID, "locked")
    hass.states.async_set(VALIDATE_CONDITION_ENTITY_ID, "off")

    for service_name, response in (
        ("get_codes", {EVENT_BLIND_LOCK_ENTITY_ID: {}}),
        ("add_code", None),
        ("delete_code", None),
    ):
        register_mock_service(
            hass, SCHLAGE_DOMAIN, service_name, AsyncMock(return_value=response)
        )

    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Validate Service",
        data={
            **VALIDATE_CONFIG,
            CONF_LOCKS: [VALIDATE_LOCK_ENTITY_ID, EVENT_BLIND_LOCK_ENTITY_ID],
        },
        unique_id="test_validate_blind_pair",
    )
    lcm_entry.add_to_hass(hass)
    with patch.object(SchlageLock, "async_is_integration_connected", return_value=True):
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()

        yield lcm_entry

        await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_use_credential_valid(hass: HomeAssistant, validate_entry) -> None:
    """A valid code returns the user with no failure reason."""
    response = await _call_use_credential(
        hass,
        {"config_entry_id": validate_entry.entry_id, ATTR_CODE: "1234"},
    )
    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}


async def test_use_credential_valid_by_title(
    hass: HomeAssistant, validate_entry
) -> None:
    """Either identifier names the same entry, as the sibling actions do."""
    response = await _call_use_credential(
        hass,
        {"config_entry_title": validate_entry.title, ATTR_CODE: "1234"},
    )
    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}


async def test_use_credential_unknown(hass: HomeAssistant, validate_entry) -> None:
    """A code no slot holds is rejected as unknown."""
    response = await _call_use_credential(
        hass,
        {"config_entry_id": validate_entry.entry_id, ATTR_CODE: "0000"},
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
async def test_use_credential_failure_reasons(
    hass: HomeAssistant, validate_entry, code: str, reason: str
) -> None:
    """A matched-but-inactive code reports why it did not validate."""
    response = await _call_use_credential(
        hass,
        {"config_entry_id": validate_entry.entry_id, ATTR_CODE: code},
    )
    assert response == {ATTR_VALID: False, ATTR_USER: None, ATTR_REASON: reason}


@pytest.mark.parametrize(
    "selector",
    [
        {"config_entry_id": "no_such_entry"},
        {"config_entry_title": "no such entry"},
    ],
)
async def test_use_credential_rejects_an_unknown_entry(
    hass: HomeAssistant, validate_entry, selector: dict
) -> None:
    """An entry nothing matches is refused the way the sibling actions refuse it."""
    with pytest.raises(
        ServiceValidationError, match="No lock code manager config entry"
    ):
        await _call_use_credential(hass, {**selector, ATTR_CODE: "1234"})


async def test_use_credential_without_return_response(
    hass: HomeAssistant, validate_entry
) -> None:
    """
    Asking without wanting the answer is allowed, and is then a no-op.

    The response is the action's only output, so OPTIONAL rather than ONLY
    is what keeps a caller that omits ``return_response`` from erroring.
    """
    response = await _call_use_credential(
        hass,
        {"config_entry_id": validate_entry.entry_id, ATTR_CODE: "1234"},
        return_response=False,
    )

    assert response is None


@pytest.mark.parametrize("code", ["   ", "\t\n"])
async def test_use_credential_refuses_a_whitespace_only_code(
    hass: HomeAssistant, validate_entry, code: str
) -> None:
    """
    A code that is only padding is refused by the schema, not answered.

    The schema strips before length-checking, so whitespace collapses to
    the empty string the sibling write actions already refuse. Answering
    it instead would report every entry with no PIN set as a match.
    """
    with pytest.raises(vol.Invalid):
        await _call_use_credential(
            hass, {"config_entry_id": validate_entry.entry_id, ATTR_CODE: code}
        )


async def test_use_credential_strips_padding_at_the_schema(
    hass: HomeAssistant, validate_entry
) -> None:
    """
    A padded code validates: the schema strips it before the action sees it.

    Distinct from the strip inside ``validate_credential`` -- this one is
    what makes the length check reject padding-only input, so it has to be
    shown not to reject a real code that merely arrives padded.
    """
    response = await _call_use_credential(
        hass,
        {"config_entry_id": validate_entry.entry_id, ATTR_CODE: " 1234 "},
    )
    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}


@pytest.mark.parametrize(
    "selector",
    [
        pytest.param(
            {"config_entry_id": "an_id", "config_entry_title": "a title"}, id="both"
        ),
        pytest.param({}, id="neither"),
    ],
)
async def test_use_credential_requires_exactly_one_entry_selector(
    hass: HomeAssistant, validate_entry, selector: dict
) -> None:
    """
    The entry is named by id or by title, never both and never neither.

    Accepting both would leave the action free to pick, and the two can
    disagree; accepting neither has no entry to answer about.
    """
    with pytest.raises(vol.Invalid):
        await _call_use_credential(hass, {**selector, ATTR_CODE: "1234"})


async def test_use_credential_on_an_entry_with_no_slots(hass: HomeAssistant) -> None:
    """
    An entry configuring no users answers unknown_code rather than erroring.

    Nothing can match, which is the same shape as a code no slot holds --
    a caller polling an entry mid-setup gets an answer, not an exception.
    """
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)
    lock_entity = er.async_get(hass).async_get_or_create(
        "lock",
        "virtual",
        "virtual_validate_no_slots",
        suggested_object_id="virtual_validate_no_slots",
        config_entry=virtual_entry,
    )
    hass.states.async_set(lock_entity.entity_id, "locked")

    empty_entry = MockConfigEntry(
        domain=DOMAIN,
        title="No Slots",
        data={CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: {}},
        unique_id="test_validate_no_slots",
    )
    empty_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(empty_entry.entry_id)
    await hass.async_block_till_done()

    response = await _call_use_credential(
        hass, {"config_entry_id": empty_entry.entry_id, ATTR_CODE: "1234"}
    )

    assert response == {
        ATTR_VALID: False,
        ATTR_USER: None,
        ATTR_REASON: REASON_UNKNOWN_CODE,
    }

    await hass.config_entries.async_unload(empty_entry.entry_id)


async def test_use_credential_matches_a_padded_stored_pin(hass: HomeAssistant) -> None:
    """
    A PIN already stored with padding validates against the code as entered.

    The config is written directly, which is the point: this simulates data
    that predates the write-path strip -- hand-edited .storage, a restored
    backup, or an older version -- so it cannot be produced through a path
    that would normalize it on the way in.
    """
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)
    lock_entity = er.async_get(hass).async_get_or_create(
        "lock",
        "virtual",
        "virtual_validate_padded",
        suggested_object_id="virtual_validate_padded",
        config_entry=virtual_entry,
    )
    hass.states.async_set(lock_entity.entity_id, "locked")

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Padded",
        data={
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: {
                1: {CONF_NAME: "dana", CONF_PIN: " 4321 ", CONF_ENABLED: True},
                2: {CONF_NAME: "blank", CONF_PIN: "   ", CONF_ENABLED: True},
            },
        },
        unique_id="test_validate_padded",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await _call_use_credential(
        hass, {"config_entry_id": entry.entry_id, ATTR_CODE: "4321"}
    ) == {ATTR_VALID: True, ATTR_USER: "dana", ATTR_REASON: None}

    # The whitespace-only slot holds no PIN, so nothing can match it -- least
    # of all a submission that is itself only padding.
    assert entry.runtime_data.slot_coordinators[2].pin_value is None
    with pytest.raises(vol.Invalid):
        await _call_use_credential(
            hass, {"config_entry_id": entry.entry_id, ATTR_CODE: "   "}
        )

    await hass.config_entries.async_unload(entry.entry_id)


async def test_use_credential_records_against_an_in_entry_lock(
    hass: HomeAssistant, validate_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A lock in the entry gets the use recorded on the slot's event entity.

    The recording is the event entity's own reading of the unified event,
    not a lock-shaped detour: the action reports something no lock
    observed, so the deprecated lock-state event -- which would have to
    claim a from/to state transition that never happened -- stays silent.
    """
    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
    deprecated = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    with caplog.at_level(logging.DEBUG):
        response = await _call_use_credential(
            hass, {"config_entry_id": validate_entry.entry_id, ATTR_CODE: "1234"}
        )
        await hass.async_block_till_done()

    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}
    assert [event.data for event in unified] == [
        {
            ATTR_NAME: "alice",
            ATTR_CONFIG_ENTRY_ID: validate_entry.entry_id,
            ATTR_CONFIG_ENTRY_TITLE: validate_entry.title,
            ATTR_SOURCE: VALIDATE_SOURCE_ENTITY_ID,
            ATTR_TARGET: VALIDATE_LOCK_ENTITY_ID,
            # A PIN is what the action validates, and nothing here watched
            # the lock move, so it never claims to know what it did.
            ATTR_CREDENTIAL_TYPE: CredentialType.PIN,
            ATTR_OPERATION: CredentialOperation.UNKNOWN,
        }
    ]
    assert deprecated == []

    state = hass.states.get(VALIDATE_EVENT_ENTITY_ID)
    assert state
    assert state.attributes[ATTR_EVENT_TYPE] == CredentialType.PIN
    assert state.attributes[ATTR_TARGET] == VALIDATE_LOCK_ENTITY_ID
    assert state.attributes[ATTR_SOURCE] == VALIDATE_SOURCE_ENTITY_ID
    assert [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ] == []


async def test_use_credential_records_only_for_the_named_user(
    hass: HomeAssistant, validate_entry
) -> None:
    """
    One user's use does not show up on another user's entity.

    The unified event names the person, not the slot they occupy, so the
    per-slot entity has to work out for itself whether the use is its own.
    """
    bob_event_entity_id = _slot_event_entity_id(hass, validate_entry, 2)
    before = hass.states.get(bob_event_entity_id)
    assert before

    await _call_use_credential(
        hass, {"config_entry_id": validate_entry.entry_id, ATTR_CODE: "1234"}
    )
    await hass.async_block_till_done()

    assert hass.states.get(bob_event_entity_id) == before
    alice = hass.states.get(VALIDATE_EVENT_ENTITY_ID)
    assert alice
    assert alice.attributes[ATTR_TARGET] == VALIDATE_LOCK_ENTITY_ID


async def test_use_credential_ignores_an_entry_that_is_not_ours(
    hass: HomeAssistant, validate_entry
) -> None:
    """
    A unified event from another entry is not this entry's to record.

    Two entries can manage the same lock, and both entities would see this
    event on the bus.
    """
    before = hass.states.get(VALIDATE_EVENT_ENTITY_ID)
    assert before

    hass.bus.async_fire(
        BUS_EVENT_CREDENTIAL_USED,
        event_data={
            ATTR_NAME: "alice",
            ATTR_CONFIG_ENTRY_ID: "some_other_entry",
            ATTR_CONFIG_ENTRY_TITLE: "Some Other Entry",
            ATTR_SOURCE: VALIDATE_SOURCE_ENTITY_ID,
            ATTR_TARGET: VALIDATE_LOCK_ENTITY_ID,
        },
    )
    await hass.async_block_till_done()

    assert hass.states.get(VALIDATE_EVENT_ENTITY_ID) == before


async def test_use_credential_against_an_event_blind_lock_in_the_entry(
    hass: HomeAssistant,
    validate_entry_with_event_blind_lock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A lock that reports nothing itself still gets uses recorded against it.

    Schlage and Akuvox never fire code slot events, and zwave-js-ui does not
    until it has learned it can. Whether the lock could have told us is
    beside the point when somebody else did: the credential is alice's, and
    what it acted on is payload rather than vocabulary.
    """
    entry = validate_entry_with_event_blind_lock
    event_entity_id = _slot_event_entity_id(hass, entry, 1)
    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
    before = hass.states.get(event_entity_id)
    assert before
    assert before.state == STATE_UNKNOWN
    blind_lock = entry.runtime_data.locks[EVENT_BLIND_LOCK_ENTITY_ID]
    assert blind_lock.supports_code_slot_events is False

    with caplog.at_level(logging.DEBUG):
        response = await _call_use_credential(
            hass,
            {
                "config_entry_id": entry.entry_id,
                ATTR_CODE: "1234",
                ATTR_TARGET: EVENT_BLIND_LOCK_ENTITY_ID,
            },
        )
        await hass.async_block_till_done()

    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}
    assert [event.data[ATTR_TARGET] for event in unified] == [
        EVENT_BLIND_LOCK_ENTITY_ID
    ]
    recorded = hass.states.get(event_entity_id)
    assert recorded.state != STATE_UNKNOWN
    assert recorded.attributes[ATTR_EVENT_TYPE] == CredentialType.PIN
    assert recorded.attributes[ATTR_TARGET] == EVENT_BLIND_LOCK_ENTITY_ID
    assert [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ] == []


async def test_use_credential_with_a_target_outside_the_entry(
    hass: HomeAssistant, validate_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A target Lock Code Manager does not manage records like any other.

    A cover is not a lock, is not in the entry, and could never be named by
    anything the entry knows -- and it is still alice's credential being
    used, so her entity is where that belongs. Only the unified event
    fires: the deprecated lock-shaped one would have to claim a from/to
    transition that never happened.
    """
    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
    deprecated = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
    before = hass.states.get(VALIDATE_EVENT_ENTITY_ID)
    assert before
    assert before.state == STATE_UNKNOWN

    with caplog.at_level(logging.DEBUG):
        response = await _call_use_credential(
            hass,
            {
                "config_entry_id": validate_entry.entry_id,
                ATTR_CODE: "1234",
                ATTR_TARGET: "cover.some_other_door",
            },
        )
        await hass.async_block_till_done()

    assert response == {ATTR_VALID: True, ATTR_USER: "alice", ATTR_REASON: None}
    assert [event.data[ATTR_TARGET] for event in unified] == ["cover.some_other_door"]
    assert deprecated == []
    recorded = hass.states.get(VALIDATE_EVENT_ENTITY_ID)
    assert recorded.state != STATE_UNKNOWN
    assert recorded.attributes[ATTR_TARGET] == "cover.some_other_door"
    assert [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ] == []


@pytest.mark.parametrize(
    "code", ["0000", "5678", "9999"], ids=["unknown", "disabled", "condition"]
)
async def test_use_credential_announces_nothing_when_invalid(
    hass: HomeAssistant, validate_entry, code: str
) -> None:
    """
    A code that does not validate is not a use, however it failed.

    The response is the whole answer, so an automation reacting to a
    rejection reads it there rather than from an event anyone could see.
    """
    fired = async_capture_events(hass, MATCH_ALL)

    response = await _call_use_credential(
        hass, {"config_entry_id": validate_entry.entry_id, ATTR_CODE: code}
    )
    await hass.async_block_till_done()

    assert response[ATTR_VALID] is False
    assert [
        event.event_type for event in fired if event.event_type.startswith(DOMAIN)
    ] == []


async def test_use_credential_never_publishes_the_source_entity_state(
    hass: HomeAssistant, validate_entry
) -> None:
    """
    Naming a source whose state is the code does not publish the code.

    ``source`` and ``target`` are data: nothing looks them up or reads
    them. A keypad entity that exposes what was typed is exactly the
    surface that has leaked cleartext PINs here before.
    """
    hass.states.async_set(VALIDATE_SOURCE_ENTITY_ID, "1234")
    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
    deprecated = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    await _call_use_credential(
        hass, {"config_entry_id": validate_entry.entry_id, ATTR_CODE: "1234"}
    )
    await hass.async_block_till_done()

    assert unified
    for event in [*unified, *deprecated]:
        assert "1234" not in json.dumps(event.data, default=str)


@pytest.mark.parametrize("missing", [ATTR_SOURCE, ATTR_TARGET])
async def test_use_credential_requires_both_attribution_fields(
    hass: HomeAssistant, validate_entry, missing: str
) -> None:
    """
    Neither end of the attribution is optional.

    Every consumer of the unified event can read both without a key test
    only because the schema refuses a call that omits either one.
    """
    data = {
        ATTR_SOURCE: VALIDATE_SOURCE_ENTITY_ID,
        ATTR_TARGET: VALIDATE_LOCK_ENTITY_ID,
        "config_entry_id": validate_entry.entry_id,
        ATTR_CODE: "1234",
    }
    del data[missing]

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, SERVICE_USE_CREDENTIAL, data, blocking=True
        )


async def test_set_credential_updates_the_user_not_just_the_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    The credential lands in the configuration, which is the whole point.

    ``set_usercode`` writes straight to a device, so the code it sets is one
    Lock Code Manager does not know about. This addresses the user, so the
    entry holds it and the sync carries it to every lock.
    """
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "test1",
            ATTR_CREDENTIAL_TYPE: "pin",
            ATTR_VALUE: "4321",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["test1"][CONF_PIN] == "4321"


async def test_set_credential_matches_a_name_the_way_it_is_said(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Case and surrounding whitespace do not have to match what was stored."""
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {
            "config_entry_id": entry.entry_id,
            CONF_NAME: "  TEST1 ",
            ATTR_CREDENTIAL_TYPE: "pin",
            ATTR_VALUE: "4321",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    # Stored under the name it already had, not re-keyed under what was typed.
    assert config.users["test1"][CONF_PIN] == "4321"


async def test_clear_credential_clears_and_disables(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Clearing takes the credential and the user's active state together.

    A user with no credential who still read as enabled would be advertised
    as able to get in while holding nothing anybody could present. This is
    the same pairing emptying the PIN field on the dashboard produces.
    """
    entry = lock_code_manager_config_entry
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_CREDENTIAL,
        {
            "config_entry_title": entry.title,
            CONF_NAME: "test1",
            ATTR_CREDENTIAL_TYPE: "pin",
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert not config.users["test1"][CONF_PIN]
    assert config.users["test1"][CONF_ENABLED] is False


@pytest.mark.parametrize(
    "service,extra",
    [
        (SERVICE_SET_CREDENTIAL, {ATTR_VALUE: "4321"}),
        (SERVICE_CLEAR_CREDENTIAL, {}),
    ],
)
async def test_credential_actions_refuse_an_unknown_user(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    service: str,
    extra: dict,
) -> None:
    """Naming nobody is the caller's mistake, not a silent no-op."""
    with pytest.raises(ServiceValidationError, match="No user named"):
        await hass.services.async_call(
            DOMAIN,
            service,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "Nobody",
                ATTR_CREDENTIAL_TYPE: "pin",
                **extra,
            },
            blocking=True,
        )


async def test_set_credential_refuses_a_kind_it_cannot_store(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A real credential kind that is not supported yet says so.

    RFID is in the vocabulary because providers report it; Lock Code Manager
    has nowhere to keep one. The caller should learn that rather than that
    the word was invalid.
    """
    with pytest.raises(ServiceValidationError, match="cannot store"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CREDENTIAL,
            {
                "config_entry_id": lock_code_manager_config_entry.entry_id,
                CONF_NAME: "test1",
                ATTR_CREDENTIAL_TYPE: "rfid",
                ATTR_VALUE: "abc123",
            },
            blocking=True,
        )


@pytest.mark.parametrize(
    "service,data,replacement",
    [
        (
            SERVICE_SET_USERCODE,
            {ATTR_CODE_SLOT: 1, ATTR_USERCODE: "4321"},
            "set_credential",
        ),
        (SERVICE_CLEAR_USERCODE, {ATTR_CODE_SLOT: 1}, "clear_credential"),
    ],
)
async def test_the_device_level_actions_say_they_are_deprecated(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
    service: str,
    data: dict,
    replacement: str,
) -> None:
    """
    They still work, and they say what to use instead.

    A warning rather than a note, because the caller has something to do
    about it: these write straight to a device, so the code they set is one
    Lock Code Manager treats as unmanaged.
    """
    caplog.clear()
    await hass.services.async_call(
        DOMAIN,
        service,
        {ATTR_LOCK_ENTITY_ID: LOCK_1_ENTITY_ID, **data},
        blocking=True,
    )
    await hass.async_block_till_done()

    deprecations = [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "deprecated" in record.getMessage()
    ]
    assert len(deprecations) == 1
    assert replacement in deprecations[0].getMessage()


async def test_credential_actions_refuse_a_user_holding_no_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
) -> None:
    """
    A user the configuration never numbered has no credential to change.

    Reachable from stored configuration: a users block with no slot
    assignment beside it leaves everybody unnumbered, which is exactly the
    shape ``EntryConfig`` skips when it builds its slot view.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"Unplaced": {CONF_PIN: "1234", CONF_ENABLED: True}},
        },
        unique_id="unplaced",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="holds no slot"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CLEAR_CREDENTIAL,
            {
                "config_entry_id": entry.entry_id,
                CONF_NAME: "Unplaced",
                ATTR_CREDENTIAL_TYPE: "pin",
            },
            blocking=True,
        )


async def test_set_credential_leaves_the_user_alone_by_default(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Giving somebody a credential is not the same as letting them in.

    A user cleared earlier stays off until somebody says otherwise, so a
    credential set on them is present and inert rather than quietly live.
    """
    entry = lock_code_manager_config_entry
    common = {"config_entry_id": entry.entry_id, ATTR_CREDENTIAL_TYPE: "pin"}

    await hass.services.async_call(
        DOMAIN, SERVICE_CLEAR_CREDENTIAL, {**common, CONF_NAME: "test1"}, blocking=True
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {**common, CONF_NAME: "test1", ATTR_VALUE: "4321"},
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["test1"][CONF_PIN] == "4321"
    assert config.users["test1"][CONF_ENABLED] is False


async def test_set_credential_can_enable_in_the_same_call(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    ``enable`` makes this the inverse of clearing rather than half of it.

    Clearing disables, so without this a caller undoing a clear needs a
    second action against a different entity to finish the job.
    """
    entry = lock_code_manager_config_entry
    common = {"config_entry_id": entry.entry_id, ATTR_CREDENTIAL_TYPE: "pin"}

    await hass.services.async_call(
        DOMAIN, SERVICE_CLEAR_CREDENTIAL, {**common, CONF_NAME: "test1"}, blocking=True
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {
            **common,
            CONF_NAME: "test1",
            ATTR_VALUE: "4321",
            ATTR_ENABLE_IF_DISABLED: True,
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    config = get_entry_config(hass.config_entries.async_get_entry(entry.entry_id))
    assert config.users["test1"][CONF_PIN] == "4321"
    assert config.users["test1"][CONF_ENABLED] is True
