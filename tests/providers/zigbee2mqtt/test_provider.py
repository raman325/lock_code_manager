"""Provider methods, error handling, connectivity, and subscription tests for Zigbee2MQTT."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)

from .conftest import _minimal_lock


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
