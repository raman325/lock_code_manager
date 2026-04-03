"""Test the Schlage lock provider."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
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
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
    _make_tagged_name,
    _parse_tag,
)

from .service_provider_tests import (
    ServiceProviderConnectionTests,
    ServiceProviderDeviceAvailabilityTests,
    register_mock_service,
)

LOCK_ENTITY_ID = "lock.schlage_test_schlage_lock"


@pytest.fixture
async def schlage_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Schlage config entry."""
    entry = MockConfigEntry(domain=SCHLAGE_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def schlage_lock(
    hass: HomeAssistant, schlage_config_entry: MockConfigEntry
) -> SchlageLock:
    """Create a SchlageLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "schlage",
        "test_schlage_lock",
        config_entry=schlage_config_entry,
    )
    return SchlageLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        schlage_config_entry,
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
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_schlage_lcm")
    entry.add_to_hass(hass)
    return entry


# --- Alias fixtures for shared test mixins ---


@pytest.fixture
def provider_lock(schlage_lock: SchlageLock) -> SchlageLock:
    """Alias schlage_lock for shared test mixins."""
    return schlage_lock


@pytest.fixture
def provider_config_entry(schlage_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias schlage_config_entry for shared test mixins."""
    return schlage_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return SCHLAGE_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[SchlageLock]:
    """Return the provider lock class."""
    return SchlageLock


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestMakeTaggedName:
    """Tests for _make_tagged_name."""

    def test_with_name(self) -> None:
        """Test tagged name with friendly name."""
        assert _make_tagged_name(1, "Guest") == "[LCM:1] Guest"

    def test_without_name(self) -> None:
        """Test tagged name defaults to 'Code Slot N'."""
        assert _make_tagged_name(5) == "[LCM:5] Code Slot 5"

    def test_none_name(self) -> None:
        """Test tagged name with explicit None."""
        assert _make_tagged_name(3, None) == "[LCM:3] Code Slot 3"


class TestParseTag:
    """Tests for _parse_tag."""

    def test_valid_tag(self) -> None:
        """Test parsing a valid tag."""
        assert _parse_tag("[LCM:1] Guest") == (1, "Guest")

    def test_large_slot(self) -> None:
        """Test parsing a large slot number."""
        assert _parse_tag("[LCM:99] Family") == (99, "Family")

    def test_no_tag(self) -> None:
        """Test parsing name without a tag."""
        assert _parse_tag("Guest Code") == (None, "Guest Code")

    def test_empty_string(self) -> None:
        """Test parsing an empty string."""
        assert _parse_tag("") == (None, "")


# ---------------------------------------------------------------------------
# Provider property tests
# ---------------------------------------------------------------------------


async def test_domain_property(schlage_lock: SchlageLock) -> None:
    """Test that domain returns 'schlage'."""
    assert schlage_lock.domain == SCHLAGE_DOMAIN


async def test_supports_code_slot_events(schlage_lock: SchlageLock) -> None:
    """Test that Schlage locks do not support code slot events."""
    assert schlage_lock.supports_code_slot_events is False


async def test_usercode_scan_interval(schlage_lock: SchlageLock) -> None:
    """Test that scan interval is 5 minutes."""
    assert schlage_lock.usercode_scan_interval == timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Connection and availability tests (shared)
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for Schlage provider using shared mixin."""


class TestDeviceAvailability(ServiceProviderDeviceAvailabilityTests):
    """Device availability tests for Schlage provider using shared mixin."""

    availability_service = "get_codes"


# ---------------------------------------------------------------------------
# get_usercodes tests
# ---------------------------------------------------------------------------


async def test_get_usercodes_tagged_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes returns UNKNOWN for tagged occupied slots, EMPTY for others."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:1] Guest", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    codes = await schlage_lock.async_get_usercodes()

    assert codes[1] is SlotCode.UNKNOWN
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes when no codes exist on the lock."""
    mock_response = {LOCK_ENTITY_ID: {}}
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    codes = await schlage_lock.async_get_usercodes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_configured_slots(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
) -> None:
    """Test get_usercodes returns empty dict when no slots are configured."""
    codes = await schlage_lock.async_get_usercodes()
    assert codes == {}


async def test_get_usercodes_does_not_auto_tag(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that get_usercodes does not auto-tag untagged codes."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    codes = await schlage_lock.async_get_usercodes()

    # Untagged codes are not counted as occupied
    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY

    # No add_code call should have been made
    assert add_handler.call_count == 0


async def test_get_usercodes_duplicate_tag_uses_first(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that duplicate tags for the same slot use the first (by code_id sort)."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code_a": {"name": "[LCM:1] First", "code": "****"},
            "code_b": {"name": "[LCM:1] Second", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    codes = await schlage_lock.async_get_usercodes()

    assert codes[1] is SlotCode.UNKNOWN
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_ignores_tags_outside_managed_range(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that tagged codes outside managed slots are ignored."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:99] Outside", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    codes = await schlage_lock.async_get_usercodes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY


# ---------------------------------------------------------------------------
# Auto-tagging tests (_async_tag_unmanaged_codes)
# ---------------------------------------------------------------------------


async def test_tag_unmanaged_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that untagged codes with real PINs are auto-tagged to managed slots."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    await schlage_lock._async_tag_unmanaged_codes()

    # Verify add_code was called with the tagged name
    assert add_handler.call_count == 1
    add_call = add_handler.call_args[0][0]
    assert add_call.data["name"] == "[LCM:1] Guest"
    assert add_call.data["code"] == "1234"

    # Verify delete_code was called to remove the original
    assert delete_handler.call_count == 1
    delete_call = delete_handler.call_args[0][0]
    assert delete_call.data["name"] == "Guest"


async def test_tag_unmanaged_codes_skips_masked_pin(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that untagged codes with masked PINs are not auto-tagged."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    await schlage_lock._async_tag_unmanaged_codes()

    # No add_code call because the PIN is masked
    assert add_handler.call_count == 0


async def test_tag_unmanaged_codes_rollback_on_delete_failure(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that auto-tagging rolls back if delete of original fails."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    # delete_code fails (will be called for both original delete and rollback)
    delete_handler = AsyncMock(side_effect=HomeAssistantError("delete failed"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    await schlage_lock._async_tag_unmanaged_codes()

    # add_code called once for the tagging attempt
    assert add_handler.call_count == 1
    # delete_code called twice: once to delete original, once for rollback
    assert delete_handler.call_count == 2


async def test_tag_unmanaged_codes_no_managed_slots(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
) -> None:
    """Test that tagging is a no-op when no slots are configured."""
    add_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    await schlage_lock._async_tag_unmanaged_codes()

    assert add_handler.call_count == 0


# ---------------------------------------------------------------------------
# set_usercode tests
# ---------------------------------------------------------------------------


async def test_set_usercode(hass: HomeAssistant, schlage_lock: SchlageLock) -> None:
    """Test set_usercode adds a tagged code."""
    # get_codes returns no existing codes
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    result = await schlage_lock.async_set_usercode(1, "1234", "Guest")

    assert result is True
    assert add_handler.call_count == 1
    add_call = add_handler.call_args[0][0]
    assert add_call.data["name"] == "[LCM:1] Guest"
    assert add_call.data["code"] == "1234"


async def test_set_usercode_replaces_existing(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test set_usercode replaces an existing code on the same slot."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:1] Old Name", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    result = await schlage_lock.async_set_usercode(1, "5678", "New Name")

    assert result is True
    # add_code called with new tagged name
    add_call = add_handler.call_args[0][0]
    assert add_call.data["name"] == "[LCM:1] New Name"
    assert add_call.data["code"] == "5678"
    # old code deleted
    delete_call = delete_handler.call_args[0][0]
    assert delete_call.data["name"] == "[LCM:1] Old Name"


async def test_set_usercode_preserves_existing_name(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test set_usercode preserves the existing friendly name when no name is provided.

    When the name does not change (PIN-only update), the old code must be deleted
    first because Schlage rejects add_code with a duplicate name.
    """
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:1] Guest", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    # Track call order to verify delete happens before add
    call_order: list[str] = []
    add_handler.side_effect = lambda _: call_order.append("add")
    delete_handler.side_effect = lambda _: call_order.append("delete")

    result = await schlage_lock.async_set_usercode(1, "9999")

    assert result is True
    # Old code deleted first, then new code added with same tagged name
    assert delete_handler.call_count == 1
    delete_call = delete_handler.call_args[0][0]
    assert delete_call.data["name"] == "[LCM:1] Guest"
    assert add_handler.call_count == 1
    add_call = add_handler.call_args[0][0]
    assert add_call.data["name"] == "[LCM:1] Guest"
    assert add_call.data["code"] == "9999"
    assert call_order == ["delete", "add"]


async def test_set_usercode_service_failure(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test that set_usercode raises LockDisconnected on service failure."""
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    with pytest.raises(LockDisconnected, match="connection lost"):
        await schlage_lock.async_set_usercode(1, "1234")


# ---------------------------------------------------------------------------
# clear_usercode tests
# ---------------------------------------------------------------------------


async def test_clear_usercode(hass: HomeAssistant, schlage_lock: SchlageLock) -> None:
    """Test clear_usercode deletes the code for the given slot."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:1] Guest", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    result = await schlage_lock.async_clear_usercode(1)

    assert result is True
    assert delete_handler.call_count == 1
    delete_call = delete_handler.call_args[0][0]
    assert delete_call.data["name"] == "[LCM:1] Guest"


async def test_clear_usercode_already_empty(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test clear_usercode returns False when no code exists for the slot."""
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)

    result = await schlage_lock.async_clear_usercode(1)

    assert result is False


async def test_clear_usercode_service_failure(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test that clear_usercode raises LockDisconnected on service failure."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:1] Guest", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    delete_handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    with pytest.raises(LockDisconnected, match="connection lost"):
        await schlage_lock.async_clear_usercode(1)


# ---------------------------------------------------------------------------
# hard_refresh_codes tests
# ---------------------------------------------------------------------------


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test hard_refresh_codes calls tagging then returns usercodes."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "[LCM:2] Family", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with patch.object(schlage_lock, "_async_tag_unmanaged_codes") as mock_tag:
        codes = await schlage_lock.async_hard_refresh_codes()
        mock_tag.assert_awaited_once()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.UNKNOWN


# ---------------------------------------------------------------------------
# get_codes service error tests
# ---------------------------------------------------------------------------


async def test_get_codes_service_failure(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that get_usercodes raises LockDisconnected on service failure."""
    handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with pytest.raises(LockDisconnected, match="connection lost"):
        await schlage_lock.async_get_usercodes()


async def test_get_codes_service_validation_error(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that get_usercodes raises LockDisconnected on ServiceValidationError."""
    handler = AsyncMock(side_effect=ServiceValidationError("invalid entity"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with pytest.raises(LockDisconnected, match="invalid entity"):
        await schlage_lock.async_get_usercodes()


async def test_get_codes_malformed_entity_response(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that get_codes raises LockCodeManagerError on malformed entity response."""
    # Return a valid dict at the top level, but the entity sub-key is not a dict
    handler = AsyncMock(return_value={LOCK_ENTITY_ID: "not a dict"})
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with pytest.raises(LockCodeManagerError, match="malformed entity response"):
        await schlage_lock.async_get_usercodes()
