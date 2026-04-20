"""Tests for the Zigbee2MQTT lock provider."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import Zigbee2MQTTLock

Z2M_TOPIC_NAME = "TestLockZ2M"
Z2M_FULL_TOPIC = f"zigbee2mqtt/{Z2M_TOPIC_NAME}"
Z2M_GET_TOPIC = f"{Z2M_FULL_TOPIC}/get"
Z2M_SET_TOPIC = f"{Z2M_FULL_TOPIC}/set"


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


def test_users_enabled_with_pin_updates() -> None:
    """When pin_code is present, coordinator receives the value."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"5": {"status": "enabled", "pin_code": "4242"}}}
    )
    lock.coordinator.push_update.assert_called_once_with({5: "4242"})


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


def test_non_enabled_user_is_empty() -> None:
    """Disabled or non-enabled statuses clear coordinator slot."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload({"users": {"5": {"status": "disabled"}}})
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

    async def test_subscribes_to_z2m_device_topic(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """setup_push_subscription registers async_subscribe on base_topic/friendly_name."""
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

            mock_async_subscribe.assert_called_once()
            assert mock_async_subscribe.call_args[0][1] == Z2M_FULL_TOPIC

    async def test_mqtt_payload_delivers_push_update_on_event_loop(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Incoming MQTT JSON schedules payload handling and coordinator.push_update."""
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

            msg_callback = mock_async_subscribe.call_args[0][2]
            payload = {"users": {"7": {"status": "enabled", "pin_code": "9090"}}}
            msg = SimpleNamespace(payload=json.dumps(payload).encode())

            msg_callback(msg)
            await hass.async_block_till_done()
            await hass.async_block_till_done()

            lock.coordinator.push_update.assert_called_once_with({7: "9090"})

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

    async def test_publishes_get_payload_for_each_managed_slot(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Each managed slot triggers async_publish on the device get topic."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")

        publishes: list[tuple[str, str]] = []

        async def fake_publish(
            hass_inner: HomeAssistant, topic: str, payload: str, **kwargs: object
        ) -> None:
            publishes.append((topic, payload))
            body = json.loads(payload)
            slot = body["pin_code"]["user"]
            lock._process_z2m_device_payload(
                {
                    "pin_code": {
                        "user": slot,
                        "user_enabled": True,
                        "pin_code": f"PIN{slot}",
                    }
                }
            )

        managed = {4, 8}

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
                return_value=managed,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=fake_publish,
            ),
        ):
            result = await lock.async_get_usercodes()

        assert len(publishes) == 2
        assert all(t == Z2M_GET_TOPIC for t, _ in publishes)
        published_slots = {
            json.loads(payload)["pin_code"]["user"] for _, payload in publishes
        }
        assert published_slots == managed
        assert result == {4: "PIN4", 8: "PIN8"}

    async def test_pin_code_get_response_maps_slot_value(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT pin_code dict on the device topic resolves the pending future."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")

        async def fake_publish(
            hass_inner: HomeAssistant, topic: str, payload: str, **kwargs: object
        ) -> None:
            body = json.loads(payload)
            assert body == {"pin_code": {"user": 3}}
            lock._process_z2m_device_payload(
                {
                    "pin_code": {
                        "user": 3,
                        "user_enabled": True,
                        "pin_code": "7788",
                    }
                }
            )

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
                return_value={3},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=fake_publish,
            ),
        ):
            result = await lock.async_get_usercodes()

        assert result == {3: "7788"}

    async def test_wait_for_timeout_raises_lock_disconnected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """If no pin_code reply arrives in time, the refresh fails (unknown state)."""
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
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
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
            with pytest.raises(LockDisconnected, match="Timed out waiting"):
                await lock.async_get_usercodes()

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
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
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
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
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

    async def test_async_set_usercode_publishes_set_topic_and_push_update(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Successful set publishes JSON and updates coordinator."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        lock.coordinator = MagicMock()
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
            assert await lock.async_set_usercode(6, "4455") is True

        mock_pub.assert_called_once()
        assert mock_pub.call_args[0][1] == Z2M_SET_TOPIC
        body = json.loads(mock_pub.call_args[0][2])
        assert body["pin_code"]["pin_code"] == "4455"
        lock.coordinator.push_update.assert_called_once_with({6: "4455"})

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

    async def test_async_clear_usercode_publishes_clear_payload(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Clear publishes user_disabled payload and EMPTY on coordinator."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        lock.coordinator = MagicMock()
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
            assert await lock.async_clear_usercode(8) is True

        mock_pub.assert_called_once()
        body = json.loads(mock_pub.call_args[0][2])
        assert body["pin_code"]["user_enabled"] is False
        assert body["pin_code"]["pin_code"] is None
        lock.coordinator.push_update.assert_called_once_with({8: SlotCode.EMPTY})

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
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
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

    async def test_wait_pin_non_timeout_exception_raises_lock_disconnected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Unexpected errors during wait_for surface as LockDisconnected."""
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
                "custom_components.lock_code_manager.providers.zigbee2mqtt.get_managed_slots",
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
            pytest.raises(LockDisconnected, match="Failed to read PIN slot 21"),
        ):
            await lock.async_get_usercodes()

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
