"""Full lifecycle E2E tests for Zigbee2MQTT lock provider."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import DEFAULT

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)

from .conftest import Z2M_FULL_TOPIC, Z2M_GET_TOPIC, Z2M_SET_TOPIC, get_z2m_lock

# Full LCM setup briefly holds the coordinator's debounced refresh lock while
# the initial sync runs. Concurrent async_request_refresh calls during that
# window hit the HA Debouncer regression introduced in home-assistant/core
# commit 7203cffbd73 (#153596), which orphans an extra call_later TimerHandle
# that Debouncer.async_shutdown does not cancel. Accept the lingering timer
# until the upstream fix lands.
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


def _published_payloads(mqtt_mock, topic: str) -> list[dict[str, Any]]:
    """
    Decode JSON payloads published to a topic via the HA MQTT client mock.

    The client mock records ``async_publish(topic, payload, qos, retain,
    message_expiry_interval=...)`` calls with topic and payload positional.
    """
    return [
        json.loads(call.args[1])
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == topic
    ]


def _fire_device_payload(hass: HomeAssistant, payload: dict[str, Any]) -> None:
    """Fire a JSON payload on the lock's Zigbee2MQTT device topic."""
    async_fire_mqtt_message(hass, Z2M_FULL_TOPIC, json.dumps(payload))


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Z2M provider."""

    async def test_provider_discovered_as_zigbee2mqtt(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
    ) -> None:
        """Verify LCM discovers the MQTT lock and creates a Zigbee2MQTTLock."""
        lock = get_z2m_lock(lcm_config_entry)
        assert isinstance(lock, Zigbee2MQTTLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert z2m_lock.coordinator is not None

    async def test_mqtt_subscription_established(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """The provider subscribes to the Z2M device topic during setup."""
        assert z2m_lock._subscribed_topic == Z2M_FULL_TOPIC
        assert z2m_lock._push_unsubs


class TestPushUpdatesViaMqtt:
    """Verify MQTT messages flow through to the coordinator."""

    async def test_users_payload_updates_coordinator(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """Firing a users payload on the device topic updates coordinator data."""
        _fire_device_payload(
            hass,
            {"users": {"1": {"status": "enabled", "pin_code": "1234"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "1234"
        )

    async def test_multiple_slots_in_single_message(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """Multiple user slots in one MQTT message all reach the coordinator."""
        _fire_device_payload(
            hass,
            {
                "users": {
                    "1": {"status": "enabled", "pin_code": "1111"},
                    "2": {"status": "enabled", "pin_code": "2222"},
                    "3": {"status": "disabled"},
                }
            },
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "1111"
        )
        assert z2m_lock.coordinator.data.get(pin_address(2)) == SlotCredential.known(
            "2222"
        )
        assert (
            z2m_lock.coordinator.data.get(pin_address(3)) is SlotCredential.unreadable()
        )

    async def test_disabled_slot_is_occupied_not_empty(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """A disabled user is one the lock is holding and refusing.

        Reporting it empty tells sync the slot is confirmed cleared and tells
        allocation the credential index is free to hand out.
        """
        _fire_device_payload(
            hass,
            {"users": {"5": {"status": "disabled"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        credential = z2m_lock.coordinator.data.get(pin_address(5))
        assert credential.is_present
        assert not credential.is_readable

    async def test_available_slot_is_empty(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """``available`` is the one status that does mean nothing is there."""
        _fire_device_payload(
            hass,
            {"users": {"6": {"status": "available"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(pin_address(6)) is SlotCredential.empty()


class TestSetAndClearUsercodes:
    """Verify set/clear operations publish correct MQTT payloads."""

    async def test_set_usercode_publishes_correct_payload(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """async_set_usercode publishes the correct SET payload."""
        await z2m_lock.async_set_usercode(1, "9999", "TestUser")

        set_publishes = _published_payloads(mqtt_mock, Z2M_SET_TOPIC)
        assert set_publishes
        assert any(
            pub.get("pin_code", {}).get("user") == 1
            and pub.get("pin_code", {}).get("pin_code") == "9999"
            and pub.get("pin_code", {}).get("user_enabled") is True
            for pub in set_publishes
        )

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """After set, the coordinator has the optimistic value."""
        await z2m_lock.async_set_usercode(1, "9999")

        assert z2m_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "9999"
        )

    async def test_clear_usercode_publishes_disable_payload(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """async_clear_usercode publishes user_enabled=false."""
        await z2m_lock.async_clear_usercode(1)

        set_publishes = _published_payloads(mqtt_mock, Z2M_SET_TOPIC)
        assert any(
            pub.get("pin_code", {}).get("user") == 1
            and pub.get("pin_code", {}).get("user_enabled") is False
            for pub in set_publishes
        )

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """After clear, the coordinator has SlotCredential.empty()."""
        await z2m_lock.async_clear_usercode(1)

        assert z2m_lock.coordinator.data.get(pin_address(1)) is SlotCredential.empty()


class TestGetUsercodes:
    """Verify the full GET request/response cycle."""

    async def test_get_usercodes_publishes_get_requests(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """
        async_get_usercodes publishes GET requests for all managed slots.

        The auto-GET responder answers with disabled slots, so the result
        should contain EMPTY for each slot.
        """
        result = await z2m_lock.async_get_usercodes()

        get_publishes = _published_payloads(mqtt_mock, Z2M_GET_TOPIC)
        requested_slots = {pub["pin_code"]["user"] for pub in get_publishes}
        assert 1 in requested_slots
        assert 2 in requested_slots

        # Auto-responder returns user_enabled=False, so slots are EMPTY
        assert result[1] is SlotCredential.empty()
        assert result[2] is SlotCredential.empty()

    async def test_get_usercodes_with_responses(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """GET requests that receive MQTT responses return the PIN values."""

        def respond_with_pin(topic: str, payload: str, *args: Any, **kwargs: Any):
            if topic == Z2M_GET_TOPIC:
                slot = json.loads(payload)["pin_code"]["user"]
                _fire_device_payload(
                    hass,
                    {
                        "pin_code": {
                            "user": slot,
                            "user_enabled": True,
                            "pin_code": f"PIN{slot}",
                        }
                    },
                )
            return DEFAULT

        mqtt_mock.async_publish.side_effect = respond_with_pin

        result = await z2m_lock.async_get_usercodes()

        assert result[1] == SlotCredential.known("PIN1")
        assert result[2] == SlotCredential.known("PIN2")
