"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass, NodeStatus
from zwave_js_server.exceptions import FailedZWaveCommand
from zwave_js_server.model.node import Node

from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
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

    return ZWaveJSLock(
        hass=hass,
        dev_reg=dev_reg,
        ent_reg=ent_reg,
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )


@pytest.fixture(autouse=True)
def mock_get_usercode_from_node():
    """
    Mock get_usercode_from_node for all tests.

    V1 set/clear calls get_usercode_from_node to poll the slot from the device.
    In tests, the node doesn't have a real Z-Wave JS server connection, so we
    mock the function. Individual tests can access the mock via parameter name.
    """
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode_from_node",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


@pytest.fixture(name="zwave_js_lock_v2")
async def zwave_js_lock_v2_fixture(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469_v2: Node,
) -> ZWaveJSLock:
    """Create a ZWaveJSLock with User Code CC V2 for testing."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    return ZWaveJSLock(
        hass=hass,
        dev_reg=dev_reg,
        ent_reg=ent_reg,
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )


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


async def test_setup_is_idempotent(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_code_manager_config_entry: MockConfigEntry,
) -> None:
    """Test that async_setup clears old listeners before re-registering."""
    await zwave_js_lock.async_setup(lock_code_manager_config_entry)
    assert len(zwave_js_lock._listeners) >= 1
    count_after_first = len(zwave_js_lock._listeners)

    # Call again — should not accumulate listeners
    await zwave_js_lock.async_setup(lock_code_manager_config_entry)
    assert len(zwave_js_lock._listeners) == count_after_first


# CC version detection tests


async def test_usercode_cc_version_v1(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that V1 lock reports correct CC version."""
    assert zwave_js_lock._usercode_cc_version == 1


async def test_usercode_cc_version_v2(zwave_js_lock_v2: ZWaveJSLock) -> None:
    """Test that V2 lock reports correct CC version."""
    assert zwave_js_lock_v2._usercode_cc_version == 2


async def test_usercode_cc_version_missing(
    zwave_js_lock: ZWaveJSLock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test warning when User Code CC is not found on node."""
    # Remove User Code CC from the node's command classes
    node = zwave_js_lock.node
    endpoint = node.endpoints[0]
    original_ccs = endpoint.data["commandClasses"]
    endpoint.data["commandClasses"] = [
        cc for cc in original_ccs if cc["id"] != CommandClass.USER_CODE.value
    ]
    # Clear cached property so it re-evaluates
    zwave_js_lock.__dict__.pop("_usercode_cc_version", None)

    assert zwave_js_lock._usercode_cc_version == 1
    assert "User Code CC not found" in caplog.text


async def test_node_property(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test node property returns the correct Z-Wave node."""
    node = zwave_js_lock.node
    assert node.node_id == lock_schlage_be469.node_id


# Connection tests


async def test_is_integration_connected_when_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is up when config entry is loaded and client connected."""
    assert zwave_integration.state == ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_integration_connected() is True


async def test_is_integration_not_connected_when_not_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is down when config entry not loaded."""
    await hass.config_entries.async_unload(zwave_integration.entry_id)
    await hass.async_block_till_done()

    assert zwave_integration.state != ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_integration_connected() is False


# Usercode tests


async def test_set_usercode_skips_when_unchanged(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode returns False when code is already set."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Slot 2 already has "1234" in the fixture
    result = await zwave_js_lock.async_set_usercode(2, "1234", "Test User")

    assert result is False

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_skips_when_code_matches(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode returns False when cached code matches the target PIN."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Mock the cache to return the same code we're trying to set
    matching_slot = {"code_slot": 2, "usercode": "5678", "in_use": True}

    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        return_value=matching_slot,
    ):
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should skip the set operation since code already matches
        assert result is False

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_proceeds_when_masked(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode proceeds when code is masked (all asterisks).

    Some locks (like Yale) return masked PINs (****) instead of actual codes.
    Since we cannot compare masked codes, the set operation should always proceed.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Mock the cache to return a masked code
    masked_slot = {"code_slot": 2, "usercode": "****", "in_use": True}

    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        return_value=masked_slot,
    ):
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should proceed since masked codes cannot be compared
        assert result is True

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_proceeds_on_cache_failure(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode proceeds when the cache lookup raises.

    A stale or missing cache entry must not block the set operation —
    the bare-except in the cache short-circuit guards against this.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        side_effect=ValueError("cache miss"),
    ):
        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_skips_when_already_cleared(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode returns False when slot is already empty."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Slot 3 is already empty in the fixture
    result = await zwave_js_lock.async_clear_usercode(3)

    assert result is False

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_proceeds_on_cache_failure(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that clear_usercode proceeds when the cache lookup raises.

    Mirrors test_set_usercode_proceeds_on_cache_failure for the clear path.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        side_effect=ValueError("cache miss"),
    ):
        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_optimistic_update(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode performs optimistic coordinator update.

    When a set operation succeeds, the coordinator should be updated immediately
    with the new value. This prevents sync loops where the binary sensor reads
    stale cached data and triggers repeated sync attempts.

    The Z-Wave command is acknowledged by the lock (via Supervision CC), but
    the JS value cache updates asynchronously via push notifications. Without
    the optimistic update, there's a race condition where coordinator refresh
    reads stale cache data.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator with stale data (simulating the race condition)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: ""}  # Slot appears empty in stale cache
    zwave_js_lock.coordinator = mock_coordinator

    result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

    assert result is True
    # Verify optimistic update was called with new PIN
    mock_coordinator.push_update.assert_called_once_with({4: "5678"})

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_optimistic_update_prevents_stale_read(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that optimistic update prevents sync loops from stale cache reads."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Simulate stale cache: coordinator thinks slot is empty
    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: ""}
    zwave_js_lock.coordinator = mock_coordinator

    await zwave_js_lock.async_set_usercode(4, "9999")

    # The optimistic update should have been called
    mock_coordinator.push_update.assert_called_once_with({4: "9999"})

    # Simulate what push_update does - update coordinator data
    mock_coordinator.data[4] = "9999"
    assert mock_coordinator.data[4] == "9999"

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_optimistic_update(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that clear_usercode performs optimistic coordinator update.

    When a clear operation succeeds, the coordinator should be updated immediately
    with SlotCode.EMPTY. This prevents sync loops where the binary sensor reads
    stale cached data showing the old PIN and triggers repeated clear attempts.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator with stale data (still shows old PIN)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Stale: slot still shows PIN
    zwave_js_lock.coordinator = mock_coordinator

    result = await zwave_js_lock.async_clear_usercode(2)

    assert result is True
    # Verify optimistic update was called with SlotCode.EMPTY
    mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})

    await zwave_js_lock.async_unload(False)


# V1 cache poll tests


async def test_v1_set_usercode_polls_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_get_usercode_from_node,
) -> None:
    """
    Test that V1 set_usercode polls the slot from the device after set.

    V1 locks don't reliably update the Z-Wave JS value cache after a set
    operation. Polling the slot forces the cache to update before the
    coordinator reads it, preventing sync loops.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: ""}
    zwave_js_lock.coordinator = mock_coordinator

    await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

    mock_get_usercode_from_node.assert_called_once_with(lock_schlage_be469, 4)

    await zwave_js_lock.async_unload(False)


async def test_v1_clear_usercode_polls_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_get_usercode_from_node,
) -> None:
    """Test that V1 clear_usercode polls the slot from the device after clear."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator

    await zwave_js_lock.async_clear_usercode(2)

    mock_get_usercode_from_node.assert_called_once_with(lock_schlage_be469, 2)

    await zwave_js_lock.async_unload(False)


async def test_v2_set_usercode_does_not_poll_slot(
    hass: HomeAssistant,
    zwave_js_lock_v2: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_get_usercode_from_node,
) -> None:
    """Test that V2 set_usercode does NOT poll the slot (cache updates reliably)."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock_v2.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: ""}
    zwave_js_lock_v2.coordinator = mock_coordinator

    await zwave_js_lock_v2.async_set_usercode(4, "5678", "Test User")

    mock_get_usercode_from_node.assert_not_called()

    await zwave_js_lock_v2.async_unload(False)


async def test_v1_set_usercode_poll_failure_raises_lock_disconnected(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_get_usercode_from_node,
) -> None:
    """
    Test that a V1 set poll failure raises LockDisconnected.

    When get_usercode_from_node raises after a V1 set operation, the error
    should be wrapped as LockDisconnected so it routes into the retry path
    instead of the generic exception handler that would suspend the lock.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: ""}
    zwave_js_lock.coordinator = mock_coordinator

    mock_get_usercode_from_node.side_effect = FailedZWaveCommand(
        "msg_id", 202, "Node presumed dead"
    )

    with pytest.raises(LockDisconnected, match="Post-set verification poll failed"):
        await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

    await zwave_js_lock.async_unload(False)


async def test_v1_clear_usercode_poll_failure_raises_lock_disconnected(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_get_usercode_from_node,
) -> None:
    """
    Test that a V1 clear poll failure raises LockDisconnected.

    When get_usercode_from_node raises after a V1 clear operation, the error
    should be wrapped as LockDisconnected so it routes into the retry path
    instead of the generic exception handler that would suspend the lock.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator

    mock_get_usercode_from_node.side_effect = FailedZWaveCommand(
        "msg_id", 202, "Node presumed dead"
    )

    with pytest.raises(LockDisconnected, match="Post-clear verification poll failed"):
        await zwave_js_lock.async_clear_usercode(2)

    await zwave_js_lock.async_unload(False)


async def test_v1_set_usercode_poll_non_zwave_error_propagates(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_get_usercode_from_node,
) -> None:
    """
    Non-Z-Wave errors from V1 post-set poll propagate uncaught.

    Only FailedZWaveCommand is wrapped as LockDisconnected. Other
    exceptions (programming errors) should propagate so the sync
    manager's generic handler can suspend the lock and surface the bug.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: ""}
    zwave_js_lock.coordinator = mock_coordinator

    mock_get_usercode_from_node.side_effect = RuntimeError("unexpected bug")

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

    await zwave_js_lock.async_unload(False)


async def test_v1_clear_usercode_poll_non_zwave_error_propagates(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_get_usercode_from_node,
) -> None:
    """Non-Z-Wave errors from V1 post-clear poll propagate uncaught."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator

    mock_get_usercode_from_node.side_effect = RuntimeError("unexpected bug")

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await zwave_js_lock.async_clear_usercode(2)

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_no_coordinator(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode handles missing coordinator gracefully.

    The coordinator check is defensive - in normal operation it always exists
    after setup. This test verifies the guard clause works.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Remove coordinator to test defensive check
    zwave_js_lock.coordinator = None

    # Should not raise even without coordinator
    result = await zwave_js_lock.async_set_usercode(4, "5678")
    assert result is True

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_no_coordinator(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode handles missing coordinator gracefully."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Remove coordinator to test defensive check
    zwave_js_lock.coordinator = None

    # Should not raise even without coordinator
    result = await zwave_js_lock.async_clear_usercode(2)
    assert result is True

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

    await zwave_js_lock.async_setup_internal(lcm_entry)

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
    await zwave_js_lock.async_setup_internal(lcm_entry)

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
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with patch.object(
        lock_schlage_be469,
        "async_refresh_cc_values",
        new_callable=AsyncMock,
    ) as mock_refresh:
        codes = await zwave_js_lock.async_hard_refresh_codes()

        mock_refresh.assert_called_once_with(CommandClass.USER_CODE)
        assert isinstance(codes, dict)

    await zwave_js_lock.async_unload(False)


# Masked usercode tests


async def test_get_usercodes_masked_pin_unmanaged_slot_returns_masked_value(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test mixed slots: managed with real code vs unmanaged with masked code.

    This test verifies behavior when the lock cache contains:
    - Slot 1: Managed by LCM, has real code "9999" -> should be returned
    - Slot 5: NOT managed by LCM, has masked code "****" -> returns UNREADABLE_CODE

    Unmanaged slots with masked PINs return SlotCode.UNREADABLE_CODE so sync
    logic knows a PIN exists on the lock, even if we can't read the actual value.
    """
    # Configure LCM to only manage slot 1 (not slot 5)
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},  # Only slot 1 is managed
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Mock the cache to include a slot with a masked PIN that isn't managed by LCM
    masked_slots = [
        {"code_slot": 1, "usercode": "9999", "in_use": True},  # Managed by LCM
        {"code_slot": 5, "usercode": "****", "in_use": True},  # NOT managed, masked
    ]

    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
        ),
        # Slot 5 not in Z-Wave node data, so code_slot_in_use returns None
        patch.object(
            zwave_js_lock,
            "code_slot_in_use",
            side_effect=lambda s: True if s == 1 else None,
        ),
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot 1 should have its code
        assert codes[1] == "9999"
        # Slot 5 returns UNREADABLE_CODE (masked code, so sync logic knows a PIN exists)
        assert codes[5] is SlotCode.UNREADABLE_CODE

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_masked_pin_returns_unknown(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that masked PINs return SlotCode.UNREADABLE_CODE.

    When the lock returns masked codes (all asterisks), the provider returns
    SlotCode.UNREADABLE_CODE so consumers know a code exists but the value is hidden.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Mock the cache to have a masked PIN on a managed slot
    masked_slots = [
        {"code_slot": 2, "usercode": "****", "in_use": True},
    ]

    with patch.object(
        zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Masked PIN should be returned as SlotCode.UNREADABLE_CODE
        assert codes[2] is SlotCode.UNREADABLE_CODE

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_empty_usercode_in_use_skipped(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that in_use=True with empty usercode is skipped (partially populated cache)."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}, "3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Slot 2: in_use=True but empty usercode (cache not populated yet)
    # Slot 3: normal code
    slots = [
        {"code_slot": 2, "usercode": "", "in_use": True},
        {"code_slot": 3, "usercode": "5678", "in_use": True},
    ]

    with patch.object(zwave_js_lock, "_get_usercodes_from_cache", return_value=slots):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot 2 should be skipped (not in result)
        assert 2 not in codes
        # Slot 3 should have its code
        assert codes[3] == "5678"

    await zwave_js_lock.async_unload(False)


# code_slot_in_use tests


@pytest.mark.parametrize(
    ("mock_config", "expected"),
    [
        ({"return_value": {"in_use": True, "usercode": "1234"}}, True),
        ({"return_value": {"in_use": False, "usercode": ""}}, False),
        ({"side_effect": KeyError("slot not found")}, None),
        ({"side_effect": ValueError("invalid slot")}, None),
    ],
)
async def test_code_slot_in_use(
    zwave_js_lock: ZWaveJSLock,
    mock_config: dict,
    expected: bool | None,
) -> None:
    """Test code_slot_in_use for various return values and exceptions."""
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        **mock_config,
    ):
        assert zwave_js_lock.code_slot_in_use(1) is expected


# _slot_expects_pin tests


@pytest.mark.parametrize(
    ("slot_config", "expected"),
    [
        ({CONF_PIN: "1234", CONF_ENABLED: True}, True),
        ({CONF_PIN: "1234", CONF_ENABLED: False}, False),
        ({CONF_PIN: "", CONF_ENABLED: True}, False),
        ({CONF_ENABLED: True}, False),
    ],
)
async def test_slot_expects_pin_by_config(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    slot_config: dict,
    expected: bool,
) -> None:
    """Test _slot_expects_pin based on coordinator.slot_expects_pin from config entry."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": slot_config},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    assert zwave_js_lock._slot_expects_pin(2) is expected

    await zwave_js_lock.async_unload(False)


async def test_slot_expects_pin_returns_false_for_unmanaged_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _slot_expects_pin returns False for unmanaged slot."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},  # Only slot 1 managed
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Slot 99 is not managed
    assert zwave_js_lock._slot_expects_pin(99) is False

    await zwave_js_lock.async_unload(False)


# Device availability tests


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (NodeStatus.ALIVE, True),
        (NodeStatus.ASLEEP, True),
        (NodeStatus.UNKNOWN, True),
        (NodeStatus.DEAD, False),
    ],
)
async def test_is_device_available_by_status(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    status: NodeStatus,
    expected: bool,
) -> None:
    """Test async_is_device_available for each node status."""
    with patch.object(
        type(lock_schlage_be469),
        "status",
        new_callable=lambda: property(lambda self: status),
    ):
        assert await zwave_js_lock.async_is_device_available() is expected


async def test_is_device_available_returns_false_on_exception(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that async_is_device_available returns False when node access raises."""

    def raise_error(self):
        raise RuntimeError("node gone")

    with patch.object(
        type(zwave_js_lock),
        "node",
        new_callable=lambda: property(raise_error),
    ):
        assert await zwave_js_lock.async_is_device_available() is False
