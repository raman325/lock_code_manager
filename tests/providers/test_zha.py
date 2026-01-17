"""Test the ZHA lock provider."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import zigpy.device
from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.zha import ZHALock


@pytest.fixture(name="zha_lock")
async def zha_lock_fixture(
    hass: HomeAssistant,
    zha_lock_entity: er.RegistryEntry,
    zha_config_entry: MockConfigEntry,
    zigpy_lock_device: zigpy.device.Device,
) -> ZHALock:
    """Create a ZHALock instance for testing."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    lock = ZHALock(
        hass=hass,
        dev_reg=dev_reg,
        ent_reg=ent_reg,
        lock_config_entry=zha_config_entry,
        lock=zha_lock_entity,
    )
    return lock


# =============================================================================
# Property tests
# =============================================================================


async def test_domain(zha_lock: ZHALock) -> None:
    """Test domain property returns zha."""
    assert zha_lock.domain == ZHA_DOMAIN


async def test_supports_push(zha_lock: ZHALock) -> None:
    """Test that ZHA locks support push updates."""
    assert zha_lock.supports_push is True


async def test_connection_check_interval(zha_lock: ZHALock) -> None:
    """Test that connection check interval is 30 seconds."""
    assert zha_lock.connection_check_interval == timedelta(seconds=30)


async def test_hard_refresh_interval_when_programming_events_supported(
    zha_lock: ZHALock,
) -> None:
    """Test hard refresh interval is None when programming events are supported."""
    zha_lock._supports_programming_events = True
    assert zha_lock.hard_refresh_interval is None


async def test_hard_refresh_interval_when_programming_events_not_supported(
    zha_lock: ZHALock,
) -> None:
    """Test hard refresh interval is 1 hour when programming events not supported."""
    zha_lock._supports_programming_events = False
    assert zha_lock.hard_refresh_interval == timedelta(hours=1)


# =============================================================================
# Connection tests
# =============================================================================


async def test_is_connection_up_when_available(
    hass: HomeAssistant,
    zha_lock: ZHALock,
) -> None:
    """Test connection is up when device is available."""
    # The mock device should be available by default
    assert await zha_lock.async_is_connection_up() is True


# =============================================================================
# Cluster access tests
# =============================================================================


async def test_get_door_lock_cluster(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zigpy_lock_device: zigpy.device.Device,
) -> None:
    """Test getting the Door Lock cluster."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    assert cluster.cluster_id == DoorLock.cluster_id


async def test_get_door_lock_cluster_caches_result(
    hass: HomeAssistant,
    zha_lock: ZHALock,
) -> None:
    """Test that cluster is cached after first access."""
    cluster1 = zha_lock._get_door_lock_cluster()
    cluster2 = zha_lock._get_door_lock_cluster()
    assert cluster1 is cluster2


# =============================================================================
# Usercode tests
# =============================================================================


async def test_get_usercodes(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
    zigpy_lock_device: zigpy.device.Device,
) -> None:
    """Test reading usercodes from the lock."""
    # Create LCM config entry with slots
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zha_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    # Mock the cluster's get_pin_code method
    cluster = zha_lock._get_door_lock_cluster()

    # Mock get_pin_code responses
    async def mock_get_pin_code(slot_num):
        if slot_num == 1:
            # Return enabled slot with code
            return type(
                "Response",
                (),
                {"user_status": DoorLock.UserStatus.Enabled, "code": "1234"},
            )()
        # Return disabled slot
        return type(
            "Response",
            (),
            {"user_status": DoorLock.UserStatus.Available, "code": ""},
        )()

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    await zha_lock.async_setup(lcm_entry)

    codes = await zha_lock.async_get_usercodes()

    assert codes[1] == "1234"
    assert codes[2] == ""

    await zha_lock.async_unload(False)


async def test_set_usercode_calls_cluster(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
) -> None:
    """Test that set_usercode calls the cluster's set_pin_code."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [], CONF_SLOTS: {}},
    )
    lcm_entry.add_to_hass(hass)
    await zha_lock.async_setup(lcm_entry)

    cluster = zha_lock._get_door_lock_cluster()
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 0})())

    result = await zha_lock.async_set_usercode(3, "5678", "Test User")

    assert result is True
    cluster.set_pin_code.assert_called_once_with(
        3,
        DoorLock.UserStatus.Enabled,
        DoorLock.UserType.Unrestricted,
        "5678",
    )

    await zha_lock.async_unload(False)


async def test_clear_usercode_calls_cluster(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
) -> None:
    """Test that clear_usercode calls the cluster's clear_pin_code."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [], CONF_SLOTS: {}},
    )
    lcm_entry.add_to_hass(hass)
    await zha_lock.async_setup(lcm_entry)

    cluster = zha_lock._get_door_lock_cluster()
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 0})()
    )

    result = await zha_lock.async_clear_usercode(3)

    assert result is True
    cluster.clear_pin_code.assert_called_once_with(3)

    await zha_lock.async_unload(False)


# =============================================================================
# Push update tests
# =============================================================================


async def test_subscribe_push_updates(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
) -> None:
    """Test subscribing to push updates."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [], CONF_SLOTS: {}},
    )
    lcm_entry.add_to_hass(hass)
    await zha_lock.async_setup(lcm_entry)

    # Subscribe to push updates
    zha_lock.subscribe_push_updates()

    assert zha_lock._cluster_listener_unsub is not None

    # Unsubscribe
    zha_lock.unsubscribe_push_updates()
    assert zha_lock._cluster_listener_unsub is None

    await zha_lock.async_unload(False)


async def test_subscribe_is_idempotent(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
) -> None:
    """Test that calling subscribe multiple times is safe."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [], CONF_SLOTS: {}},
    )
    lcm_entry.add_to_hass(hass)
    await zha_lock.async_setup(lcm_entry)

    zha_lock.subscribe_push_updates()
    first_unsub = zha_lock._cluster_listener_unsub

    zha_lock.subscribe_push_updates()
    assert zha_lock._cluster_listener_unsub is first_unsub

    zha_lock.unsubscribe_push_updates()
    await zha_lock.async_unload(False)


# =============================================================================
# Programming event support detection
# =============================================================================


async def test_check_programming_event_support_with_mask(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
) -> None:
    """Test detecting programming event support via mask attributes."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [], CONF_SLOTS: {}},
    )
    lcm_entry.add_to_hass(hass)
    await zha_lock.async_setup(lcm_entry)

    cluster = zha_lock._get_door_lock_cluster()

    # Mock get() to return a non-zero mask value
    def mock_get(attr_name):
        if attr_name == "keypad_programming_event_mask":
            return 0x0001
        return None

    cluster.get = mock_get

    supports = await zha_lock._async_check_programming_event_support()
    assert supports is True

    await zha_lock.async_unload(False)


async def test_check_programming_event_support_without_mask(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    zha_config_entry: MockConfigEntry,
) -> None:
    """Test detecting programming event not supported when no mask attributes."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [], CONF_SLOTS: {}},
    )
    lcm_entry.add_to_hass(hass)
    await zha_lock.async_setup(lcm_entry)

    cluster = zha_lock._get_door_lock_cluster()

    # Mock get() to return None/0 for all mask attributes
    cluster.get = lambda attr_name: None

    supports = await zha_lock._async_check_programming_event_support()
    assert supports is False

    await zha_lock.async_unload(False)
