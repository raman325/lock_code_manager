"""Test the Schlage lock provider."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    WriteResult,
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
    _make_tagged_name,
    _parse_tag,
)
from tests.providers.helpers import (
    ProviderNativeTransportContractTests,
    ServiceProviderConnectionTests,
    ServiceProviderDeviceAvailabilityTests,
    register_mock_service,
)

from .conftest import LOCK_ENTITY_ID


def _pin_cred(slot: int, pin: str) -> Credential:
    """Build a known-Personal-Identification-Number credential for a slot."""
    return credential_from_slot(slot, SlotCredential.known(pin))


def _cred_ref(slot: int) -> CredentialRef:
    """Build a CredentialRef for a slot."""
    return CredentialRef(user_id=slot, type=CredentialType.PIN, slot=slot)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestMakeTaggedName:
    """Tests for _make_tagged_name."""

    @pytest.mark.parametrize(
        ("slot", "name", "expected"),
        [
            pytest.param(1, "Guest", "lcm:1:Guest", id="with-name"),
            pytest.param(5, None, "lcm:5:Code Slot 5", id="without-name"),
            pytest.param(3, None, "lcm:3:Code Slot 3", id="none-name"),
        ],
    )
    def test_make_tagged_name(self, slot: int, name: str | None, expected: str) -> None:
        """Test _make_tagged_name for various inputs."""
        assert _make_tagged_name(slot, name) == expected


class TestParseTag:
    """Tests for _parse_tag."""

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


class TestNativeTransportContract(ProviderNativeTransportContractTests):
    """Schlage routes a native ``OSError`` from ``get_codes`` to LockDisconnected."""

    # Reaches the schlage integration through BaseLock.async_call_service, whose
    # native transport exception is OSError (an unwrapped ConnectionError).
    native_transport_read_service = "get_codes"

    @pytest.fixture
    def provider_lock(
        self, schlage_lock: SchlageLock, simple_lcm_config_entry: MockConfigEntry
    ) -> SchlageLock:
        """A lock with managed slots, so the read actually calls the service.

        ``async_get_users`` short-circuits with no managed slots, never
        reaching the service seam, so the contract needs an LCM entry.
        """
        return schlage_lock


# ---------------------------------------------------------------------------
# async_get_users / async_get_usercodes tests
# ---------------------------------------------------------------------------


async def test_get_users_no_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_get_users when no codes exist on the lock returns empty users for managed slots."""
    mock_response = {LOCK_ENTITY_ID: {}}
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    users = await schlage_lock.async_get_users()
    user_map = {u.user_id: u for u in users}

    assert user_map[1].pin_credentials[0].state is SlotCredential.empty()
    assert user_map[2].pin_credentials[0].state is SlotCredential.empty()


async def test_get_users_no_configured_slots(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
) -> None:
    """Test async_get_users returns empty list when no slots are configured."""
    users = await schlage_lock.async_get_users()
    assert users == []


async def test_get_usercodes_base_projection_no_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_get_usercodes base projection returns empty for managed slots with no codes."""
    mock_response = {LOCK_ENTITY_ID: {}}
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    codes = await schlage_lock.async_get_usercodes()

    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.empty()


async def test_get_usercodes_no_configured_slots(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
) -> None:
    """Test async_get_usercodes returns empty dict when no slots are configured."""
    codes = await schlage_lock.async_get_usercodes()
    assert codes == {}


async def test_get_users_does_not_auto_tag(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that async_get_users does not auto-tag untagged codes."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    users = await schlage_lock.async_get_users()
    user_map = {u.user_id: u for u in users}

    # Untagged codes are not counted as occupied
    assert user_map[1].pin_credentials[0].state is SlotCredential.empty()
    assert user_map[2].pin_credentials[0].state is SlotCredential.empty()

    # No add_code call should have been made
    assert add_handler.call_count == 0


async def test_get_users_duplicate_tag_uses_first(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that duplicate tags for the same slot use the first (by code_id sort)."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code_a": {"name": "lcm:1:First", "code": "****"},
            "code_b": {"name": "lcm:1:Second", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    users = await schlage_lock.async_get_users()
    user_map = {u.user_id: u for u in users}

    assert user_map[1].pin_credentials[0].state is SlotCredential.unreadable()
    assert user_map[2].pin_credentials[0].state is SlotCredential.empty()


async def test_get_users_ignores_tags_outside_managed_range(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that tagged codes outside managed slots are ignored."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "lcm:99:Outside", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    users = await schlage_lock.async_get_users()
    user_map = {u.user_id: u for u in users}

    assert user_map[1].pin_credentials[0].state is SlotCredential.empty()
    assert user_map[2].pin_credentials[0].state is SlotCredential.empty()


# ---------------------------------------------------------------------------
# Auto-tagging tests (_async_tag_unmanaged_codes)
# ---------------------------------------------------------------------------


async def test_tag_unmanaged_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
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
    assert add_call.data["name"] == "lcm:1:Guest"
    assert add_call.data["code"] == "1234"

    # Verify delete_code was called to remove the original
    assert delete_handler.call_count == 1
    delete_call = delete_handler.call_args[0][0]
    assert delete_call.data["name"] == "Guest"


async def test_tag_unmanaged_codes_skips_masked_pin(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
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
    simple_lcm_config_entry: MockConfigEntry,
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


async def test_async_setup_idempotent_skips_repeat_tag_pass(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A second async_setup call must not re-run the tag pass.

    Reconnect can call async_setup again on the same provider instance;
    re-tagging there would produce double-tag / rename storms against a
    possibly-drifted device list.
    """
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=get_response)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "add_code", AsyncMock(return_value=None)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "delete_code", AsyncMock(return_value=None)
    )

    with patch.object(
        schlage_lock,
        "_async_run_tag_pass",
        wraps=schlage_lock._async_run_tag_pass,
    ) as mock_pass:
        await schlage_lock.async_setup(simple_lcm_config_entry)
        await schlage_lock.async_setup(simple_lcm_config_entry)

    assert mock_pass.await_count == 1
    assert schlage_lock._tagged_once is True


async def test_async_setup_retries_after_disconnect_during_first_pass(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A LockDisconnected during the first tag pass must leave _tagged_once False so reconnect retries."""
    untagged_codes = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=untagged_codes)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "add_code", AsyncMock(return_value=None)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "delete_code", AsyncMock(return_value=None)
    )

    # First pass: _async_add_code is patched to raise LockDisconnected on
    # every untagged entry. The tag pass must re-raise after logging so
    # _tagged_once stays False.
    with patch.object(
        schlage_lock,
        "_async_add_code",
        side_effect=LockDisconnected("offline"),
    ):
        with pytest.raises(LockDisconnected, match="disconnect during tag pass"):
            await schlage_lock.async_setup(simple_lcm_config_entry)
    assert schlage_lock._tagged_once is False

    # Reconnect: now the underlying add succeeds. The pass must actually
    # complete and the guard must be set so a further reconnect skips.
    await schlage_lock.async_setup(simple_lcm_config_entry)
    assert schlage_lock._tagged_once is True


async def test_async_setup_retries_after_disconnect_on_delete_step(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """LockDisconnected on the delete-original step also re-raises after the loop."""
    untagged_codes = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "Guest", "code": "1234"},
        },
    }
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=untagged_codes)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "add_code", AsyncMock(return_value=None)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "delete_code", AsyncMock(return_value=None)
    )

    # Patch only _async_delete_code so the add succeeds and the delete-
    # original failure path is exercised. The rollback (delete tagged)
    # would also be called; we stub both via the same patch.
    with patch.object(
        schlage_lock,
        "_async_delete_code",
        side_effect=LockDisconnected("delete offline"),
    ):
        with pytest.raises(LockDisconnected, match="disconnect during tag pass"):
            await schlage_lock.async_setup(simple_lcm_config_entry)
    assert schlage_lock._tagged_once is False


async def test_concurrent_set_credential_serialized_under_sequence_lock(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Concurrent async_set_credential calls must not interleave their read-modify-write."""
    in_section = [0]
    max_overlap = [0]

    async def get_codes_handler(call):
        in_section[0] += 1
        max_overlap[0] = max(max_overlap[0], in_section[0])
        # Yield so other tasks could observe overlap if sequencing fails
        await asyncio.sleep(0)
        in_section[0] -= 1
        return {LOCK_ENTITY_ID: {}}

    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_codes_handler)
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "add_code", AsyncMock(return_value=None)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "delete_code", AsyncMock(return_value=None)
    )

    await asyncio.gather(
        schlage_lock.async_set_credential(
            1,
            _pin_cred(1, "1111"),
            "1111",
            name=None,
            source="direct",
        ),
        schlage_lock.async_set_credential(
            2,
            _pin_cred(2, "2222"),
            "2222",
            name=None,
            source="direct",
        ),
        schlage_lock.async_set_credential(
            3,
            _pin_cred(3, "3333"),
            "3333",
            name=None,
            source="direct",
        ),
    )

    assert max_overlap[0] == 1


# ---------------------------------------------------------------------------
# async_set_credential tests
# ---------------------------------------------------------------------------


async def test_set_credential_replaces_existing(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test async_set_credential replaces an existing code on the same slot."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "lcm:1:Old Name", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(return_value=None)
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    result = await schlage_lock.async_set_credential(
        1,
        _pin_cred(1, "5678"),
        "5678",
        name="New Name",
        source="direct",
    )

    assert result is WriteResult.CONFIRMED
    # add_code called with new tagged name
    add_call = add_handler.call_args[0][0]
    assert add_call.data["name"] == "lcm:1:New Name"
    assert add_call.data["code"] == "5678"
    # Both old name and new tagged name deleted (eventual consistency guard)
    assert delete_handler.call_count == 2
    deleted_names = {call[0][0].data["name"] for call in delete_handler.call_args_list}
    assert deleted_names == {"lcm:1:Old Name", "lcm:1:New Name"}


async def test_set_credential_preserves_existing_name(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """
    Test async_set_credential preserves the existing friendly name when no name is provided.

    When the name does not change (Personal Identification Number-only update), the old
    code must be deleted first because Schlage rejects add_code with a duplicate name.
    """
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "lcm:1:Guest", "code": "****"},
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

    result = await schlage_lock.async_set_credential(
        1,
        _pin_cred(1, "9999"),
        "9999",
        name=None,
        source="direct",
    )

    assert result is WriteResult.CONFIRMED
    # Name unchanged so existing_full_name == tagged_name, deduplicated to 1 delete
    assert delete_handler.call_count == 1
    delete_call = delete_handler.call_args[0][0]
    assert delete_call.data["name"] == "lcm:1:Guest"
    assert add_handler.call_count == 1
    add_call = add_handler.call_args[0][0]
    assert add_call.data["name"] == "lcm:1:Guest"
    assert add_call.data["code"] == "9999"
    assert call_order == ["delete", "add"]


async def test_set_credential_migrates_legacy_format_tag_on_write(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """
    Touching a legacy ``[LCM:<slot>]``-tagged code rewrites it to ``lcm:<slot>:`` on the next write.

    Pre-PR-C installs have codes named ``[LCM:1] Old``. The tolerant
    parser already discovers them by slot (via #1238), so the provider
    finds the code at slot 1; the rewrite happens implicitly because
    ``_make_tagged_name`` now produces the canonical format. The lock
    sees a delete of the legacy name and an add of the canonical
    name, completing the per-code migration.
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

    deleted_names: set[str] = set()
    delete_handler.side_effect = lambda call: deleted_names.add(call.data["name"])

    result = await schlage_lock.async_set_credential(
        1,
        _pin_cred(1, "9999"),
        "9999",
        name=None,
        source="direct",
    )

    assert result is WriteResult.CONFIRMED
    add_call = add_handler.call_args[0][0]
    # The new value is written under the canonical tag, NOT the legacy tag.
    assert add_call.data["name"] == "lcm:1:Guest"
    # The legacy-tagged code is cleaned up. (Both names show up if the
    # provider deletes the pre-existing legacy entry and the new entry
    # in a single transaction; the important assertion is that the
    # legacy name is gone after the write completes.)
    assert "[LCM:1] Guest" in deleted_names


async def test_set_credential_already_exists_treated_as_success(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """
    Test that 'already exists' on add_code is treated as success.

    Schlage's cloud API has eventual consistency: a delete may not propagate
    before the add, causing 'already exists'.  Since Personal Identification
    Numbers are write-only, we can't verify the value but the code IS on the lock.
    """
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(
        side_effect=HomeAssistantError(
            'A PIN code with the name "lcm:1:Guest" already exists on the lock'
        )
    )
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    result = await schlage_lock.async_set_credential(
        1,
        _pin_cred(1, "1234"),
        "1234",
        name="Guest",
        source="direct",
    )

    assert result is WriteResult.CONFIRMED


async def test_set_credential_non_exists_error_still_raises(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test that add_code errors other than 'already exists' still raise."""
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    delete_handler = AsyncMock(return_value=None)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    with pytest.raises(LockOperationFailed, match="connection lost"):
        await schlage_lock.async_set_credential(
            1,
            _pin_cred(1, "1234"),
            "1234",
            name=None,
            source="direct",
        )


async def test_set_credential_service_failure(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test that async_set_credential raises LockOperationFailed on service failure."""
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    add_handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "add_code", add_handler)

    with pytest.raises(LockOperationFailed, match="connection lost"):
        await schlage_lock.async_set_credential(
            1,
            _pin_cred(1, "1234"),
            "1234",
            name=None,
            source="direct",
        )


# ---------------------------------------------------------------------------
# async_delete_credential tests
# ---------------------------------------------------------------------------


async def test_delete_credential_already_empty(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test async_delete_credential returns False when no code exists for the slot."""
    get_response = {LOCK_ENTITY_ID: {}}
    get_handler = AsyncMock(return_value=get_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)

    result = await schlage_lock.async_delete_credential(_cred_ref(1))

    assert result is False


async def test_delete_credential_service_failure(
    hass: HomeAssistant, schlage_lock: SchlageLock
) -> None:
    """Test that async_delete_credential raises LockOperationFailed on service failure."""
    get_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "lcm:1:Guest", "code": "****"},
        },
    }
    get_handler = AsyncMock(return_value=get_response)
    delete_handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", get_handler)
    register_mock_service(hass, SCHLAGE_DOMAIN, "delete_code", delete_handler)

    with pytest.raises(LockOperationFailed, match="connection lost"):
        await schlage_lock.async_delete_credential(_cred_ref(1))


# ---------------------------------------------------------------------------
# hard_refresh_codes tests
# ---------------------------------------------------------------------------


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test hard_refresh_codes calls tagging then returns usercodes via base projection."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "code1": {"name": "lcm:2:Family", "code": "****"},
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with patch.object(schlage_lock, "_async_run_tag_pass") as mock_tag:
        codes = await schlage_lock.async_hard_refresh_codes()
        mock_tag.assert_awaited_once()

    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.unreadable()


# ---------------------------------------------------------------------------
# get_codes service error tests
# ---------------------------------------------------------------------------


async def test_get_codes_service_failure(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that async_get_users raises LockOperationFailed on service failure."""
    handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with pytest.raises(LockOperationFailed, match="connection lost"):
        await schlage_lock.async_get_users()


async def test_get_codes_service_validation_error(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that async_get_users raises LockOperationFailed on ServiceValidationError."""
    handler = AsyncMock(side_effect=ServiceValidationError("invalid entity"))
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with pytest.raises(LockOperationFailed, match="invalid entity"):
        await schlage_lock.async_get_users()


async def test_get_codes_malformed_entity_response(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that get_codes raises LockCodeManagerError on malformed entity response."""
    # Return a valid dict at the top level, but the entity sub-key is not a dict
    handler = AsyncMock(return_value={LOCK_ENTITY_ID: "not a dict"})
    register_mock_service(hass, SCHLAGE_DOMAIN, "get_codes", handler)

    with pytest.raises(LockCodeManagerError, match="malformed entity response"):
        await schlage_lock.async_get_users()


# ---------------------------------------------------------------------------
# Base orchestration end-to-end (primitive layer, no connection guard)
# ---------------------------------------------------------------------------


async def test_base_orchestration_set_and_get(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """async_set_credential + async_get_usercodes (base projection) shows unreadable after set."""
    get_response = {LOCK_ENTITY_ID: {}}
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=get_response)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "add_code", AsyncMock(return_value=None)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "delete_code", AsyncMock(return_value=None)
    )

    await schlage_lock.async_set_credential(
        1,
        _pin_cred(1, "9999"),
        "9999",
        name="test",
        source="direct",
    )

    # After setting, the lock reports the slot as unreadable (write-only Personal Identification Number)
    mock_after = {LOCK_ENTITY_ID: {"c1": {"name": "lcm:1:test", "code": "****"}}}
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=mock_after)
    )

    codes = await schlage_lock.async_get_usercodes()
    assert codes[1] is SlotCredential.unreadable()
    assert codes[2] is SlotCredential.empty()


async def test_base_orchestration_clear(
    hass: HomeAssistant,
    schlage_lock: SchlageLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """async_delete_credential + async_get_usercodes (base projection) shows empty after clear."""
    get_response = {LOCK_ENTITY_ID: {"c1": {"name": "lcm:1:Guest", "code": "****"}}}
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=get_response)
    )
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "delete_code", AsyncMock(return_value=None)
    )

    await schlage_lock.async_delete_credential(_cred_ref(1))

    # After clearing, the lock reports empty
    empty_response = {LOCK_ENTITY_ID: {}}
    register_mock_service(
        hass, SCHLAGE_DOMAIN, "get_codes", AsyncMock(return_value=empty_response)
    )
    codes = await schlage_lock.async_get_usercodes()
    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.empty()
