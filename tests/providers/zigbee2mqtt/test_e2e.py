"""Full lifecycle E2E tests for Zigbee2MQTT lock provider."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)

from .conftest import (
    Z2M_FULL_TOPIC,
    Z2M_GET_TOPIC,
    Z2M_LOCK_ENTITY_ID,
    Z2M_SET_TOPIC,
    MqttMessageBus,
)


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Z2M provider."""

    async def test_provider_discovered_as_zigbee2mqtt(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
    ) -> None:
        """Verify LCM discovers the MQTT lock and creates a Zigbee2MQTTLock."""
        lock = lcm_config_entry.runtime_data.locks.get(Z2M_LOCK_ENTITY_ID)
        assert lock is not None
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
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """The provider subscribes to the Z2M device topic during setup."""
        assert Z2M_FULL_TOPIC in mqtt_bus.subscriptions
        assert len(mqtt_bus.subscriptions[Z2M_FULL_TOPIC]) > 0


class TestPushUpdatesViaMqtt:
    """Verify MQTT messages flow through to the coordinator."""

    async def test_users_payload_updates_coordinator(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Firing a users payload on the device topic updates coordinator data."""
        mqtt_bus.fire_message(
            Z2M_FULL_TOPIC,
            {"users": {"1": {"status": "enabled", "pin_code": "1234"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(1) == "1234"

    async def test_multiple_slots_in_single_message(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """Multiple user slots in one MQTT message all reach the coordinator."""
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

        assert z2m_lock.coordinator.data.get(1) == "1111"
        assert z2m_lock.coordinator.data.get(2) == "2222"
        assert z2m_lock.coordinator.data.get(3) is SlotCode.EMPTY

    async def test_disabled_slot_maps_to_empty(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """A disabled user slot is reported as SlotCode.EMPTY."""
        mqtt_bus.fire_message(
            Z2M_FULL_TOPIC,
            {"users": {"5": {"status": "disabled"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(5) is SlotCode.EMPTY


class TestSetAndClearUsercodes:
    """Verify set/clear operations publish correct MQTT payloads."""

    async def test_set_usercode_publishes_correct_payload(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """async_set_usercode publishes the correct SET payload."""
        await z2m_lock.async_set_usercode(1, "9999", "TestUser")

        assert any(topic == Z2M_SET_TOPIC for topic, _ in mqtt_bus.publishes)
        set_publishes = [
            json.loads(p) for t, p in mqtt_bus.publishes if t == Z2M_SET_TOPIC
        ]
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
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """After set, the coordinator has the optimistic value."""
        await z2m_lock.async_set_usercode(1, "9999")

        assert z2m_lock.coordinator.data.get(1) == "9999"

    async def test_clear_usercode_publishes_disable_payload(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """async_clear_usercode publishes user_enabled=false."""
        await z2m_lock.async_clear_usercode(1)

        set_publishes = [
            json.loads(p) for t, p in mqtt_bus.publishes if t == Z2M_SET_TOPIC
        ]
        assert any(
            pub.get("pin_code", {}).get("user") == 1
            and pub.get("pin_code", {}).get("user_enabled") is False
            for pub in set_publishes
        )

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """After clear, the coordinator has SlotCode.EMPTY."""
        await z2m_lock.async_clear_usercode(1)

        assert z2m_lock.coordinator.data.get(1) is SlotCode.EMPTY


class TestGetUsercodes:
    """Verify the full GET request/response cycle."""

    async def test_get_usercodes_publishes_get_requests(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """async_get_usercodes publishes GET requests for all managed slots.

        The auto-responder in the fixture responds with empty slots,
        so the result should contain EMPTY for each slot.
        """
        result = await z2m_lock.async_get_usercodes()

        get_publishes = [
            json.loads(p) for t, p in mqtt_bus.publishes if t == Z2M_GET_TOPIC
        ]
        requested_slots = {pub["pin_code"]["user"] for pub in get_publishes}
        assert 1 in requested_slots
        assert 2 in requested_slots

        # Auto-responder returns user_enabled=False, so slots are EMPTY
        assert result[1] is SlotCode.EMPTY
        assert result[2] is SlotCode.EMPTY

    async def test_get_usercodes_with_responses(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_bus: MqttMessageBus,
    ) -> None:
        """GET requests that receive MQTT responses return the PIN values."""
        original_publish = mqtt_bus.publish

        async def publish_and_respond(hass, topic, payload, **kwargs):
            await original_publish(hass, topic, payload, **kwargs)
            if topic == Z2M_GET_TOPIC:
                body = json.loads(payload)
                slot = body["pin_code"]["user"]
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

        mqtt_bus.publish = publish_and_respond

        result = await z2m_lock.async_get_usercodes()

        assert result[1] == "PIN1"
        assert result[2] == "PIN2"
