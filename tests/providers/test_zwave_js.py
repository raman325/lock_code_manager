"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.lock import (
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.node import Node

from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

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

    assert codes[1] == "9999"
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


async def test_set_usercode_skips_when_masked_code_matches(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode returns False when masked code resolves to same PIN.

    Some locks (like Yale) return masked PINs (****) instead of actual codes.
    When the masked code resolves to the same PIN we're trying to set, we should
    skip the set operation to prevent battery drain from repeated writes.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to return a masked code
    masked_slot = {"code_slot": 2, "usercode": "****", "in_use": True}

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            return_value=masked_slot,
        ),
        patch.object(
            zwave_js_lock, "_resolve_pin_if_masked", return_value="5678"
        ) as mock_resolve,
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        # Try to set the same PIN that the masked code resolves to
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should skip the set operation
        assert result is False
        mock_resolve.assert_called_once_with("****", 2)
        mock_service.assert_not_called()

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_proceeds_when_masked_code_differs(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode proceeds when masked code resolves to different PIN.

    When the masked code resolves to a different PIN than what we're trying to set,
    the set operation should proceed normally.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to return a masked code
    masked_slot = {"code_slot": 2, "usercode": "****", "in_use": True}

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            return_value=masked_slot,
        ),
        patch.object(zwave_js_lock, "_resolve_pin_if_masked", return_value="1234"),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        # Try to set a different PIN than what the masked code resolves to
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should proceed with the set operation
        assert result is True
        mock_service.assert_called_once()

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_proceeds_when_masked_code_unresolvable(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that set_usercode proceeds when masked code cannot be resolved.

    When the masked code cannot be resolved (e.g., slot not managed by LCM),
    the set operation should proceed to ensure the code gets set.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to return a masked code
    masked_slot = {"code_slot": 2, "usercode": "****", "in_use": True}

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            return_value=masked_slot,
        ),
        patch.object(zwave_js_lock, "_resolve_pin_if_masked", return_value=None),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        # Try to set a PIN when masked code can't be resolved
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should proceed with the set operation
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
    await zwave_js_lock.async_setup(lcm_entry)

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
    """
    Test that optimistic update prevents sync loops from stale cache reads.

    This test verifies the fix for the reported issue where out-of-sync slots
    cause constant lock activity. The scenario:
    1. LCM sets a code on the lock
    2. Z-Wave command succeeds (lock acknowledges)
    3. Without optimistic update: coordinator.data still has old value
    4. Binary sensor sees mismatch → triggers another sync → loop

    With the fix, push_update immediately sets coordinator.data to the new value,
    so the binary sensor sees the expected value and doesn't retry.
    """
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator with stale data (still shows old PIN)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Stale: slot still shows PIN
    zwave_js_lock.coordinator = mock_coordinator

    with patch.object(zwave_js_lock, "async_call_service", new_callable=AsyncMock):
        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True
        # Verify optimistic update was called with empty string
        mock_coordinator.push_update.assert_called_once_with({2: ""})

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock_v2.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
        # Slot 5 returns masked value (so sync logic knows a PIN exists)
        assert codes[5] == "****"

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_masked_pin_resolved_when_active(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that masked PINs are resolved to the configured PIN when slot is active.

    When active=ON and the PIN entity has a valid numeric PIN, the masked code
    should be resolved to that PIN value.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to have a masked PIN on a managed slot
    masked_slots = [
        {"code_slot": 2, "usercode": "****", "in_use": True},
    ]

    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
        ),
        patch.object(zwave_js_lock, "_resolve_pin_if_masked", return_value="5678"),
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Masked PIN should be resolved to the configured PIN
        assert codes[2] == "5678"

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_masked_pin_skipped_when_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that masked PINs are skipped when slot is inactive (active=OFF).

    When the slot is managed but active=OFF (or entities not ready), the masked
    code cannot be resolved and the slot is skipped entirely.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to have a masked PIN on a managed slot
    masked_slots = [
        {"code_slot": 2, "usercode": "****", "in_use": True},
    ]

    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
        ),
        patch.object(zwave_js_lock, "_resolve_pin_if_masked", return_value=None),
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot should be skipped entirely since it can't be resolved
        assert 2 not in codes

    await zwave_js_lock.async_unload(False)


async def test_resolve_pin_if_masked_detection(zwave_js_lock: ZWaveJSLock) -> None:
    """Test _resolve_pin_if_masked returns value as-is when not masked."""
    # Not masked - returns value as-is
    assert zwave_js_lock._resolve_pin_if_masked("1234", 1) == "1234"
    assert zwave_js_lock._resolve_pin_if_masked("5678", 2) == "5678"
    assert zwave_js_lock._resolve_pin_if_masked("", 1) == ""
    # Partially masked - returns as-is (not all asterisks)
    assert zwave_js_lock._resolve_pin_if_masked("***1", 1) == "***1"
    assert zwave_js_lock._resolve_pin_if_masked("1***", 1) == "1***"


async def test_push_update_masked_code_resolved(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that push updates with masked codes resolve and update coordinator.

    When a push update arrives with a masked code and it can be resolved,
    the resolved PIN should be pushed to the coordinator.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator (push_update is synchronous)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _resolve_pin_if_masked to return a PIN
    with patch.object(zwave_js_lock, "_resolve_pin_if_masked", return_value="9876"):
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

        # Coordinator should receive the resolved PIN, not the masked value
        mock_coordinator.push_update.assert_called_once_with({2: "9876"})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_masked_code_skipped_when_unresolvable(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that push updates with unresolvable masked codes are skipped.

    When a push update arrives with a masked code that cannot be resolved,
    the update should be skipped entirely to prevent infinite sync loops.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator (push_update is synchronous)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _resolve_pin_if_masked to return None (can't resolve)
    with patch.object(zwave_js_lock, "_resolve_pin_if_masked", return_value=None):
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

        # Coordinator should NOT receive any update
        mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# Integration tests for _resolve_pin_if_masked (without mocking)


async def test_resolve_pin_if_masked_returns_pin_when_active(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test _resolve_pin_if_masked returns PIN when slot is active with valid PIN.

    This integration test exercises the actual resolution logic without mocking
    to verify entity lookup and state checking works correctly.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active (binary_sensor) and PIN (text) entities with proper unique IDs
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    active_entry = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{base_unique_id}|active",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=ON, pin="5678"
    hass.states.async_set(active_entry.entity_id, "on")
    hass.states.async_set(pin_entry.entity_id, "5678")
    await hass.async_block_till_done()

    # Mock code_slot_in_use to return True (slot has a code on the lock)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # _resolve_pin_if_masked should return the PIN value when given a masked code
        result = zwave_js_lock._resolve_pin_if_masked("****", 3)
        assert result == "5678"

    await zwave_js_lock.async_unload(False)


async def test_resolve_pin_if_masked_returns_masked_value_when_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test _resolve_pin_if_masked returns masked value when slot is inactive.

    When the slot is managed but active=OFF (slot not enabled), the masked
    code is returned as-is. This ensures sync logic knows a PIN is set on
    the lock, even though the slot isn't currently active in LCM.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active (binary_sensor) and PIN (text) entities
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    active_entry = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{base_unique_id}|active",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=OFF (inactive), pin="5678"
    hass.states.async_set(active_entry.entity_id, "off")
    hass.states.async_set(pin_entry.entity_id, "5678")
    await hass.async_block_till_done()

    # Mock code_slot_in_use to return True (slot has a code on the lock)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # _resolve_pin_if_masked should return the masked value (not None)
        # so sync logic knows a PIN exists on the lock
        result = zwave_js_lock._resolve_pin_if_masked("****", 3)
        assert result == "****"

    await zwave_js_lock.async_unload(False)


async def test_resolve_pin_if_masked_returns_pin_even_if_not_numeric(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test _resolve_pin_if_masked returns PIN state even if not strictly numeric.

    The .isnumeric() check was removed to handle edge cases where PINs might
    be stored in non-standard formats. When active=ON, the PIN entity state
    is returned regardless of format.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active (binary_sensor) and PIN (text) entities
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    active_entry = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{base_unique_id}|active",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=ON, pin="unknown" (non-numeric)
    hass.states.async_set(active_entry.entity_id, "on")
    hass.states.async_set(pin_entry.entity_id, "unknown")
    await hass.async_block_till_done()

    # Mock code_slot_in_use to return True (slot has a code on the lock)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # _resolve_pin_if_masked should return the PIN state as-is
        result = zwave_js_lock._resolve_pin_if_masked("****", 3)
        assert result == "unknown"

    await zwave_js_lock.async_unload(False)


async def test_resolve_pin_if_masked_returns_masked_for_unmanaged_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test _resolve_pin_if_masked returns masked value for slots not managed by LCM.

    When code_slot_in_use returns None (slot not in Z-Wave node data), the
    masked value is returned as-is so sync logic knows a PIN exists.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},  # Only slot 1 is managed
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock code_slot_in_use to return None (slot not in node data)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=None):
        # _resolve_pin_if_masked returns masked value (slot_in_use is falsy)
        result = zwave_js_lock._resolve_pin_if_masked("****", 99)
        assert result == "****"

    await zwave_js_lock.async_unload(False)


async def test_resolve_pin_if_masked_returns_masked_when_entities_missing(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test _resolve_pin_if_masked returns None when entities are not registered.

    When slot is in use but entities are missing (config entry exists but entities
    not created yet), the method returns None because it cannot look up the
    active state or PIN.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Don't create any entities - they're "missing"

    # Mock code_slot_in_use to return True (slot has a code on the lock)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # _resolve_pin_if_masked returns None when entities not found
        # (can't look up active state or PIN)
        result = zwave_js_lock._resolve_pin_if_masked("****", 3)
        assert result is None

    await zwave_js_lock.async_unload(False)


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
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator with existing data
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
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

    # Coordinator should be updated with empty string
    mock_coordinator.push_update.assert_called_once_with({2: ""})

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
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator - slot already empty
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: ""}  # Slot already empty
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
    await zwave_js_lock.async_setup(lcm_entry)

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


async def test_code_slot_in_use_returns_true(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test code_slot_in_use returns True when slot is in use."""
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        return_value={"in_use": True, "usercode": "1234"},
    ):
        result = zwave_js_lock.code_slot_in_use(1)
        assert result is True


async def test_code_slot_in_use_returns_false(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test code_slot_in_use returns False when slot is not in use."""
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        return_value={"in_use": False, "usercode": ""},
    ):
        result = zwave_js_lock.code_slot_in_use(1)
        assert result is False


async def test_code_slot_in_use_returns_none_on_key_error(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test code_slot_in_use returns None when KeyError occurs."""
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        side_effect=KeyError("slot not found"),
    ):
        result = zwave_js_lock.code_slot_in_use(99)
        assert result is None


async def test_code_slot_in_use_returns_none_on_value_error(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test code_slot_in_use returns None when ValueError occurs."""
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        side_effect=ValueError("invalid slot"),
    ):
        result = zwave_js_lock.code_slot_in_use(0)
        assert result is None


# All-zeros handling tests


async def test_resolve_pin_if_masked_all_zeros_slot_not_in_use(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test _resolve_pin_if_masked treats all-zeros as cleared when slot not in use.

    Some locks return all zeros (e.g., "0000") instead of a blank value when
    a slot is cleared. When the slot is confirmed not in use, these should
    be treated as cleared (empty string).
    """
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=False):
        # All zeros with slot not in use → empty string (cleared)
        assert zwave_js_lock._resolve_pin_if_masked("0000", 1) == ""
        assert zwave_js_lock._resolve_pin_if_masked("000000", 1) == ""
        assert zwave_js_lock._resolve_pin_if_masked("00", 1) == ""


async def test_resolve_pin_if_masked_all_zeros_slot_in_use(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test _resolve_pin_if_masked returns all-zeros as-is when slot is in use.

    When a slot is marked as in use, all-zeros should be returned as-is
    since it might be a valid PIN (though unusual).
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # All zeros with slot in use → returned as-is (not masked asterisks)
        result = zwave_js_lock._resolve_pin_if_masked("0000", 1)
        assert result == "0000"

    await zwave_js_lock.async_unload(False)


async def test_resolve_pin_if_masked_all_zeros_slot_unknown(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test _resolve_pin_if_masked returns all-zeros as-is when slot status unknown.

    When code_slot_in_use returns None (unable to determine status),
    all-zeros should be returned as-is to be safe.
    """
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=None):
        # All zeros with unknown slot status → returned as-is
        result = zwave_js_lock._resolve_pin_if_masked("0000", 1)
        assert result == "0000"


# _slot_expects_pin tests


async def test_slot_expects_pin_returns_true_when_active_with_pin(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _slot_expects_pin returns True when active=ON and PIN is set."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active and PIN entities
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|2"

    active_entry = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{base_unique_id}|active",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=ON, pin="1234"
    hass.states.async_set(active_entry.entity_id, "on")
    hass.states.async_set(pin_entry.entity_id, "1234")
    await hass.async_block_till_done()

    assert zwave_js_lock._slot_expects_pin(2) is True

    await zwave_js_lock.async_unload(False)


async def test_slot_expects_pin_returns_false_when_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _slot_expects_pin returns False when active=OFF."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active and PIN entities
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|2"

    active_entry = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{base_unique_id}|active",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=OFF, pin="1234"
    hass.states.async_set(active_entry.entity_id, "off")
    hass.states.async_set(pin_entry.entity_id, "1234")
    await hass.async_block_till_done()

    assert zwave_js_lock._slot_expects_pin(2) is False

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

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
        mock_coordinator.push_update.assert_called_once_with({2: ""})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)
