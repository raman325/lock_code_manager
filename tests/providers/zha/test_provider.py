"""Test the ZHA lock provider."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zha import ZHALock

# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


async def test_domain(zha_lock: ZHALock) -> None:
    """Test domain property returns zha."""
    assert zha_lock.domain == ZHA_DOMAIN


async def test_supports_push(zha_lock: ZHALock) -> None:
    """Test that ZHA locks support push updates."""
    assert zha_lock.supports_push is True


async def test_connection_check_interval(zha_lock: ZHALock) -> None:
    """Test connection check interval is 30 seconds."""
    assert zha_lock.connection_check_interval == timedelta(seconds=30)


async def test_hard_refresh_with_programming_events(zha_lock: ZHALock) -> None:
    """Test hard refresh interval is None when programming events supported."""
    zha_lock._supports_programming_events = True
    assert zha_lock.hard_refresh_interval is None


async def test_hard_refresh_without_programming_events(zha_lock: ZHALock) -> None:
    """Test hard refresh interval is 1 hour when programming events not supported."""
    zha_lock._supports_programming_events = False
    assert zha_lock.hard_refresh_interval == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


async def test_is_integration_connected(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test connection is up when device is available."""
    assert await zha_lock.async_is_integration_connected() is True


# ---------------------------------------------------------------------------
# Cluster access tests
# ---------------------------------------------------------------------------


async def test_get_door_lock_cluster(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test getting the DoorLock cluster."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    assert cluster.cluster_id == DoorLock.cluster_id


async def test_get_door_lock_cluster_caches_result(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test that the cluster is cached after first access."""
    cluster1 = zha_lock._get_door_lock_cluster()
    cluster2 = zha_lock._get_door_lock_cluster()
    assert cluster1 is cluster2


# ---------------------------------------------------------------------------
# Usercode tests
# ---------------------------------------------------------------------------


async def test_get_usercodes(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test reading usercodes from the lock."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        if slot_num == 1:
            return type(
                "Response",
                (),
                {"user_status": DoorLock.UserStatus.Enabled, "code": "1234"},
            )()
        return type(
            "Response",
            (),
            {"user_status": DoorLock.UserStatus.Available, "code": ""},
        )()

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    codes = await zha_lock.async_get_usercodes()

    assert codes[1] == "1234"
    assert codes[2] is SlotCode.EMPTY


async def test_set_usercode(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test set_usercode calls the cluster correctly."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 0})())

    result = await zha_lock.async_set_usercode(3, "5678", "Test User")

    assert result is True
    cluster.set_pin_code.assert_called_once_with(
        3,
        DoorLock.UserStatus.Enabled,
        DoorLock.UserType.Unrestricted,
        "5678",
    )


async def test_set_usercode_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test set_usercode raises LockDisconnected on failure."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 1})())

    with pytest.raises(LockDisconnected, match="set_pin_code failed"):
        await zha_lock.async_set_usercode(3, "5678")


async def test_clear_usercode(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test clear_usercode calls the cluster correctly."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 0})()
    )

    result = await zha_lock.async_clear_usercode(3)

    assert result is True
    cluster.clear_pin_code.assert_called_once_with(3)


async def test_clear_usercode_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test clear_usercode raises LockDisconnected on failure."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 1})()
    )

    with pytest.raises(LockDisconnected, match="clear_pin_code failed"):
        await zha_lock.async_clear_usercode(3)


# ---------------------------------------------------------------------------
# Push update tests
# ---------------------------------------------------------------------------


async def test_subscribe_push_updates(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test subscribing to push updates."""
    zha_lock.setup_push_subscription()
    assert zha_lock._cluster_listener_unsub is not None

    zha_lock.teardown_push_subscription()
    assert zha_lock._cluster_listener_unsub is None


async def test_subscribe_is_idempotent(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test that calling subscribe multiple times is safe."""
    zha_lock.setup_push_subscription()
    first_unsub = zha_lock._cluster_listener_unsub

    zha_lock.setup_push_subscription()
    assert zha_lock._cluster_listener_unsub is first_unsub

    zha_lock.teardown_push_subscription()


# ---------------------------------------------------------------------------
# Programming event support detection
# ---------------------------------------------------------------------------


async def test_programming_event_support_with_mask(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test detecting programming event support via mask attributes."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    def mock_get(attr_name):
        if attr_name == "keypad_programming_event_mask":
            return 0x0001
        return None

    cluster.get = mock_get

    result = await zha_lock._async_check_programming_event_support()
    assert result is True


async def test_programming_event_support_without_mask(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test programming events not supported when no mask attributes."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    cluster.get = lambda attr_name: None

    result = await zha_lock._async_check_programming_event_support()
    assert result is False
