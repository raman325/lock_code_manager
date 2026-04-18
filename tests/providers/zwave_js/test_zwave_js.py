"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass, NodeStatus
from zwave_js_server.const.command_class.lock import (
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.node import Node

from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_FROM,
    ATTR_TO,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.exceptions import DuplicateCodeError
from custom_components.lock_code_manager.models import (
    LockCodeManagerConfigEntryRuntimeData,
    SlotCode,
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

    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Slot 1: "9999" (in_use=True)
    # Slot 2: "1234" (in_use=True)
    # Slot 3: empty (in_use=False)
    codes = await zwave_js_lock.async_get_usercodes()

    assert codes[1] == "9999"
    assert codes[2] == "1234"
    assert codes[3] is SlotCode.EMPTY

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_calls_service(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode calls the Z-Wave JS service."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

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
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        # Slot 2 already has "1234" in the fixture
        result = await zwave_js_lock.async_set_usercode(2, "1234", "Test User")

        assert result is False
        mock_service.assert_not_called()

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

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            return_value=matching_slot,
        ),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should skip the set operation since code already matches
        assert result is False
        mock_service.assert_not_called()

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

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            return_value=masked_slot,
        ),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should proceed since masked codes cannot be compared
        assert result is True
        mock_service.assert_called_once()

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_proceeds_on_cache_failure(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode proceeds when the cache lookup raises.

    A stale or missing cache entry must not block the set operation —
    the bare-except in the cache short-circuit guards against this.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            side_effect=ValueError("cache miss"),
        ),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True
        mock_service.assert_called_once()

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_calls_service(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode calls the Z-Wave JS service."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

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
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        # Slot 3 is already empty in the fixture
        result = await zwave_js_lock.async_clear_usercode(3)

        assert result is False
        mock_service.assert_not_called()

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_proceeds_on_cache_failure(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode proceeds when the cache lookup raises.

    Mirrors test_set_usercode_proceeds_on_cache_failure for the clear path.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            side_effect=ValueError("cache miss"),
        ),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True
        mock_service.assert_called_once()

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

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
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

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
        await zwave_js_lock.async_set_usercode(4, "9999")

        # The optimistic update should have been called
        mock_coordinator.push_update.assert_called_once_with({4: "9999"})

        # Simulate what push_update does - update coordinator data
        # (In real code, push_update calls async_set_updated_data which does this)
        mock_coordinator.data[4] = "9999"

        # Now coordinator.data reflects the expected value
        # Binary sensor would see coordinator.data[4] == "9999" == pin_state
        # → expected_in_sync = True → no retry loop
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
    with an empty string. This prevents sync loops where the binary sensor reads
    stale cached data showing the old PIN and triggers repeated clear attempts.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator with stale data (still shows old PIN)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Stale: slot still shows PIN
    zwave_js_lock.coordinator = mock_coordinator

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
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

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
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

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
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

    with patch.object(zwave_js_lock_v2, "async_call_service", new_callable=AsyncMock):
        await zwave_js_lock_v2.async_set_usercode(4, "5678", "Test User")

    mock_get_usercode_from_node.assert_not_called()

    await zwave_js_lock_v2.async_unload(False)


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

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
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

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
        # Should not raise even without coordinator
        result = await zwave_js_lock.async_clear_usercode(2)
        assert result is True

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
    await zwave_js_lock.async_setup_internal(lcm_entry)

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
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    first_unsub = zwave_js_lock._value_update_unsub

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is first_unsub

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_subscribe_push_retry_on_client_not_ready(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that push subscription schedules retry when client isn't ready."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.unsubscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is None

    # Make client appear not ready
    with patch.object(
        zwave_js_lock, "_get_client_state", return_value=(False, "not connected")
    ):
        zwave_js_lock.subscribe_push_updates()

    # Should not have subscribed but should have scheduled retry
    assert zwave_js_lock._value_update_unsub is None
    assert zwave_js_lock._push_retry is not None
    assert zwave_js_lock._push_retry.pending

    # Clean up
    zwave_js_lock._push_retry.cancel()
    await zwave_js_lock.async_unload(False)


async def test_subscribe_push_retry_on_node_error(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push subscription schedules retry when node.on raises ValueError."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.unsubscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is None

    # Make node.on raise ValueError
    with patch.object(lock_schlage_be469, "on", side_effect=ValueError("not ready")):
        zwave_js_lock.subscribe_push_updates()

    assert zwave_js_lock._value_update_unsub is None
    assert zwave_js_lock._push_retry is not None
    assert zwave_js_lock._push_retry.pending

    # Clean up
    zwave_js_lock._push_retry.cancel()
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
    await zwave_js_lock.async_setup_internal(lcm_entry)

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


# Notification event tests


def async_capture_events(
    hass: HomeAssistant, event_name: str
) -> list[Event[dict[str, Any]]]:
    """Create a helper that captures events."""
    events: list[Event[dict[str, Any]]] = []

    @callback
    def capture_events(event: Event[dict[str, Any]]) -> None:
        events.append(event)

    hass.bus.async_listen(event_name, capture_events)
    return events


async def test_notification_event_keypad_lock_fires_lock_state_changed(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a keypad lock notification event fires EVENT_LOCK_STATE_CHANGED."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Capture LCM lock state changed events
    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Create a notification event matching the pattern from HA core tests
    # Type 6 = ACCESS_CONTROL, Event 5 = KEYPAD_LOCK_OPERATION
    event = ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": lock_schlage_be469.node_id,
            "endpointIndex": 0,
            "ccId": 113,  # Notification CC
            "args": {
                "type": 6,  # ACCESS_CONTROL
                "event": 5,  # KEYPAD_LOCK_OPERATION
                "label": "Access Control",
                "eventLabel": "Keypad lock operation",
                "parameters": {"userId": 1},
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Verify the LCM event was fired
    assert len(events) == 1
    assert events[0].data[ATTR_CODE_SLOT] == 1
    assert events[0].data[ATTR_ACTION_TEXT] == "Keypad lock operation"
    assert events[0].data[ATTR_TO] == "locked"
    assert events[0].data[ATTR_FROM] == "unlocked"

    await zwave_js_lock.async_unload(False)


async def test_notification_event_keypad_unlock_fires_lock_state_changed(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a keypad unlock notification event fires EVENT_LOCK_STATE_CHANGED."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Type 6 = ACCESS_CONTROL, Event 6 = KEYPAD_UNLOCK_OPERATION
    event = ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": lock_schlage_be469.node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 6,
                "event": 6,  # KEYPAD_UNLOCK_OPERATION
                "label": "Access Control",
                "eventLabel": "Keypad unlock operation",
                "parameters": {"userId": 2},
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data[ATTR_CODE_SLOT] == 2
    assert events[0].data[ATTR_ACTION_TEXT] == "Keypad unlock operation"
    assert events[0].data[ATTR_TO] == "unlocked"
    assert events[0].data[ATTR_FROM] == "locked"

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
    - Slot 5: NOT managed by LCM, has masked code "****" -> returns masked value

    Unmanaged slots with masked PINs return the masked value so sync logic
    knows a PIN exists on the lock, even if we can't resolve the actual value.
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


async def test_push_update_masked_code_sends_unknown(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that push updates with masked codes send SlotCode.UNREADABLE_CODE.

    When a push update arrives with a masked code (all asterisks) and the slot
    is in use, the coordinator should receive SlotCode.UNREADABLE_CODE.
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

    # Set up a mock coordinator (push_update is synchronous)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock code_slot_in_use to return True (slot has a code)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # Simulate a value update event with masked code
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "****",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        # Coordinator should receive SlotCode.UNREADABLE_CODE for masked codes
        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCode.UNREADABLE_CODE}
        )


async def test_push_update_falsy_value_sends_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates with falsy values send SlotCode.EMPTY."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    # Simulate a value update with empty string (falsy)
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 2,
                "newValue": "",
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})


async def test_push_update_all_zeros_not_in_use_sends_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that all-zeros with slot_in_use=False sends SlotCode.EMPTY."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=False):
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "0000",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})


async def test_push_update_duplicate_value_skipped(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that duplicate push updates are silently skipped."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Already has this value
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "1234",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        # push_update should NOT be called (duplicate)
        mock_coordinator.push_update.assert_not_called()


async def test_push_update_masked_code_with_unknown_in_use_sends_unknown(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that masked codes with slot_in_use=None still send UNREADABLE_CODE."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    # slot_in_use returns None (indeterminate) — should still treat masked as UNREADABLE_CODE
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=None):
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "****",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCode.UNREADABLE_CODE}
        )


async def test_push_update_user_id_status_available_clears_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE push update clears the slot.

    When the lock sends a userIdStatus update with AVAILABLE status,
    it means the slot has been cleared. This should update the coordinator
    to mark the slot as empty.
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

    # Set up a mock coordinator with existing data
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
    mock_coordinator.slot_expects_pin.return_value = False  # No expected PIN
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Simulate userIdStatus=AVAILABLE event (slot cleared)
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_STATUS_PROPERTY,
                "propertyKey": 2,
                "newValue": CodeSlotStatus.AVAILABLE,
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # Coordinator should be updated with SlotCode.EMPTY
    mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_available_skipped_when_already_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE is skipped when slot already empty.

    If the coordinator already shows the slot as empty, we shouldn't
    push another update.
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

    # Set up a mock coordinator - slot already empty
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCode.EMPTY}  # Slot already empty
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Simulate userIdStatus=AVAILABLE event
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_STATUS_PROPERTY,
                "propertyKey": 2,
                "newValue": CodeSlotStatus.AVAILABLE,
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # Coordinator should NOT be updated (already empty)
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_enabled_ignored(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=ENABLED push updates are ignored.

    We only care about AVAILABLE status for clearing slots.
    ENABLED status doesn't tell us the PIN value.
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

    # Set up a mock coordinator
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: ""}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Simulate userIdStatus=ENABLED event
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_STATUS_PROPERTY,
                "propertyKey": 2,
                "newValue": CodeSlotStatus.ENABLED,
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # Coordinator should NOT be updated
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
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


async def test_push_update_user_id_status_available_ignored_when_slot_expects_pin(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE is ignored when slot expects a PIN.

    This prevents sync loops where the lock sends stale AVAILABLE status
    after a code was successfully set.
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

    # Set up a mock coordinator with existing PIN
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _slot_expects_pin to return True (LCM expects a PIN on this slot)
    with patch.object(zwave_js_lock, "_slot_expects_pin", return_value=True):
        # Simulate userIdStatus=AVAILABLE event (stale status from lock)
        event = ZwaveEvent(
            type="value updated",
            data={
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": LOCK_USERCODE_STATUS_PROPERTY,
                    "propertyKey": 2,
                    "newValue": CodeSlotStatus.AVAILABLE,
                },
            },
        )
        lock_schlage_be469.emit("value updated", event.data)
        await hass.async_block_till_done()

        # Coordinator should NOT be updated (AVAILABLE ignored)
        mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_available_clears_when_slot_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE clears slot when LCM doesn't expect a PIN.

    When the slot is inactive (active=OFF), AVAILABLE status should clear
    the coordinator as expected.
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

    # Set up a mock coordinator with existing PIN
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _slot_expects_pin to return False (slot is inactive)
    with patch.object(zwave_js_lock, "_slot_expects_pin", return_value=False):
        # Simulate userIdStatus=AVAILABLE event
        event = ZwaveEvent(
            type="value updated",
            data={
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": LOCK_USERCODE_STATUS_PROPERTY,
                    "propertyKey": 2,
                    "newValue": CodeSlotStatus.AVAILABLE,
                },
            },
        )
        lock_schlage_be469.emit("value updated", event.data)
        await hass.async_block_till_done()

        # Coordinator SHOULD be updated (slot cleared)
        mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})

    zwave_js_lock.unsubscribe_push_updates()
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


# Duplicate code notification tests


def _make_duplicate_code_event(node_id: int, user_id: int | None = None) -> ZwaveEvent:
    """Create a duplicate code notification ZwaveEvent."""
    params: dict[str, Any] = {}
    if user_id is not None:
        params["userId"] = user_id
    return ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 6,  # ACCESS_CONTROL
                "event": 15,  # NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
                "label": "Access Control",
                "eventLabel": "New user code not added due to duplicate code",
                "parameters": params,
            },
        },
    )


@pytest.fixture
def mock_zwave_usercodes(zwave_client: MagicMock):
    """Mock Z-Wave JS usercode functions with mutable state.

    Both ``get_usercodes`` and ``get_usercode`` read from a shared mutable
    ``codes`` dict.  The client's ``async_send_command`` is wrapped so that
    set / clear Z-Wave commands automatically update ``codes``, preventing
    the coordinator refresh from overwriting optimistic push updates with
    stale data.

    Yields ``(mock_get_usercodes, mock_get_usercode, codes)`` where *codes*
    is ``dict[int, dict]`` keyed by slot number.
    """
    codes: dict[int, dict] = {}

    original_side_effect = zwave_client.async_send_command.side_effect

    async def _send_command_with_codes(message, require_schema=None):
        if message.get("command") == "node.set_value":
            vid = message.get("valueId", {})
            if vid.get("commandClass") == CommandClass.USER_CODE:
                slot = vid.get("propertyKey")
                if slot is not None:
                    prop = vid.get("property")
                    if prop == "userIdStatus":
                        # Clear operation
                        codes[slot] = {
                            "code_slot": slot,
                            "in_use": False,
                            "usercode": "",
                        }
                    elif prop == "userCode":
                        # Set operation
                        codes[slot] = {
                            "code_slot": slot,
                            "in_use": True,
                            "usercode": str(message["value"]),
                        }
        return await original_side_effect(message, require_schema)

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercodes",
        ) as mock_all,
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        ) as mock_one,
    ):
        mock_all.side_effect = lambda node: list(codes.values())
        mock_one.side_effect = lambda node, slot: codes.get(
            slot, {"code_slot": slot, "in_use": False, "usercode": ""}
        )
        zwave_client.async_send_command.side_effect = _send_command_with_codes
        yield mock_all, mock_one, codes
        zwave_client.async_send_command.side_effect = original_side_effect


async def _setup_lcm_entry(
    hass: HomeAssistant,
    lock_entity_id: str,
    slots: dict[str, dict],
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> MockConfigEntry:
    """Set up a full LCM config entry with real platform entities."""
    _mock_all, _mock_one, codes = mock_zwave_usercodes

    for k, v in slots.items():
        pin = v.get(CONF_PIN, "")
        in_use = bool(pin)
        codes[int(k)] = {"code_slot": int(k), "usercode": pin, "in_use": in_use}

    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock_entity_id],
            CONF_SLOTS: slots,
        },
        unique_id=f"duplicate_code_test_{lock_entity_id}",
    )
    lcm_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()
    return lcm_entry


def _get_enabled_switch_entity_id(hass: HomeAssistant, entry_id: str, slot: int) -> str:
    """Get the enabled switch entity ID for a slot."""
    ent_reg = er.async_get(hass)
    uid = f"{entry_id}|{slot}|{CONF_ENABLED}"
    entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, uid)
    assert entity_id, f"Switch entity not found for slot {slot}"
    return entity_id


async def test_duplicate_code_notification_marks_rejected(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test that event 15 marks the slot rejected and clears in-progress."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"2": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )

    # Find the ZWaveJSLock instance created by LCM setup
    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]
    lock_instance._set_in_progress_code_slot = 2

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    # Slot should be marked as rejected for the sync manager to pick up
    assert 2 in lock_instance._rejected_code_slots

    # In-progress field should be cleared
    assert lock_instance._set_in_progress_code_slot is None

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_duplicate_code_notification_no_user_id_marks_rejected(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test event 15 with no userId in params still marks rejected using in-progress slot."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"3": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )

    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]
    lock_instance._set_in_progress_code_slot = 3

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id)
    )
    await hass.async_block_till_done()

    assert 3 in lock_instance._rejected_code_slots
    assert lock_instance._set_in_progress_code_slot is None

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_duplicate_code_notification_ignored_when_not_in_progress(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test event 15 is ignored when _set_in_progress_code_slot is None."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"2": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )
    switch_entity_id = _get_enabled_switch_entity_id(hass, lcm_entry.entry_id, 2)
    assert hass.states.get(switch_entity_id).state == STATE_ON

    # Do NOT set _set_in_progress_code_slot (external trigger)
    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    # Switch should still be on
    assert hass.states.get(switch_entity_id).state == STATE_ON

    # No repair issue created for slot_disabled
    issue_registry = async_get_issue_registry(hass)
    matching_issues = [
        issue
        for issue in issue_registry.issues.values()
        if issue.domain == DOMAIN and issue.issue_id.startswith("slot_disabled_")
    ]
    assert len(matching_issues) == 0

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_duplicate_code_notification_ignored_when_user_id_mismatches(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test event 15 is ignored when userId doesn't match in-progress slot."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {
            "2": {CONF_NAME: "test2", CONF_PIN: "1234", CONF_ENABLED: True},
            "3": {CONF_NAME: "test3", CONF_PIN: "5678", CONF_ENABLED: True},
        },
        mock_zwave_usercodes,
    )
    switch_2 = _get_enabled_switch_entity_id(hass, lcm_entry.entry_id, 2)
    switch_3 = _get_enabled_switch_entity_id(hass, lcm_entry.entry_id, 3)

    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]

    # LCM is setting slot 2, but notification says slot 3
    lock_instance._set_in_progress_code_slot = 2

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=3)
    )
    await hass.async_block_till_done()

    # Neither switch should be turned off
    assert hass.states.get(switch_2).state == STATE_ON
    assert hass.states.get(switch_3).state == STATE_ON

    # In-progress should NOT be cleared (it wasn't our event)
    assert lock_instance._set_in_progress_code_slot == 2

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_set_in_progress_cleared_on_value_update(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a push value update for the in-progress slot clears the field."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: ""}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Simulate LCM setting a code on slot 2
    zwave_js_lock._set_in_progress_code_slot = 2

    # Simulate a push value update for slot 2 (lock accepted the code)
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_PROPERTY,
                "propertyKey": 2,
                "newValue": "1234",
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # In-progress should be cleared
    assert zwave_js_lock._set_in_progress_code_slot is None

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_internal_set_usercode_raises_duplicate_for_rejected_slot(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test async_internal_set_usercode raises DuplicateCodeError for rejected slots.

    When a slot is in _rejected_code_slots (marked by event 15), the next call to
    async_internal_set_usercode should raise DuplicateCodeError without calling the
    provider's async_set_usercode, and clear the slot from the rejected set.
    """
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"2": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )

    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]

    # Mark slot 2 as rejected (simulating event 15)
    lock_instance.mark_code_rejected(2)
    assert 2 in lock_instance._rejected_code_slots

    # Attempt to set usercode should raise DuplicateCodeError
    with pytest.raises(DuplicateCodeError) as exc_info:
        await lock_instance.async_internal_set_usercode(2, "1234", source="sync")

    assert exc_info.value.code_slot == 2
    assert exc_info.value.conflicting_slot is None
    assert "duplicate detected by lock firmware" in str(exc_info.value)

    # Slot should be cleared from the rejected set
    assert 2 not in lock_instance._rejected_code_slots

    await hass.config_entries.async_unload(lcm_entry.entry_id)
