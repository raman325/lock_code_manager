"""Full lifecycle E2E tests for Matter lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from matter_server.client.models.node import MatterNode
from matter_server.common.models import EventType, MatterNodeEvent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import TICK_INTERVAL
from custom_components.lock_code_manager.domain.credentials import (
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.matter import MatterLock

# Module path where lock_helpers functions are imported in the provider
from tests.common import in_sync_entity_id
from tests.conftest import async_advance_time, async_initial_tick

_PROVIDER_MODULE = "custom_components.lock_code_manager.providers.matter"


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Matter provider."""

    async def test_provider_discovered_as_matter(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Matter lock and creates a MatterLock."""
        lock = lcm_config_entry.runtime_data.locks.get(lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, MatterLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_matter_lock.coordinator is not None


class TestSetAndClearUsercodes:
    """Verify set/clear operations call the correct Matter helpers via base orchestration."""

    async def test_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Set a code via the base orchestration and verify set_lock_credential was called."""
        matter_mock_helpers["set_lock_credential"].reset_mock()
        matter_mock_helpers["get_lock_users"].reset_mock()
        matter_mock_helpers["set_lock_user"].reset_mock()
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
        ):
            result = await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert result is WriteResult.CONFIRMED
        assert matter_mock_helpers["set_lock_credential"].call_count >= 1

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Clear a code via the base orchestration and verify clear_lock_credential was called."""
        matter_mock_helpers["clear_lock_credential"].reset_mock()
        matter_mock_helpers["get_lock_users"].reset_mock()
        # Slot 2 is occupied (user_index 2) in the default fixture helpers
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_user",
                matter_mock_helpers["clear_lock_user"],
            ),
        ):
            result = await e2e_matter_lock.async_clear_usercode(2)

        assert result is True
        assert matter_mock_helpers["clear_lock_credential"].call_count >= 1

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """
        After set, the coordinator has the optimistic UNREADABLE_CODE value.

        Matter PINs are write-only so optimistic updates use UNREADABLE_CODE
        instead of the actual PIN value. The push comes from async_set_credential.
        """
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
        ):
            await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert (
            e2e_matter_lock.coordinator.data.get(pin_address(4))
            is SlotCredential.unreadable()
        )

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """After clear, the coordinator has SlotCredential.empty()."""
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_user",
                matter_mock_helpers["clear_lock_user"],
            ),
        ):
            await e2e_matter_lock.async_clear_usercode(2)

        assert (
            e2e_matter_lock.coordinator.data.get(pin_address(2))
            is SlotCredential.empty()
        )


class TestGetUsercodes:
    """Verify reading usercodes from the Matter lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        lock_entity: er.RegistryEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """
        Get usercodes returns slot occupancy from Matter.

        The base async_get_usercodes projects async_get_users output onto
        the managed-slot map. Slot 1 is occupied (unreadable), slot 2 is
        empty when the mock reports only a user on slot 1.
        """
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "credentials": [
                            {
                                "type": "pin",
                                "index": 1,
                            }
                        ],
                    },
                ],
            }
        )

        with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
            codes = await e2e_matter_lock.async_get_usercodes()

        assert codes[1] is SlotCredential.unreadable()
        assert codes[2] is SlotCredential.empty()


# =============================================================================
# The #1538 symptom, end to end on a real coordinator
# =============================================================================

_TAGGED_USERS = {
    "max_users": 10,
    "users": [
        {
            "user_index": 1,
            "user_name": "lcm:1:slot1",
            "credentials": [{"type": "pin", "index": 1}],
        },
        {
            "user_index": 2,
            "user_name": "lcm:2:slot2",
            "credentials": [{"type": "pin", "index": 2}],
        },
    ],
}
_AFTER_USER_ONE_DELETED = {"max_users": 10, "users": _TAGGED_USERS["users"][1:]}


def _user_cleared_event(node_id: int, user_index: int) -> MatterNodeEvent:
    """The LockUserChange a lock emits for ClearUser (a user record, cleared)."""
    return MatterNodeEvent(
        node_id=node_id,
        endpoint_id=1,
        cluster_id=257,
        event_id=4,
        event_number=0,
        priority=1,
        timestamp=0,
        timestamp_type=0,
        data={
            "lockDataType": 2,
            "dataOperationType": 1,
            "dataIndex": user_index,
            "userIndex": user_index,
        },
    )


class TestOutOfBandDeletion:
    """A credential deleted through Manage access is noticed and put back."""

    @pytest.fixture
    def matter_mock_helpers(
        self, matter_mock_helpers: dict[str, AsyncMock]
    ) -> dict[str, AsyncMock]:
        """LCM-tagged users, as the provider itself would have written them."""
        matter_mock_helpers["get_lock_users"].return_value = _TAGGED_USERS
        return matter_mock_helpers

    async def test_clear_user_flips_in_sync_off_and_sync_recreates_the_pin(
        self,
        hass: HomeAssistant,
        matter_client: MagicMock,
        matter_node: MatterNode,
        lock_entity: er.RegistryEntry,
        lcm_config_entry: MockConfigEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Issue #1538 on a Matter lock: the deletion is seen at once, not hourly.

        Manage access sends ClearUser, so the lock no longer lists the user
        when the event arrives; the slot it anchored is resolved from the
        provider's last read. The in-sync sensor goes off on the event, the
        next tick writes the PIN back for the same user, and the sensor
        returns to on once the lock lists the user again.
        """
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 1, lock_entity.entity_id)
        await async_initial_tick(hass, in_sync)
        for _ in range(3):
            if hass.states.get(in_sync).state == STATE_ON:
                break
            await async_advance_time(hass, TICK_INTERVAL)
        assert hass.states.get(in_sync).state == STATE_ON

        lock = lcm_config_entry.runtime_data.locks[lock_entity.entity_id]
        # Home Assistant's own Matter lock entity subscribes to the same node
        # events; take the provider's subscription, not the platform's.
        deliver = next(
            call.kwargs["callback"]
            for call in matter_client.subscribe_events.call_args_list
            if call.kwargs.get("event_filter") == EventType.NODE_EVENT
            and call.kwargs.get("node_filter") == matter_node.node_id
            and getattr(call.kwargs["callback"], "__self__", None) is lock
        )

        matter_mock_helpers["get_lock_users"].return_value = _AFTER_USER_ONE_DELETED
        matter_mock_helpers["set_lock_credential"].reset_mock()
        deliver(EventType.NODE_EVENT, _user_cleared_event(matter_node.node_id, 1))
        await hass.async_block_till_done()
        assert hass.states.get(in_sync).state == STATE_OFF

        await async_advance_time(hass, TICK_INTERVAL * 2)
        writes = matter_mock_helpers["set_lock_credential"].await_args_list
        assert any(
            call.kwargs.get("credential_data") == "1234"
            and call.kwargs.get("user_index") == 1
            for call in writes
        ), [call.kwargs for call in writes]

        matter_mock_helpers["get_lock_users"].return_value = _TAGGED_USERS
        await async_advance_time(hass, TICK_INTERVAL * 3)
        assert hass.states.get(in_sync).state == STATE_ON
