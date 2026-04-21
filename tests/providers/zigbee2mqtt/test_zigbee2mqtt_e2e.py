"""End-to-end tests for the Zigbee2MQTT lock provider.

These tests patch async_subscribe and async_publish at the Home Assistant MQTT
component boundary (not inside the provider), so the full Z2M provider code
runs unpatched through the real HA MQTT API surface. A lightweight message bus
connects subscribe callbacks to simulated broker messages.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.providers.zigbee2mqtt import Zigbee2MQTTLock

from .conftest import (
    Z2M_FULL_TOPIC,
    Z2M_GET_TOPIC,
    Z2M_SET_TOPIC,
    MqttMessageBus,
)


@pytest.mark.usefixtures("mqtt_patches")
class TestPushUpdateViaMqtt:
    """Verify that incoming MQTT messages flow through to coordinator data."""

    async def test_push_update_via_mqtt_message(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Firing a users payload on the device topic updates the coordinator."""
        z2m_lock.coordinator = MagicMock()

        mqtt_bus.fire_message(
            Z2M_FULL_TOPIC,
            {"users": {"1": {"status": "enabled", "pin_code": "1234"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        z2m_lock.coordinator.push_update.assert_called_once_with({1: "1234"})

    async def test_push_update_multiple_slots(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Multiple user slots in a single MQTT message all reach the coordinator."""
        z2m_lock.coordinator = MagicMock()

        mqtt_bus.fire_message(
            Z2M_FULL_TOPIC,
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

        call_args = z2m_lock.coordinator.push_update.call_args[0][0]
        assert call_args[1] == "1111"
        assert call_args[2] == "2222"

    async def test_push_update_triggers_coordinator_refresh(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """A users payload update flows through to coordinator.push_update."""
        z2m_lock.coordinator = MagicMock()

        mqtt_bus.fire_message(
            Z2M_FULL_TOPIC,
            {"users": {"5": {"status": "enabled", "pin_code": "9999"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        # The coordinator's push_update was invoked, proving the full path
        # from MQTT message -> provider callback -> coordinator update works.
        z2m_lock.coordinator.push_update.assert_called_once_with({5: "9999"})


@pytest.mark.usefixtures("mqtt_patches")
class TestSetUsercodePublishesMqtt:
    """Verify that async_set_usercode publishes the correct MQTT payload."""

    async def test_set_usercode_publishes_mqtt(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Setting a usercode publishes the correct set payload to the broker."""
        z2m_lock.coordinator = MagicMock()

        result = await z2m_lock.async_set_usercode(1, "9999")

        assert result is True
        assert len(mqtt_bus.publishes) == 1
        topic, payload_str = mqtt_bus.publishes[0]
        assert topic == Z2M_SET_TOPIC
        payload = json.loads(payload_str)
        assert payload == {
            "pin_code": {
                "user": 1,
                "user_type": "unrestricted",
                "pin_code": "9999",
                "user_enabled": True,
            }
        }

    async def test_set_usercode_pushes_optimistic_update(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """After publishing, the provider optimistically updates the coordinator."""
        z2m_lock.coordinator = MagicMock()

        await z2m_lock.async_set_usercode(3, "5555")

        z2m_lock.coordinator.push_update.assert_called_once_with({3: "5555"})


@pytest.mark.usefixtures("mqtt_patches")
class TestClearUsercodePublishesMqtt:
    """Verify that async_clear_usercode publishes the correct MQTT payload."""

    async def test_clear_usercode_publishes_mqtt(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Clearing a usercode publishes a disable payload with null pin_code."""
        z2m_lock.coordinator = MagicMock()

        result = await z2m_lock.async_clear_usercode(4)

        assert result is True
        assert len(mqtt_bus.publishes) == 1
        topic, payload_str = mqtt_bus.publishes[0]
        assert topic == Z2M_SET_TOPIC
        payload = json.loads(payload_str)
        assert payload["pin_code"]["user"] == 4
        assert payload["pin_code"]["user_enabled"] is False
        assert payload["pin_code"]["pin_code"] is None


@pytest.mark.usefixtures("mqtt_patches")
class TestGetUsercodesPublishesAndReceivesResponse:
    """Verify the full get_usercodes request/response cycle via MQTT."""

    async def test_get_usercodes_publishes_and_receives_response(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Calling get_usercodes publishes GET requests and processes responses."""
        # Wire up the bus to auto-respond to GET requests with pin_code payloads,
        # simulating what Zigbee2MQTT does when queried for a slot.
        original_publish = mqtt_bus.publish

        async def respond_to_get(hass, topic, payload, **kwargs):
            await original_publish(hass, topic, payload, **kwargs)
            if topic == Z2M_GET_TOPIC:
                body = json.loads(payload)
                slot = body["pin_code"]["user"]
                # Simulate Z2M responding on the device topic
                mqtt_bus.fire_message(
                    Z2M_FULL_TOPIC,
                    {
                        "pin_code": {
                            "user": slot,
                            "user_enabled": True,
                            "pin_code": f"PIN{slot}",
                        }
                    },
                )

        mqtt_bus.publish = respond_to_get  # type: ignore[assignment]

        managed = {1, 2}
        with patch(
            "custom_components.lock_code_manager.providers._base.get_managed_slots",
            return_value=managed,
        ):
            result = await z2m_lock.async_get_usercodes()

        # Verify GET publishes went out for each managed slot
        get_publishes = [(t, p) for t, p in mqtt_bus.publishes if t == Z2M_GET_TOPIC]
        assert len(get_publishes) == 2
        published_slots = {json.loads(p)["pin_code"]["user"] for _, p in get_publishes}
        assert published_slots == managed

        # Verify results contain the response data
        assert result == {1: "PIN1", 2: "PIN2"}

    async def test_get_usercodes_empty_managed_slots(
        self,
        hass: HomeAssistant,
        z2m_lock: Zigbee2MQTTLock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """No managed slots yields an empty dict without publishing anything."""
        with patch(
            "custom_components.lock_code_manager.providers._base.get_managed_slots",
            return_value=set(),
        ):
            result = await z2m_lock.async_get_usercodes()

        assert result == {}
        assert len(mqtt_bus.publishes) == 0
