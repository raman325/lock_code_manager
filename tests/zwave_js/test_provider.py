"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass
from zwave_js_server.model.node import Node

from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock


@pytest.fixture(name="zwave_js_lock")
async def zwave_js_lock_fixture(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
) -> ZWaveJSLock:
    """Create a ZWaveJSLock instance for testing."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    lock = ZWaveJSLock(
        hass=hass,
        dev_reg=dev_reg,
        ent_reg=ent_reg,
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )
    return lock


# Properties tests


async def test_domain(zwave_js_lock: ZWaveJSLock) -> None:
    """Test domain property returns zwave_js."""
    assert zwave_js_lock.domain == ZWAVE_JS_DOMAIN


async def test_supports_push(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that Z-Wave JS locks support push updates."""
    assert zwave_js_lock.supports_push is True


async def test_connection_check_interval_is_none(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that connection check interval is None (uses config entry state)."""
    assert zwave_js_lock.connection_check_interval is None


async def test_node_property(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test node property returns the correct Z-Wave node."""
    node = zwave_js_lock.node
    assert node.node_id == lock_schlage_be469.node_id


# Connection tests


async def test_is_connection_up_when_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is up when config entry is loaded and client connected."""
    assert zwave_integration.state == ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_connection_up() is True


async def test_is_connection_down_when_not_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is down when config entry not loaded."""
    await hass.config_entries.async_unload(zwave_integration.entry_id)
    await hass.async_block_till_done()

    assert zwave_integration.state != ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_connection_up() is False


# Usercode tests


async def test_get_usercodes_from_cache(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test reading usercodes from the Z-Wave JS value cache."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}, "3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    await zwave_js_lock.async_setup(lcm_entry)

    # Slot 1: "9999" (in_use=True)
    # Slot 2: "1234" (in_use=True)
    # Slot 3: empty (in_use=False)
    codes = await zwave_js_lock.async_get_usercodes()

    assert codes[2] == "1234"
    assert codes[3] == ""

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_calls_service(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode calls the Z-Wave JS service."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True
        mock_service.assert_called_once_with(
            ZWAVE_JS_DOMAIN,
            "set_lock_usercode",
            {
                "entity_id": zwave_js_lock.lock.entity_id,
                "code_slot": 4,
                "usercode": "5678",
            },
        )

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_skips_when_unchanged(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode returns False when code is already set."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        # Slot 2 already has "1234" in the fixture
        result = await zwave_js_lock.async_set_usercode(2, "1234", "Test User")

        assert result is False
        mock_service.assert_not_called()

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_calls_service(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode calls the Z-Wave JS service."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True
        mock_service.assert_called_once_with(
            ZWAVE_JS_DOMAIN,
            "clear_lock_usercode",
            {
                "entity_id": zwave_js_lock.lock.entity_id,
                "code_slot": 2,
            },
        )

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_skips_when_already_cleared(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode returns False when slot is already empty."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        # Slot 3 is already empty in the fixture
        result = await zwave_js_lock.async_clear_usercode(3)

        assert result is False
        mock_service.assert_not_called()

    await zwave_js_lock.async_unload(False)


# Push updates tests


async def test_subscribe_push_updates(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test subscribing to push updates."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Subscribe to push updates (idempotent - may already be subscribed)
    zwave_js_lock.subscribe_push_updates()

    assert zwave_js_lock._value_update_unsub is not None

    zwave_js_lock.unsubscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is None

    await zwave_js_lock.async_unload(False)


async def test_subscribe_is_idempotent(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that calling subscribe multiple times is safe."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    first_unsub = zwave_js_lock._value_update_unsub

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is first_unsub

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# Event filter tests


async def test_event_filter_matches_correct_node(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that event filter matches events for the correct node."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(zwave_js_lock.lock.device_id)
    assert device is not None

    event_data = {
        "home_id": zwave_js_lock.node.client.driver.controller.home_id,
        "node_id": lock_schlage_be469.node_id,
        "device_id": zwave_js_lock.lock.device_id,
    }

    assert zwave_js_lock._zwave_js_event_filter(event_data) is True

    event_data_wrong_node = {
        "home_id": zwave_js_lock.node.client.driver.controller.home_id,
        "node_id": 999,
        "device_id": zwave_js_lock.lock.device_id,
    }
    assert zwave_js_lock._zwave_js_event_filter(event_data_wrong_node) is False

    await zwave_js_lock.async_unload(False)


# Setup/unload tests


async def test_setup_registers_event_listener(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that setup registers an event listener for Z-Wave JS events."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)

    assert len(zwave_js_lock._listeners) == 0

    await zwave_js_lock.async_setup(lcm_entry)

    assert len(zwave_js_lock._listeners) == 1

    await zwave_js_lock.async_unload(False)

    assert len(zwave_js_lock._listeners) == 0


async def test_unload_cleans_up_push_subscription(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that unload cleans up push subscriptions."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is not None

    await zwave_js_lock.async_unload(False)
    assert zwave_js_lock._value_update_unsub is None


# Hard refresh tests


async def test_hard_refresh_calls_refresh_cc_values(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that hard refresh calls the node's refresh method."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        lock_schlage_be469,
        "async_refresh_cc_values",
        new_callable=AsyncMock,
    ) as mock_refresh:
        codes = await zwave_js_lock.async_hard_refresh_codes()

        mock_refresh.assert_called_once_with(CommandClass.USER_CODE)
        assert isinstance(codes, dict)

    await zwave_js_lock.async_unload(False)
