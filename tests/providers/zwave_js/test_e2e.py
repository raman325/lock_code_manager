"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from zwave_js_server.const import CommandClass
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.node import Node

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock


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


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Z-Wave JS provider."""

    async def test_provider_discovered_as_zwave_js(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Z-Wave JS lock and creates a ZWaveJSLock."""
        lock = lcm_config_entry.runtime_data.locks.get(lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, ZWaveJSLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_zwave_lock.coordinator is not None

    async def test_push_subscription_established(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """The provider subscribes to Z-Wave JS value updates during setup."""
        assert e2e_zwave_lock._value_update_unsub is not None


class TestSetAndClearUsercodes:
    """Verify set/clear operations send proper Z-Wave commands."""

    async def test_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        zwave_client: MagicMock,
    ) -> None:
        """Set a code via the provider and verify the Z-Wave command was sent."""
        zwave_client.async_send_command.reset_mock()
        result = await e2e_zwave_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True
        assert zwave_client.async_send_command.call_count >= 1

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        zwave_client: MagicMock,
    ) -> None:
        """Clear a code via the provider and verify the Z-Wave command was sent."""
        zwave_client.async_send_command.reset_mock()
        result = await e2e_zwave_lock.async_clear_usercode(2)

        assert result is True
        assert zwave_client.async_send_command.call_count >= 1

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """After set, the coordinator has the optimistic value."""
        await e2e_zwave_lock.async_set_usercode(4, "5678", "Test User")

        assert e2e_zwave_lock.coordinator.data.get(4) == "5678"

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """After clear, the coordinator has SlotCode.EMPTY."""
        await e2e_zwave_lock.async_clear_usercode(2)

        assert e2e_zwave_lock.coordinator.data.get(2) is SlotCode.EMPTY


class TestGetUsercodes:
    """Verify reading usercodes from the Z-Wave JS value cache."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """
        Get usercodes returns the fixture's cached codes.

        The node fixture has slot 1="9999" (in_use), slot 2="1234" (in_use),
        and slot 3=empty (not in_use).
        """
        codes = await e2e_zwave_lock.async_get_usercodes()

        # Slots 1 and 2 are managed by the LCM config
        assert codes[1] == "9999"
        assert codes[2] == "1234"


class TestEvents:
    """Verify Z-Wave notification events flow through to LCM events."""

    async def test_notification_event_fires_lock_state_changed(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
    ) -> None:
        """
        Fire a Z-Wave notification event and verify LCM processes it.

        A keypad lock notification (type 6, event 5) should be handled by the
        provider's event listener. We verify the listener is active by checking
        the provider has registered listeners.
        """
        events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

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
                    "event": 5,
                    "label": "Access Control",
                    "eventLabel": "Keypad lock operation",
                    "parameters": {"userId": 1},
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0].data[ATTR_CODE_SLOT] == 1

    async def test_push_value_update_reaches_coordinator(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
    ) -> None:
        """A Z-Wave value update event for a usercode updates the coordinator."""
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 1,
                    "newValue": "4321",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        assert e2e_zwave_lock.coordinator.data.get(1) == "4321"
