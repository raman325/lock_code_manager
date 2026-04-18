"""Test the Akuvox lock provider."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

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
from tests.providers.helpers import (
    ServiceProviderConnectionTests,
    register_mock_service,
)

from .conftest import LOCK_ENTITY_ID, make_user

# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Tests for module-level helpers."""

    @pytest.mark.parametrize(
        ("slot", "name", "expected"),
        [
            pytest.param(1, "Guest", "[LCM:1] Guest", id="with-name"),
            pytest.param(5, None, "[LCM:5] Code Slot 5", id="without-name"),
            pytest.param(3, None, "[LCM:3] Code Slot 3", id="none-name"),
        ],
    )
    def test_make_tagged_name(self, slot: int, name: str | None, expected: str) -> None:
        """Test _make_tagged_name for various inputs."""
        assert _make_tagged_name(slot, name) == expected

    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            pytest.param("[LCM:1] Guest", (1, "Guest"), id="valid-tag"),
            pytest.param("[LCM:99] Family", (99, "Family"), id="large-slot"),
            pytest.param("Guest Code", (None, "Guest Code"), id="no-tag"),
            pytest.param("", (None, ""), id="empty-string"),
        ],
    )
    def test_parse_tag(self, input_str: str, expected: tuple[int | None, str]) -> None:
        """Test _parse_tag for various inputs."""
        assert _parse_tag(input_str) == expected

    @pytest.mark.parametrize(
        ("user_data", "expected"),
        [
            pytest.param(
                {"source_type": "1", "user_type": "0"},
                True,
                id="source-type-1-local",
            ),
            pytest.param(
                {"source_type": "2", "user_type": "0"},
                False,
                id="source-type-2-cloud",
            ),
            pytest.param(
                {"source_type": None, "user_type": "-1"},
                True,
                id="none-source-local-user-type",
            ),
            pytest.param(
                {"source_type": None, "user_type": "0"},
                False,
                id="none-source-cloud-user-type",
            ),
            pytest.param(
                {"user_type": "-1"},
                True,
                id="missing-source-type-local",
            ),
            pytest.param(
                {"source_type": "", "user_type": "-1"},
                True,
                id="empty-source-type-local",
            ),
            pytest.param(
                {"source_type": "", "user_type": "0"},
                False,
                id="empty-source-type-cloud",
            ),
            pytest.param({}, False, id="missing-both"),
        ],
    )
    def test_is_local_user(self, user_data: dict, expected: bool) -> None:
        """Test _is_local_user for various user data patterns."""
        assert _is_local_user(user_data) is expected


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
                    make_user("100", "[LCM:1] Guest", "1234"),
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
                    make_user("200", "Visitor", "9999"),
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
                    make_user("300", "Cloud User", "1111", source_type="2"),
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
                    make_user("400", "[LCM:1] Empty Slot", ""),
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
                    make_user("500", "[LCM:99] Outside", "5555"),
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
                    make_user("100", "[LCM:1] Guest", "1234"),
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
                    make_user("100", "[LCM:1] Guest", "1234"),
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
                    make_user("100", "[LCM:1] Guest", "1234"),
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
                    make_user("100", "[LCM:1] Guest", "1234"),
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

        # The base async_call_service wrapper re-raises any failure as
        # LockDisconnected with the standard "Service call X.Y failed" prefix.
        with pytest.raises(
            LockDisconnected, match="Service call local_akuvox.list_users failed"
        ):
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
                    make_user("200", "Visitor", "9999"),
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
                    make_user("200", "Visitor A", "1111"),
                    make_user("201", "Visitor B", "2222"),
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
                "users": [make_user("200", "Visitor", "9999")],
            },
        }
        tagged_response = {
            LOCK_ENTITY_ID: {
                "users": [make_user("200", "[LCM:1] Visitor", "9999")],
            },
        }
        list_handler = AsyncMock(side_effect=[untagged_response, tagged_response])
        register_mock_service(hass, AKUVOX_DOMAIN, "list_users", list_handler)
        register_mock_service(hass, AKUVOX_DOMAIN, "modify_user", AsyncMock())

        codes = await akuvox_lock.async_hard_refresh_codes()

        assert codes[1] == "9999"
        assert codes[2] is SlotCode.EMPTY
