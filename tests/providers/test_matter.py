"""Test the Matter lock provider."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
)
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.matter import (
    MATTER_DOMAIN,
    MatterLock,
)

from .service_provider_tests import (
    ServiceProviderConnectionTests,
    ServiceProviderDeviceAvailabilityTests,
    register_mock_service,
)

LOCK_ENTITY_ID = "lock.matter_test_matter_lock"


@pytest.fixture
async def matter_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Matter config entry."""
    entry = MockConfigEntry(domain=MATTER_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def matter_lock(
    hass: HomeAssistant, matter_config_entry: MockConfigEntry
) -> MatterLock:
    """Create a MatterLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "matter",
        "test_matter_lock",
        config_entry=matter_config_entry,
    )
    return MatterLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        matter_config_entry,
        lock_entity,
    )


@pytest.fixture
async def lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Lock Code Manager config entry that manages slots 1 and 2."""
    config = {
        CONF_LOCKS: [LOCK_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_matter_lcm")
    entry.add_to_hass(hass)
    return entry


# --- Alias fixtures for shared test mixins ---


@pytest.fixture
def provider_lock(matter_lock: MatterLock) -> MatterLock:
    """Alias matter_lock for shared test mixins."""
    return matter_lock


@pytest.fixture
def provider_config_entry(matter_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias matter_config_entry for shared test mixins."""
    return matter_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return MATTER_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[MatterLock]:
    """Return the provider lock class."""
    return MatterLock


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


async def test_domain_property(matter_lock: MatterLock) -> None:
    """Test that domain returns 'matter'."""
    assert matter_lock.domain == MATTER_DOMAIN


async def test_supports_code_slot_events(matter_lock: MatterLock) -> None:
    """Test that Matter locks do not support code slot events."""
    assert matter_lock.supports_code_slot_events is False


async def test_usercode_scan_interval(matter_lock: MatterLock) -> None:
    """Test that scan interval is 5 minutes."""
    assert matter_lock.usercode_scan_interval == timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Connection and availability tests (shared)
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for Matter provider using shared mixin."""


class TestDeviceAvailability(ServiceProviderDeviceAvailabilityTests):
    """Device availability tests for Matter provider using shared mixin."""

    availability_service = "get_lock_info"


# ---------------------------------------------------------------------------
# Setup tests
# ---------------------------------------------------------------------------


async def test_setup(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup validates lock supports user management and PIN credentials."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "supports_user_management": True,
            "supported_credential_types": ["pin"],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_info", handler)

    await matter_lock.async_setup(lcm_config_entry)
    assert handler.call_count == 1


async def test_setup_unsupported_lock(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup raises when lock does not support user management."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "supports_user_management": False,
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_info", handler)

    with pytest.raises(LockCodeManagerError, match="does not support user management"):
        await matter_lock.async_setup(lcm_config_entry)


async def test_setup_no_pin_support(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup raises when lock supports users but not PIN credentials."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "supports_user_management": True,
            "supported_credential_types": ["rfid"],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_info", handler)

    with pytest.raises(LockCodeManagerError, match="does not support PIN credentials"):
        await matter_lock.async_setup(lcm_config_entry)


# ---------------------------------------------------------------------------
# get_usercodes tests
# ---------------------------------------------------------------------------


async def test_get_usercodes(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes returns UNKNOWN for occupied, EMPTY for cleared slots."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "user_name": "slot1",
                    "credentials": [
                        {
                            "credential_type": "pin",
                            "credential_index": 1,
                        }
                    ],
                },
            ],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_get_usercodes()

    assert codes[1] is SlotCode.UNKNOWN
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_users(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes when no users exist on the lock."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_get_usercodes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_configured_slots(
    hass: HomeAssistant,
    matter_lock: MatterLock,
) -> None:
    """Test get_usercodes returns empty dict when no slots are configured."""
    codes = await matter_lock.async_get_usercodes()
    assert codes == {}


# ---------------------------------------------------------------------------
# set_usercode tests
# ---------------------------------------------------------------------------


async def test_set_usercode(hass: HomeAssistant, matter_lock: MatterLock) -> None:
    """Test set_usercode calls the correct Matter services."""
    calls: list[dict[str, Any]] = []

    async def _capture_call(call):
        calls.append({"service": call.service, "data": dict(call.data)})
        return {LOCK_ENTITY_ID: {}}

    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_credential", AsyncMock(side_effect=_capture_call)
    )
    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_user", AsyncMock(side_effect=_capture_call)
    )

    result = await matter_lock.async_set_usercode(1, "1234", "User One")

    assert result is True
    assert len(calls) == 2
    # First call: set_lock_credential
    assert calls[0]["service"] == "set_lock_credential"
    assert calls[0]["data"]["credential_type"] == "pin"
    assert calls[0]["data"]["credential_data"] == "1234"
    assert calls[0]["data"]["credential_index"] == 1
    # Second call: set_lock_user
    assert calls[1]["service"] == "set_lock_user"
    assert calls[1]["data"]["user_name"] == "User One"


async def test_set_usercode_no_name(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test set_usercode without a name only calls set_lock_credential."""
    calls: list[str] = []

    async def _capture_call(call):
        calls.append(call.service)
        return {LOCK_ENTITY_ID: {}}

    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_credential", AsyncMock(side_effect=_capture_call)
    )
    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_user", AsyncMock(side_effect=_capture_call)
    )

    result = await matter_lock.async_set_usercode(3, "9999")

    assert result is True
    assert calls == ["set_lock_credential"]


# ---------------------------------------------------------------------------
# clear_usercode tests
# ---------------------------------------------------------------------------


async def test_clear_usercode(hass: HomeAssistant, matter_lock: MatterLock) -> None:
    """Test clear_usercode calls clear_lock_credential when credential exists."""
    credential_status_response = {
        LOCK_ENTITY_ID: {"credential_exists": True},
    }
    clear_response = {LOCK_ENTITY_ID: {}}

    handler_status = AsyncMock(return_value=credential_status_response)
    handler_clear = AsyncMock(return_value=clear_response)
    register_mock_service(
        hass, MATTER_DOMAIN, "get_lock_credential_status", handler_status
    )
    register_mock_service(hass, MATTER_DOMAIN, "clear_lock_credential", handler_clear)

    result = await matter_lock.async_clear_usercode(1)

    assert result is True
    assert handler_status.call_count == 1
    assert handler_clear.call_count == 1


async def test_clear_usercode_already_empty(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test clear_usercode returns False when the credential does not exist."""
    credential_status_response = {
        LOCK_ENTITY_ID: {"credential_exists": False},
    }
    handler_status = AsyncMock(return_value=credential_status_response)
    handler_clear = AsyncMock(return_value={LOCK_ENTITY_ID: {}})
    register_mock_service(
        hass, MATTER_DOMAIN, "get_lock_credential_status", handler_status
    )
    register_mock_service(hass, MATTER_DOMAIN, "clear_lock_credential", handler_clear)

    result = await matter_lock.async_clear_usercode(2)

    assert result is False
    # Only the credential status check should have been called
    assert handler_status.call_count == 1
    assert handler_clear.call_count == 0


# ---------------------------------------------------------------------------
# hard_refresh_codes tests
# ---------------------------------------------------------------------------


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test hard_refresh_codes returns same result as get_usercodes."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "credentials": [
                        {"credential_type": "pin", "credential_index": 2},
                    ],
                }
            ],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_hard_refresh_codes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.UNKNOWN


# ---------------------------------------------------------------------------
# Service error tests
# ---------------------------------------------------------------------------


async def test_service_call_failure_raises_lock_disconnected(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test that Matter service failures raise LockDisconnected."""
    handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, MATTER_DOMAIN, "set_lock_credential", handler)

    with pytest.raises(LockDisconnected, match="connection lost"):
        await matter_lock.async_set_usercode(1, "1234")


async def test_get_usercodes_multiple_credential_types(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that only PIN credentials are considered, not other types like RFID."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "credentials": [
                        {"credential_type": "rfid", "credential_index": 1},
                        {"credential_type": "pin", "credential_index": 2},
                    ],
                },
            ],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_get_usercodes()

    # Slot 1 has only RFID credential, not PIN
    assert codes[1] is SlotCode.EMPTY
    # Slot 2 has a PIN credential
    assert codes[2] is SlotCode.UNKNOWN
