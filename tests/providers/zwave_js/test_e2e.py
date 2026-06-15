"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const.command_class.access_control import (
    UserCredentialType,
    UserCredentialUserType,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.access_control import CredentialData, UserData
from zwave_js_server.model.node import Node

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.domain.credentials import WriteResult
from custom_components.lock_code_manager.domain.models import SlotCredential
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
        assert e2e_zwave_lock._push_unsubs


class TestSetAndClearUsercodes:
    """Verify set/clear operations invoke the unified access-control primitives."""

    async def test_set_usercode_calls_lock_helpers(
        self,
        hass: HomeAssistant,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        zwave_integration: MockConfigEntry,
    ) -> None:
        """Setting a code drives async_set_user then async_set_credential via lock_helpers."""
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zwave_js_lock.lock.entity_id],
                CONF_SLOTS: {"4": {}},
            },
        )
        lcm_entry.add_to_hass(hass)
        zwave_js_lock._min_operation_delay = 0.0
        mock_access_control.get_user_cached.return_value = None
        mock_lock_helpers["async_set_user"].return_value = {"user_id": 4}

        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is WriteResult.CONFIRMED
        mock_lock_helpers["async_set_user"].assert_called_once()
        mock_lock_helpers["async_set_credential"].assert_called_once()

    async def test_clear_usercode_calls_lock_helpers(
        self,
        hass: HomeAssistant,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        zwave_integration: MockConfigEntry,
    ) -> None:
        """Clearing a slot resolves the owner then calls async_delete_credential."""
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zwave_js_lock.lock.entity_id],
                CONF_SLOTS: {"2": {}},
            },
        )
        lcm_entry.add_to_hass(hass)
        zwave_js_lock._min_operation_delay = 0.0
        mock_access_control.get_users_cached.return_value = [
            UserData(
                user_id=2,
                active=True,
                user_type=UserCredentialUserType.GENERAL,
                user_name="bob",
            ),
        ]
        mock_access_control.get_all_credentials_cached.return_value = [
            CredentialData(
                user_id=2,
                type=UserCredentialType.PIN_CODE,
                slot=2,
                data="1234",
            ),
        ]

        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True
        mock_lock_helpers["async_delete_credential"].assert_called_once()


class TestGetUsercodes:
    """Verify reading usercodes from the access_control API."""

    async def test_get_usercodes_returns_codes_from_access_control(
        self,
        hass: HomeAssistant,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        zwave_integration: MockConfigEntry,
    ) -> None:
        """
        async_get_usercodes projects access_control users and credentials to slots.

        The access_control fixture is seeded with two users at slots 1 and 2.
        The result maps each slot to the readable Personal Identification Number.
        """
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zwave_js_lock.lock.entity_id],
                CONF_SLOTS: {"1": {}, "2": {}},
            },
        )
        lcm_entry.add_to_hass(hass)
        mock_access_control.get_users_cached.return_value = [
            UserData(
                user_id=1,
                active=True,
                user_type=UserCredentialUserType.GENERAL,
                user_name="alice",
            ),
            UserData(
                user_id=2,
                active=True,
                user_type=UserCredentialUserType.GENERAL,
                user_name="bob",
            ),
        ]
        mock_access_control.get_all_credentials_cached.return_value = [
            CredentialData(
                user_id=1,
                type=UserCredentialType.PIN_CODE,
                slot=1,
                data="9999",
            ),
            CredentialData(
                user_id=2,
                type=UserCredentialType.PIN_CODE,
                slot=2,
                data="1234",
            ),
        ]

        codes = await zwave_js_lock.async_get_usercodes()

        assert codes[1] == SlotCredential.known("9999")
        assert codes[2] == SlotCredential.known("1234")


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

    async def test_push_credential_added_reaches_coordinator(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
    ) -> None:
        """A credential-added node event for a Personal Identification Number updates the coordinator."""
        event = ZwaveEvent(
            type="credential added",
            data={
                "source": "node",
                "event": "credential added",
                "nodeId": lock_schlage_be469.node_id,
                "endpointIndex": 0,
                "args": {
                    "userId": 1,
                    "credentialType": UserCredentialType.PIN_CODE,
                    "credentialSlot": 1,
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        # Credential events push unreadable (the lock doesn't expose the Personal
        # Identification Number value in the event; a coordinator refresh reads it).
        assert e2e_zwave_lock.coordinator.data.get(1) == SlotCredential.unreadable()
