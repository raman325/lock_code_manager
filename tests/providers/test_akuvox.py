"""Test the Akuvox lock provider."""

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
from custom_components.lock_code_manager.providers.akuvox import (
    AKUVOX_DOMAIN,
    AkuvoxLock,
    _is_local_user,
    _make_tagged_name,
    _parse_tag,
)

from .service_provider_tests import (
    ServiceProviderConnectionTests,
    register_mock_service,
)

LOCK_ENTITY_ID = "lock.local_akuvox_test_relay_a"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def akuvox_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a local_akuvox config entry."""
    entry = MockConfigEntry(domain=AKUVOX_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def akuvox_lock(
    hass: HomeAssistant, akuvox_config_entry: MockConfigEntry
) -> AkuvoxLock:
    """Create an AkuvoxLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "local_akuvox",
        "test_relay_a",
        config_entry=akuvox_config_entry,
    )
    return AkuvoxLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        akuvox_config_entry,
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
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_akuvox_lcm")
    entry.add_to_hass(hass)
    return entry


# --- Alias fixtures for shared test mixins ---


@pytest.fixture
def provider_lock(akuvox_lock: AkuvoxLock) -> AkuvoxLock:
    """Alias akuvox_lock for shared test mixins."""
    return akuvox_lock


@pytest.fixture
def provider_config_entry(akuvox_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias akuvox_config_entry for shared test mixins."""
    return akuvox_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return AKUVOX_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[AkuvoxLock]:
    """Return the provider lock class."""
    return AkuvoxLock


def _make_user(
    device_id: str,
    name: str,
    private_pin: str = "",
    source_type: str | None = "1",
    user_type: str = "0",
) -> dict[str, Any]:
    """Create a user dict matching list_users response format."""
    return {
        "id": device_id,
        "name": name,
        "private_pin": private_pin,
        "source_type": source_type,
        "user_type": user_type,
    }


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for module-level helpers."""

    def test_make_tagged_name_with_name(self) -> None:
        """Test tagged name with friendly name."""
        assert _make_tagged_name(1, "Guest") == "[LCM:1] Guest"

    def test_make_tagged_name_without_name(self) -> None:
        """Test tagged name defaults to 'Code Slot N'."""
        assert _make_tagged_name(5) == "[LCM:5] Code Slot 5"

    def test_make_tagged_name_none_name(self) -> None:
        """Test tagged name with explicit None."""
        assert _make_tagged_name(3, None) == "[LCM:3] Code Slot 3"

    def test_parse_tag_valid(self) -> None:
        """Test parsing a valid tag."""
        assert _parse_tag("[LCM:1] Guest") == (1, "Guest")

    def test_parse_tag_large_slot(self) -> None:
        """Test parsing a large slot number."""
        assert _parse_tag("[LCM:99] Family") == (99, "Family")

    def test_parse_tag_no_tag(self) -> None:
        """Test parsing name without a tag."""
        assert _parse_tag("Guest Code") == (None, "Guest Code")

    def test_parse_tag_empty_string(self) -> None:
        """Test parsing an empty string."""
        assert _parse_tag("") == (None, "")

    def test_is_local_user_source_type_1(self) -> None:
        """A08S/E18C pattern: source_type '1' is local."""
        assert _is_local_user({"source_type": "1", "user_type": "0"}) is True

    def test_is_local_user_source_type_2(self) -> None:
        """A08S/E18C pattern: source_type '2' is cloud."""
        assert _is_local_user({"source_type": "2", "user_type": "0"}) is False

    def test_is_local_user_none_source_local_user_type(self) -> None:
        """X916 pattern: source_type None, user_type '-1' is local."""
        assert _is_local_user({"source_type": None, "user_type": "-1"}) is True

    def test_is_local_user_none_source_cloud_user_type(self) -> None:
        """X916 pattern: source_type None, user_type '0' is cloud."""
        assert _is_local_user({"source_type": None, "user_type": "0"}) is False

    def test_is_local_user_missing_source_type(self) -> None:
        """Missing source_type key falls back to user_type."""
        assert _is_local_user({"user_type": "-1"}) is True

    def test_is_local_user_empty_string_source_type(self) -> None:
        """Empty string source_type falls through to user_type check."""
        assert _is_local_user({"source_type": "", "user_type": "-1"}) is True
        assert _is_local_user({"source_type": "", "user_type": "0"}) is False

    def test_is_local_user_missing_both(self) -> None:
        """Missing both fields is not local."""
        assert _is_local_user({}) is False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


class TestProperties:
    """Tests for provider properties."""

    async def test_domain(self, akuvox_lock: AkuvoxLock) -> None:
        """Test domain property."""
        assert akuvox_lock.domain == AKUVOX_DOMAIN

    async def test_supports_code_slot_events(self, akuvox_lock: AkuvoxLock) -> None:
        """Test that Akuvox locks do not support code slot events."""
        assert akuvox_lock.supports_code_slot_events is False

    async def test_usercode_scan_interval(self, akuvox_lock: AkuvoxLock) -> None:
        """Test that scan interval is 2 minutes."""
        assert akuvox_lock.usercode_scan_interval == timedelta(minutes=2)


# ---------------------------------------------------------------------------
# Connection (shared)
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for Akuvox provider using shared mixin."""


# ---------------------------------------------------------------------------
# Get usercodes
# ---------------------------------------------------------------------------


class TestGetUsercodes:
    """Tests for async_get_usercodes."""

    async def test_get_usercodes_tagged_users(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test get_usercodes returns PINs for tagged users, EMPTY for unoccupied."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("100", "[LCM:1] Guest", "1234"),
                ],
            },
        }
        handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", handler)

        codes = await akuvox_lock.async_get_usercodes()

        assert codes[1] == "1234"
        assert codes[2] is SlotCode.EMPTY

    async def test_get_usercodes_no_users(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test get_usercodes when no users exist on the lock."""
        mock_response = {LOCK_ENTITY_ID: {"users": []}}
        handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", handler)

        codes = await akuvox_lock.async_get_usercodes()

        assert codes[1] is SlotCode.EMPTY
        assert codes[2] is SlotCode.EMPTY

    async def test_get_usercodes_no_configured_slots(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
    ) -> None:
        """Test get_usercodes returns empty dict when no slots are configured."""
        codes = await akuvox_lock.async_get_usercodes()
        assert codes == {}

    async def test_get_usercodes_does_not_auto_tag(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that async_get_usercodes does not auto-tag untagged users."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("200", "Visitor", "9999"),
                ],
            },
        }
        list_handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", list_handler)

        codes = await akuvox_lock.async_get_usercodes()

        # Untagged user should NOT appear in results (no auto-tagging)
        assert codes[1] is SlotCode.EMPTY
        assert codes[2] is SlotCode.EMPTY

    async def test_get_usercodes_skips_cloud_users(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that cloud users are ignored."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("300", "Cloud User", "1111", source_type="2"),
                ],
            },
        }
        handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", handler)

        codes = await akuvox_lock.async_get_usercodes()

        assert codes[1] is SlotCode.EMPTY
        assert codes[2] is SlotCode.EMPTY

    async def test_get_usercodes_tagged_user_no_pin(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that a tagged user with no PIN is reported as EMPTY."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("400", "[LCM:1] Empty Slot", ""),
                ],
            },
        }
        handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", handler)

        codes = await akuvox_lock.async_get_usercodes()

        assert codes[1] is SlotCode.EMPTY

    async def test_get_usercodes_tagged_outside_managed_range(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that tagged users outside managed slots are ignored."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("500", "[LCM:99] Outside", "5555"),
                ],
            },
        }
        handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", handler)

        codes = await akuvox_lock.async_get_usercodes()

        assert 99 not in codes
        assert codes[1] is SlotCode.EMPTY
        assert codes[2] is SlotCode.EMPTY


# ---------------------------------------------------------------------------
# Set usercode
# ---------------------------------------------------------------------------


class TestSetUsercode:
    """Tests for async_set_usercode."""

    async def test_set_usercode_new_user(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test setting a usercode creates a new user when none exists."""
        # list_users returns no users
        list_response = {LOCK_ENTITY_ID: {"users": []}}
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )

        add_calls: list[dict[str, Any]] = []

        async def _capture_add(call):
            add_calls.append(dict(call.data))

        register_mock_service(
            hass, AKUVOX_DOMAIN, "add_user", AsyncMock(side_effect=_capture_add)
        )

        result = await akuvox_lock.async_set_usercode(1, "1234", "Guest")

        assert result is True
        assert len(add_calls) == 1
        assert add_calls[0]["name"] == "[LCM:1] Guest"
        assert add_calls[0]["private_pin"] == "1234"

    async def test_set_usercode_existing_user(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test setting a usercode modifies an existing tagged user."""
        list_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("100", "[LCM:1] Guest", "1234"),
                ],
            },
        }
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )

        modify_calls: list[dict[str, Any]] = []

        async def _capture_modify(call):
            modify_calls.append(dict(call.data))

        register_mock_service(
            hass, AKUVOX_DOMAIN, "modify_user", AsyncMock(side_effect=_capture_modify)
        )

        result = await akuvox_lock.async_set_usercode(1, "5678", "Updated")

        assert result is True
        assert len(modify_calls) == 1
        assert modify_calls[0]["id"] == "100"
        assert modify_calls[0]["name"] == "[LCM:1] Updated"
        assert modify_calls[0]["private_pin"] == "5678"

    async def test_set_usercode_no_name_keeps_existing(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test setting a usercode without a name preserves the existing name."""
        list_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("100", "[LCM:1] Guest", "1234"),
                ],
            },
        }
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )

        modify_calls: list[dict[str, Any]] = []

        async def _capture_modify(call):
            modify_calls.append(dict(call.data))

        register_mock_service(
            hass, AKUVOX_DOMAIN, "modify_user", AsyncMock(side_effect=_capture_modify)
        )

        result = await akuvox_lock.async_set_usercode(1, "9999")

        assert result is True
        assert modify_calls[0]["name"] == "[LCM:1] Guest"

    async def test_set_usercode_service_failure(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test that service failures raise LockDisconnected."""
        list_response = {LOCK_ENTITY_ID: {"users": []}}
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )
        register_mock_service(
            hass,
            AKUVOX_DOMAIN,
            "add_user",
            AsyncMock(side_effect=HomeAssistantError("device offline")),
        )

        with pytest.raises(LockDisconnected, match="device offline"):
            await akuvox_lock.async_set_usercode(1, "1234")


# ---------------------------------------------------------------------------
# Clear usercode
# ---------------------------------------------------------------------------


class TestClearUsercode:
    """Tests for async_clear_usercode."""

    async def test_clear_usercode_existing(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test clearing a usercode deletes the user from the device."""
        list_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("100", "[LCM:1] Guest", "1234"),
                ],
            },
        }
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )

        delete_calls: list[dict[str, Any]] = []

        async def _capture_delete(call):
            delete_calls.append(dict(call.data))

        register_mock_service(
            hass, AKUVOX_DOMAIN, "delete_user", AsyncMock(side_effect=_capture_delete)
        )

        result = await akuvox_lock.async_clear_usercode(1)

        assert result is True
        assert len(delete_calls) == 1
        assert delete_calls[0]["id"] == "100"

    async def test_clear_usercode_already_empty(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test clearing returns False when the slot has no user."""
        list_response = {LOCK_ENTITY_ID: {"users": []}}
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )

        result = await akuvox_lock.async_clear_usercode(1)
        assert result is False

    async def test_clear_usercode_service_failure(
        self, hass: HomeAssistant, akuvox_lock: AkuvoxLock
    ) -> None:
        """Test that service failures raise LockDisconnected."""
        list_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("100", "[LCM:1] Guest", "1234"),
                ],
            },
        }
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=list_response)
        )
        register_mock_service(
            hass,
            AKUVOX_DOMAIN,
            "delete_user",
            AsyncMock(side_effect=HomeAssistantError("device offline")),
        )

        with pytest.raises(LockDisconnected, match="device offline"):
            await akuvox_lock.async_clear_usercode(1)


# ---------------------------------------------------------------------------
# list_users error handling
# ---------------------------------------------------------------------------


class TestListUsersErrors:
    """Tests for list_users service error handling."""

    async def test_list_users_service_failure(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that list_users failure raises LockDisconnected."""
        register_mock_service(
            hass,
            AKUVOX_DOMAIN,
            "list_users",
            AsyncMock(side_effect=HomeAssistantError("connection lost")),
        )

        with pytest.raises(LockDisconnected, match="connection lost"):
            await akuvox_lock.async_get_usercodes()

    async def test_list_users_invalid_response(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that a non-dict service response raises LockDisconnected.

        Home Assistant's service layer rejects non-dict return values when
        return_response=True, so the service call itself raises HomeAssistantError
        which our provider wraps as LockDisconnected.
        """
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value="not a dict")
        )

        with pytest.raises(LockDisconnected, match="Failed to list users"):
            await akuvox_lock.async_get_usercodes()

    async def test_list_users_malformed_entity_response(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that a malformed entity-level response raises LockCodeManagerError."""
        # Response is a dict but the entity key maps to a non-dict value
        register_mock_service(
            hass,
            AKUVOX_DOMAIN,
            "list_users",
            AsyncMock(return_value={LOCK_ENTITY_ID: "not a dict"}),
        )

        with pytest.raises(
            LockCodeManagerError, match="Malformed list_users entity response"
        ):
            await akuvox_lock.async_get_usercodes()


# ---------------------------------------------------------------------------
# Auto-tagging
# ---------------------------------------------------------------------------


class TestAutoTagging:
    """Tests for _async_tag_unmanaged_users."""

    async def test_tags_untagged_users(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that untagged local users with PINs are auto-tagged."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("200", "Visitor", "9999"),
                ],
            },
        }
        list_handler = AsyncMock(return_value=mock_response)
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", list_handler)

        modify_calls: list[dict[str, Any]] = []

        async def _capture_modify(call):
            modify_calls.append(dict(call.data))

        register_mock_service(
            hass, AKUVOX_DOMAIN, "modify_user", AsyncMock(side_effect=_capture_modify)
        )

        await akuvox_lock._async_tag_unmanaged_users()

        assert len(modify_calls) == 1
        assert modify_calls[0]["id"] == "200"
        assert modify_calls[0]["name"] == "[LCM:1] Visitor"

    async def test_failed_modify_does_not_consume_slot(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that a failed modify does not consume a slot number."""
        mock_response = {
            LOCK_ENTITY_ID: {
                "users": [
                    _make_user("200", "Visitor A", "1111"),
                    _make_user("201", "Visitor B", "2222"),
                ],
            },
        }
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", AsyncMock(return_value=mock_response)
        )

        modify_calls: list[dict[str, Any]] = []
        call_tracker: list[int] = []

        async def _failing_then_ok(call):
            call_tracker.append(1)
            if len(call_tracker) == 1:
                raise HomeAssistantError("device busy")
            modify_calls.append(dict(call.data))

        register_mock_service(
            hass, AKUVOX_DOMAIN, "modify_user", AsyncMock(side_effect=_failing_then_ok)
        )

        await akuvox_lock._async_tag_unmanaged_users()

        # First modify failed so slot 1 should be reused for the second user
        assert len(modify_calls) == 1
        assert modify_calls[0]["id"] == "201"
        assert modify_calls[0]["name"] == "[LCM:1] Visitor B"

    async def test_no_managed_slots_is_noop(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
    ) -> None:
        """Test that auto-tagging is a no-op when no slots are managed."""
        # No LCM config entry means no managed slots
        await akuvox_lock._async_tag_unmanaged_users()
        # No service calls should have been made (no list_users registered)


# ---------------------------------------------------------------------------
# Hard refresh
# ---------------------------------------------------------------------------


class TestHardRefresh:
    """Tests for async_hard_refresh_codes."""

    async def test_hard_refresh_tags_then_reads(
        self,
        hass: HomeAssistant,
        akuvox_lock: AkuvoxLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """Test that hard refresh auto-tags first, then reads codes."""
        # First call returns untagged user, second call returns tagged user
        untagged_response = {
            LOCK_ENTITY_ID: {
                "users": [_make_user("200", "Visitor", "9999")],
            },
        }
        tagged_response = {
            LOCK_ENTITY_ID: {
                "users": [_make_user("200", "[LCM:1] Visitor", "9999")],
            },
        }
        list_handler = AsyncMock(side_effect=[untagged_response, tagged_response])
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", list_handler)
        register_mock_service(hass, AKUVOX_DOMAIN, "modify_user", AsyncMock())

        codes = await akuvox_lock.async_hard_refresh_codes()

        assert codes[1] == "9999"
        assert codes[2] is SlotCode.EMPTY
