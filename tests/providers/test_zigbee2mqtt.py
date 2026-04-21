"""Tests for the Zigbee2MQTT lock provider."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
    _mqtt_payload_pin_has_code_value,
)

Z2M_TOPIC_NAME = "TestLockZ2M"
Z2M_FULL_TOPIC = f"zigbee2mqtt/{Z2M_TOPIC_NAME}"
Z2M_GET_TOPIC = f"{Z2M_FULL_TOPIC}/get"
Z2M_SET_TOPIC = f"{Z2M_FULL_TOPIC}/set"
Z2M_LOCK_ENTITY_ID = "lock.mqtt_test_z2m"

# LCM config: one lock, two slots
Z2M_LCM_CONFIG = {
    CONF_LOCKS: [Z2M_LOCK_ENTITY_ID],
    CONF_SLOTS: {
        1: {"name": "slot1", "pin": "1234", "enabled": True},
        2: {"name": "slot2", "pin": "5678", "enabled": True},
    },
}


@dataclass
class MqttMessageBus:
    """Lightweight MQTT message bus for E2E testing.

    Tracks subscriptions and publishes so tests can fire incoming messages
    and verify outgoing publishes without a real Paho client.
    """

    subscriptions: dict[str, list[Callable]] = field(default_factory=dict)
    publishes: list[tuple[str, str]] = field(default_factory=list)

    def subscribe(self, topic: str, callback: Callable) -> Callable[[], None]:
        """Register a subscription callback and return an unsubscribe handle."""
        self.subscriptions.setdefault(topic, []).append(callback)

        def unsub() -> None:
            self.subscriptions.get(topic, []).remove(callback)

        return unsub

    async def publish(
        self,
        hass: HomeAssistant,
        topic: str,
        payload: str,
        **kwargs: Any,
    ) -> None:
        """Record a published message."""
        self.publishes.append((topic, payload))

    def fire_message(self, topic: str, payload: dict[str, Any]) -> None:
        """Simulate an incoming MQTT message on a topic.

        Creates a ReceiveMessage-like object and dispatches to all
        registered callbacks for the topic.
        """
        msg = MagicMock(spec=ReceiveMessage)
        msg.topic = topic
        msg.payload = json.dumps(payload).encode()
        msg.qos = 0
        msg.retain = False
        for cb in self.subscriptions.get(topic, []):
            cb(msg)


@pytest.fixture
def mqtt_bus() -> MqttMessageBus:
    """Create a fresh MQTT message bus for the test."""
    return MqttMessageBus()


@pytest.fixture
def mqtt_patches(mqtt_bus: MqttMessageBus):
    """Patch async_subscribe and async_publish at the HA MQTT component boundary.

    Also patches mqtt_config_entry_enabled to return True so the provider
    does not bail out during setup.
    """

    async def fake_subscribe(hass, topic, callback, *args, **kwargs):
        return mqtt_bus.subscribe(topic, callback)

    async def fake_publish(hass, topic, payload, *args, **kwargs):
        await mqtt_bus.publish(hass, topic, payload, **kwargs)
        # Auto-respond to GET requests so the coordinator's initial
        # refresh doesn't block on 10-second timeouts per slot.
        if topic.endswith("/get"):
            try:
                body = json.loads(payload)
                slot = body.get("pin_code", {}).get("user")
                if slot is not None:
                    device_topic = topic.rsplit("/get", 1)[0]
                    mqtt_bus.fire_message(
                        device_topic,
                        {
                            "pin_code": {
                                "user": slot,
                                "user_enabled": False,
                                "pin_code": None,
                            }
                        },
                    )
            except (json.JSONDecodeError, TypeError):
                pass

    with (
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            side_effect=fake_subscribe,
        ),
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
            side_effect=fake_publish,
        ),
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
            return_value=True,
        ),
    ):
        yield


@pytest.fixture
async def mqtt_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Create an MQTT config entry, Z2M device, and lock entity.

    Sets the MQTT config entry to LOADED state so the provider's
    async_is_integration_connected check passes.
    """
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, ConfigEntryState.LOADED, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zigbee2mqtt_0xc0ffee")},
        name=Z2M_TOPIC_NAME,
    )

    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "mqtt",
        "test_z2m",
        config_entry=mqtt_entry,
        device_id=device.id,
    )

    hass.states.async_set(lock_entity.entity_id, "locked")

    return lock_entity


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    mqtt_lock_entity: er.RegistryEntry,
    mqtt_patches,
) -> MockConfigEntry:
    """Set up a full LCM config entry managing the Z2M lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the mqtt platform, instantiates Zigbee2MQTTLock,
    creates the coordinator, entities, and sync managers.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN, data=Z2M_LCM_CONFIG, unique_id="test_z2m_e2e"
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_z2m_lock(hass: HomeAssistant, lcm_entry: MockConfigEntry) -> Zigbee2MQTTLock:
    """Extract the Zigbee2MQTTLock from a loaded LCM config entry."""
    lock = lcm_entry.runtime_data.locks.get(Z2M_LOCK_ENTITY_ID)
    assert lock is not None, f"Lock {Z2M_LOCK_ENTITY_ID} not found in runtime data"
    assert isinstance(lock, Zigbee2MQTTLock)
    return lock


@pytest.fixture
def z2m_lock(hass, lcm_config_entry):
    """Extract the Z2M lock from the LCM config entry."""
    return get_z2m_lock(hass, lcm_config_entry)


# ---------------------------------------------------------------------------
# E2E tests — full LCM config entry lifecycle with MQTT message bus
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Unit tests — edge cases, error paths, pure function tests
# ---------------------------------------------------------------------------


def _minimal_lock() -> Zigbee2MQTTLock:
    """Build a Zigbee2MQTTLock without Home Assistant test harness."""
    lock_entity = SimpleNamespace(
        entity_id="lock.test",
        device_id=None,
        platform="mqtt",
        config_entry_id=None,
        unique_id=None,
    )
    return Zigbee2MQTTLock(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        None,
        lock_entity,  # type: ignore[arg-type]
    )


@pytest.fixture
async def zigbee2mqtt_lock_with_device(hass: HomeAssistant) -> Zigbee2MQTTLock:
    """Zigbee2MQTTLock backed by a device with Zigbee2MQTT identifiers."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zigbee2mqtt_0xc0ffee")},
        name=Z2M_TOPIC_NAME,
    )

    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "mqtt",
        "test_z2m_push",
        config_entry=mqtt_entry,
        device_id=device.id,
    )

    return Zigbee2MQTTLock(hass, dev_reg, ent_reg, mqtt_entry, lock_entity)


def test_mqtt_payload_pin_has_code_value_rejects_bool() -> None:
    """Boolean JSON must not count as a PIN payload (truthiness trap)."""
    assert _mqtt_payload_pin_has_code_value(False) is False
    assert _mqtt_payload_pin_has_code_value(True) is False


def test_zigbee2mqtt_provider_properties_and_no_device_entry_skips_name() -> None:
    """Provider metadata and friendly name path when the lock has no device entry."""
    lock = _minimal_lock()
    assert lock.domain == MQTT_DOMAIN
    assert lock.supports_push is True
    assert lock.usercode_scan_interval == timedelta(minutes=5)
    assert lock.hard_refresh_interval == timedelta(hours=1)
    assert lock.connection_check_interval == timedelta(seconds=30)
    assert lock._get_friendly_name() is None


async def test_get_friendly_name_rejects_non_z2m_bridge(
    zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
) -> None:
    """MQTT devices without a zigbee2mqtt_* identifier do not yield a topic name."""
    assert zigbee2mqtt_lock_wrong_identifier._get_friendly_name() is None


async def test_pin_code_get_disabled_or_empty_pin_sets_future_none() -> None:
    """PIN response with disabled user, empty pin, or numeric zero."""
    loop = asyncio.get_running_loop()
    lock = _minimal_lock()

    fut_disabled = loop.create_future()
    lock._pending_codes[7] = fut_disabled
    lock._process_z2m_device_payload(
        {"pin_code": {"user": 7, "user_enabled": False, "pin_code": "1234"}}
    )
    assert fut_disabled.done() and fut_disabled.result() is None

    fut_empty = loop.create_future()
    lock._pending_codes[8] = fut_empty
    lock._process_z2m_device_payload(
        {"pin_code": {"user": 8, "user_enabled": True, "pin_code": ""}}
    )
    assert fut_empty.done() and fut_empty.result() is None

    fut_zero = loop.create_future()
    lock._pending_codes[9] = fut_zero
    lock._process_z2m_device_payload(
        {"pin_code": {"user": 9, "user_enabled": True, "pin_code": 0}}
    )
    assert fut_zero.done() and fut_zero.result() == "0"


async def test_async_is_integration_connected_paths(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """MQTT availability and Z2M friendly name gate integration connectivity."""
    lock = zigbee2mqtt_lock_with_device

    hass.states.async_set(lock.lock.entity_id, "locked")
    with patch(
        "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
        return_value=False,
    ):
        assert await lock.async_is_integration_connected() is False

    with (
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
            return_value=True,
        ),
        patch.object(lock, "_get_friendly_name", return_value=None),
    ):
        assert await lock.async_is_integration_connected() is False


async def test_async_is_device_available_reflects_entity_state(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """Physical availability follows the lock entity state, not MQTT topic resolution."""
    lock = zigbee2mqtt_lock_with_device

    hass.states.async_set(lock.lock.entity_id, "locked")
    assert await lock.async_is_device_available() is True

    hass.states.async_set(lock.lock.entity_id, "unavailable")
    assert await lock.async_is_device_available() is False


async def test_setup_push_subscription_inner_returns_when_mqtt_disabled(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """When MQTT is disabled before subscribe runs, no broker subscription is attempted."""
    lock = zigbee2mqtt_lock_with_device
    mock_subscribe = AsyncMock()
    with (
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
            return_value=False,
        ),
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            mock_subscribe,
        ),
    ):
        lock.setup_push_subscription()
        await hass.async_block_till_done()

    mock_subscribe.assert_not_called()


@pytest.fixture
async def zigbee2mqtt_lock_wrong_identifier(hass: HomeAssistant) -> Zigbee2MQTTLock:
    """Lock device that is MQTT but not recognized as Zigbee2MQTT."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "other_bridge_123")},
        name="SomeLock",
    )

    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "mqtt",
        "test_z2m_other",
        config_entry=mqtt_entry,
        device_id=device.id,
    )

    return Zigbee2MQTTLock(hass, dev_reg, ent_reg, mqtt_entry, lock_entity)


def test_users_enabled_without_pin_key_skips_push() -> None:
    """Do not infer EMPTY when expose_pin hides pin_code (enabled user, key absent)."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload({"users": {"5": {"status": "enabled"}}})
    lock.coordinator.push_update.assert_not_called()


def test_users_enabled_with_numeric_zero_pin_updates() -> None:
    """Numeric zero is a valid digit; it must not be treated as a missing PIN."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"2": {"status": "enabled", "pin_code": 0}}}
    )
    lock.coordinator.push_update.assert_called_once_with({2: "0"})


def test_users_enabled_pin_null_clears_slot() -> None:
    """Explicit null pin in MQTT means cleared at the device."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"5": {"status": "enabled", "pin_code": None}}}
    )
    lock.coordinator.push_update.assert_called_once_with({5: SlotCode.EMPTY})


def test_users_non_numeric_slot_key_skipped() -> None:
    """Invalid user slot keys in ``users`` are ignored."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"bad": {"status": "enabled", "pin_code": "1"}}}
    )
    lock.coordinator.push_update.assert_not_called()


async def test_pin_code_deleted_schedules_refresh_task(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """Z2M action payloads request a coordinator refresh via async task."""
    lock = zigbee2mqtt_lock_with_device
    lock.coordinator = MagicMock()
    lock.coordinator.async_request_refresh = AsyncMock(return_value=None)

    lock._process_z2m_device_payload({"action": "pin_code_deleted", "action_user": 4})
    await hass.async_block_till_done()

    lock.coordinator.async_request_refresh.assert_awaited_once()


def test_pin_code_get_invalid_user_type_returns_early() -> None:
    """Non-numeric ``pin_code.user`` does not touch pending futures."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    fut = Mock(spec=["cancel", "done", "set_result"])
    fut.done.return_value = False
    lock._pending_codes[1] = fut  # type: ignore[assignment]
    lock._process_z2m_device_payload(
        {"pin_code": {"user": "notint", "user_enabled": True, "pin_code": "1"}}
    )
    fut.done.assert_not_called()
    fut.set_result.assert_not_called()


async def test_mqtt_payload_invalid_json_ignored(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """Subscription callback skips invalid JSON payloads."""
    lock = zigbee2mqtt_lock_with_device
    lock.coordinator = MagicMock()
    with (
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
            return_value=True,
        ),
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            new_callable=AsyncMock,
        ) as mock_subscribe,
    ):
        mock_subscribe.return_value = lambda: None
        lock.setup_push_subscription()
        await hass.async_block_till_done()
        cb = mock_subscribe.call_args[0][2]
        cb(SimpleNamespace(payload=b"not json {{{"))
        await hass.async_block_till_done()

    lock.coordinator.push_update.assert_not_called()


class TestPushSubscription:
    """MQTT push subscription lifecycle for Zigbee2MQTTLock."""

    async def test_setup_push_idempotent(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Second setup_push_subscription does not subscribe again."""
        lock = zigbee2mqtt_lock_with_device
        lock.coordinator = MagicMock()

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
                new_callable=AsyncMock,
            ) as mock_async_subscribe,
        ):
            mock_async_subscribe.return_value = lambda: None

            lock.setup_push_subscription()
            await hass.async_block_till_done()
            lock.setup_push_subscription()
            await hass.async_block_till_done()

            mock_async_subscribe.assert_called_once()

    async def test_setup_push_raises_when_no_mqtt_topic(
        self,
        zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
    ) -> None:
        """Without a Z2M-friendly topic, setup raises LockDisconnected."""
        lock = zigbee2mqtt_lock_wrong_identifier
        lock.coordinator = MagicMock()

        with pytest.raises(LockDisconnected):
            lock.setup_push_subscription()


class TestAsyncGetUsercodes:
    """Request/response path for async_get_usercodes via MQTT get + pin_code futures."""

    async def test_wait_for_timeout_maps_slot_to_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """If no pin_code reply arrives in time, that slot is UNREADABLE (transient read failure)."""
        real_wait_for = asyncio.wait_for

        async def fast_pin_timeout(
            awaitable: object, timeout: float | None = None
        ) -> object:
            """Force a real asyncio timeout so the timeout debug branch executes."""
            return await real_wait_for(awaitable, timeout=0.001)

        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={11},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.asyncio.wait_for",
                side_effect=fast_pin_timeout,
            ),
        ):
            result = await lock.async_get_usercodes()

        assert result == {11: SlotCode.UNREADABLE_CODE}

    async def test_publish_failure_maps_slot_to_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT GET publish failure yields UNREADABLE for that slot."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")

        async def boom(*_args: object, **_kwargs: object) -> None:
            raise HomeAssistantError("broker unavailable")

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={7},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=boom,
            ),
        ):
            result = await lock.async_get_usercodes()

        assert result == {7: SlotCode.UNREADABLE_CODE}

    async def test_async_get_usercodes_raises_when_lock_not_connected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """When the lock is not considered connected, get usercodes does not run."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=False),
            ),
            pytest.raises(LockDisconnected, match="Lock not connected"),
        ):
            await lock.async_get_usercodes()

    async def test_async_get_usercodes_raises_when_not_zigbee2mqtt_bridge(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
    ) -> None:
        """MQTT-only locks without a zigbee2mqtt_* device id get an explicit error."""
        lock = zigbee2mqtt_lock_wrong_identifier
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={1},
            ),
            pytest.raises(LockDisconnected, match="not a Zigbee2MQTT lock"),
        ):
            await lock.async_get_usercodes()

    async def test_async_get_usercodes_raises_when_get_topic_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Missing MQTT get topic aborts before publishing PIN queries."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch.object(lock, "_get_topic", return_value=None),
            pytest.raises(LockDisconnected, match="Could not determine MQTT topic"),
        ):
            await lock.async_get_usercodes()

    async def test_async_get_usercodes_raises_when_device_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Unavailable lock entity aborts before publishing PIN queries."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "unavailable")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            pytest.raises(LockDisconnected, match="Device not available"),
        ):
            await lock.async_get_usercodes()


class TestAsyncSetClearHardRefresh:
    """Cover async_set_usercode, async_clear_usercode, mqtt errors, and teardown."""

    async def test_async_get_usercodes_empty_managed_returns_empty_dict(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """No managed slots yields an empty mapping without publishing."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value=set(),
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                new_callable=AsyncMock,
            ) as mock_pub,
        ):
            result = await lock.async_get_usercodes()

        assert result == {}
        mock_pub.assert_not_called()

    async def test_async_get_usercodes_mqtt_disabled_raises(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT integration disabled raises LockDisconnected."""
        lock = zigbee2mqtt_lock_with_device
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected),
        ):
            await lock.async_get_usercodes()

    async def test_async_set_usercode_raises_when_mqtt_disabled(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT integration disabled rejects set before connectivity checks."""
        lock = zigbee2mqtt_lock_with_device
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected, match="MQTT component not available"),
        ):
            await lock.async_set_usercode(1, "1234")

    async def test_async_clear_usercode_raises_when_mqtt_disabled(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT integration disabled rejects clear before connectivity checks."""
        lock = zigbee2mqtt_lock_with_device
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected, match="MQTT component not available"),
        ):
            await lock.async_clear_usercode(5)

    async def test_async_set_usercode_raises_when_not_connected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Disconnected lock raises before publishing a set PIN payload."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=False),
            ),
            pytest.raises(LockDisconnected, match="Lock not connected"),
        ):
            await lock.async_set_usercode(3, "9999")

    async def test_async_clear_usercode_raises_when_not_connected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Disconnected lock raises before publishing a clear PIN payload."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=False),
            ),
            pytest.raises(LockDisconnected, match="Lock not connected"),
        ):
            await lock.async_clear_usercode(9)

    async def test_async_set_usercode_raises_when_not_zigbee2mqtt_bridge(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
    ) -> None:
        """MQTT lock without zigbee2mqtt_* id fails set with the same hint as reads."""
        lock = zigbee2mqtt_lock_wrong_identifier
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            pytest.raises(LockDisconnected, match="not a Zigbee2MQTT lock"),
        ):
            await lock.async_set_usercode(1, "1234")

    async def test_async_set_usercode_raises_when_topic_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """If the MQTT topic cannot be resolved, set PIN fails early."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=True),
            ),
            patch.object(lock, "_get_topic", return_value=None),
            pytest.raises(LockDisconnected, match="Could not determine MQTT topic"),
        ):
            await lock.async_set_usercode(2, "8888")

    async def test_async_clear_usercode_raises_when_topic_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """If the MQTT topic cannot be resolved, clear PIN fails early."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=True),
            ),
            patch.object(lock, "_get_topic", return_value=None),
            pytest.raises(LockDisconnected, match="Could not determine MQTT topic"),
        ):
            await lock.async_clear_usercode(6)

    async def test_async_set_usercode_without_coordinator_still_true(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Set succeeds without coordinator reference."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        lock.coordinator = None
        mock_pub = AsyncMock()
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
        ):
            assert await lock.async_set_usercode(2, "9999") is True

    async def test_async_set_usercode_publish_failure_raises(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Publish errors surface as LockDisconnected."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        mock_pub = AsyncMock(side_effect=OSError("broker"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(LockDisconnected, match="Failed to set PIN"),
        ):
            await lock.async_set_usercode(1, "1111")

    async def test_async_clear_usercode_publish_failure_raises(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Non-MQTT publish failures propagate for visibility (not masked as disconnected)."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        mock_pub = AsyncMock(side_effect=RuntimeError("fail"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(RuntimeError, match="fail"),
        ):
            await lock.async_clear_usercode(4)

    async def test_async_clear_usercode_publish_oserror_raises_lock_disconnected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Clear path maps MQTT publish failures to LockDisconnected."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        mock_pub = AsyncMock(side_effect=OSError("broker"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(LockDisconnected, match="Failed to clear PIN"),
        ):
            await lock.async_clear_usercode(4)

    async def test_async_hard_refresh_delegates_to_get_usercodes(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Hard refresh returns the same data as get_usercodes."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")

        async def fake_publish(
            hass_inner: HomeAssistant, topic: str, payload: str, **kwargs: object
        ) -> None:
            slot = json.loads(payload)["pin_code"]["user"]
            lock._process_z2m_device_payload(
                {
                    "pin_code": {
                        "user": slot,
                        "user_enabled": True,
                        "pin_code": "ABC",
                    }
                }
            )

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={12},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=fake_publish,
            ),
        ):
            refresh = await lock.async_hard_refresh_codes()
            direct = await lock.async_get_usercodes()

        assert refresh == direct == {12: "ABC"}

    async def test_wait_pin_non_timeout_exception_maps_slot_to_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Unexpected errors during wait_for map the slot to UNREADABLE so the coordinator loads."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")

        async def boom(_awaitable: object, _timeout: float | None = None) -> object:
            raise ValueError("unexpected")

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={21},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                new_callable=AsyncMock,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.asyncio.wait_for",
                side_effect=boom,
            ),
        ):
            result = await lock.async_get_usercodes()

        assert result == {21: SlotCode.UNREADABLE_CODE}

    async def test_setup_push_subscribe_failure_leaves_unsub_none(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Failed MQTT subscribe does not record an unsubscribe handle."""
        lock = zigbee2mqtt_lock_with_device
        lock.coordinator = MagicMock()
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
                new=AsyncMock(side_effect=RuntimeError("subscribe failed")),
            ),
        ):
            lock.setup_push_subscription()
            await hass.async_block_till_done()

        assert lock._unsubscribe is None

    async def test_teardown_push_unsubscribes_and_cancels_pending(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Teardown calls MQTT unsubscribe and cancels outstanding futures."""
        lock = zigbee2mqtt_lock_with_device
        unsub = MagicMock()
        lock._unsubscribe = unsub
        fut = asyncio.get_running_loop().create_future()
        lock._pending_codes[3] = fut

        lock.teardown_push_subscription()

        unsub.assert_called_once()
        assert lock._unsubscribe is None
        assert fut.cancelled()
