"""Provider methods, error handling, connectivity, and subscription tests for Zigbee2MQTT."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager
from datetime import timedelta
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.lock_code_manager.domain.credentials import (
    CredentialRef,
    CredentialType,
    WriteResult,
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers import _base as base_module
from custom_components.lock_code_manager.providers._mqtt import BaseMqttLock
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)
from custom_components.lock_code_manager.providers.zwave_js_ui import API_CALL_TIMEOUT
from tests.providers.helpers import ProviderNativeTransportContractTests

from .conftest import Z2M_FULL_TOPIC, _minimal_lock

_PUBLISH = "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish"
_WAIT_FOR = "custom_components.lock_code_manager.providers.zigbee2mqtt.asyncio.wait_for"
# The operational preamble every operation runs first lives on BaseMqttLock,
# so the MQTT-enabled check it makes is bound in that module, not this
# provider's -- which still makes its own for the subscription paths.
MQTT_BASE = "custom_components.lock_code_manager.providers._mqtt"


def _publish_never_leaves() -> AbstractContextManager[Any]:
    """Make every GET fail on publish, the way an unreachable broker does."""

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise HomeAssistantError("broker unavailable")

    return patch(_PUBLISH, side_effect=boom)


def _no_reply_ever_arrives() -> AbstractContextManager[Any]:
    """Let every GET out and answer none of them, the way a silent bridge does."""
    return patch(_WAIT_FOR, new_callable=AsyncMock, side_effect=TimeoutError)


def _answering_publish(
    lock: Zigbee2MQTTLock, codes: dict[int, str]
) -> Callable[..., Coroutine[Any, Any, None]]:
    """
    Stand in for a bridge that answers the GET for the named slots only.

    Zigbee2MQTT replies on the device topic, which is what resolves the
    pending read; a slot left out of ``codes`` is published and never
    answered. The read future exists before the publish is awaited, so
    answering from inside the publish is the same ordering the bridge gets.
    """

    async def _publish(
        hass: HomeAssistant, topic: str, payload: str, **kwargs: object
    ) -> None:
        slot = json.loads(payload)["pin_code"]["user"]
        if slot in codes:
            lock._process_z2m_device_payload(
                {
                    "pin_code": {
                        "user": slot,
                        "user_enabled": True,
                        "pin_code": codes[slot],
                    }
                }
            )

    return _publish


@pytest.mark.skip(
    reason="Zigbee2MQTT's read path deliberately degrades per-slot: a native "
    "OSError on MQTT publish or an asyncio TimeoutError waiting for the bridge "
    "response is caught and the slot marked unreadable, so a transient MQTT "
    "error is not treated as confirmed-empty. Its connection gates raise "
    "LockDisconnected from boolean checks (MQTT enabled / connected / "
    "available), not from a native exception, so the issue #1257 contract does "
    "not apply to a read seam."
)
class TestNativeTransportContract(ProviderNativeTransportContractTests):
    """Documents that the native-transport contract does not apply to Z2M reads."""


def test_zigbee2mqtt_provider_properties_and_no_device_entry_resolves_no_topic() -> (
    None
):
    """Provider metadata and topic resolution when the lock has no device entry."""
    lock = _minimal_lock()
    assert lock.domain == MQTT_DOMAIN
    assert lock.supports_push is True
    # Zigbee2MQTT publishes the used slot on the same device topic the push
    # updates arrive on, so a lock that pushes also reports uses.
    assert lock.supports_code_slot_events is True
    assert lock.usercode_scan_interval == timedelta(minutes=5)
    assert lock.hard_refresh_interval == timedelta(hours=1)
    assert lock.connection_check_interval == timedelta(seconds=30)
    assert lock._resolve_device_topic() is None


async def test_non_z2m_bridge_not_connected(
    zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
) -> None:
    """MQTT devices without a zigbee2mqtt_* identifier resolve no topic and stay disconnected."""
    lock = zigbee2mqtt_lock_wrong_identifier
    assert lock._resolve_device_topic() is None
    with patch(
        "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
        return_value=True,
    ):
        assert await lock.async_is_integration_connected() is False


async def test_async_is_integration_connected_paths(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """MQTT availability and discovery topic resolution gate integration connectivity."""
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
        patch.object(Zigbee2MQTTLock, "_resolve_device_topic", return_value=None),
    ):
        assert await lock.async_is_integration_connected() is False

    with (
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
            return_value=True,
        ),
        patch.object(
            Zigbee2MQTTLock,
            "_resolve_device_topic",
            return_value="zigbee2mqtt/Test Lock",
        ),
    ):
        assert await lock.async_is_integration_connected() is True


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
    zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
) -> None:
    """When MQTT is disabled before subscribe runs, no broker subscription is attempted."""
    lock = zigbee2mqtt_lock_connected
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
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Second setup_push_subscription does not subscribe again."""
        lock = zigbee2mqtt_lock_connected

        lock.setup_push_subscription()
        await hass.async_block_till_done()
        lock.setup_push_subscription()
        await hass.async_block_till_done()

        assert len(lock._push_unsubs) == 1
        assert lock._subscribed_topic == Z2M_FULL_TOPIC

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
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Failed MQTT subscribe does not record an unsubscribe handle."""
        lock = zigbee2mqtt_lock_connected
        lock.coordinator = MagicMock()
        with patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            new=AsyncMock(side_effect=RuntimeError("subscribe failed")),
        ):
            lock.setup_push_subscription()
            await hass.async_block_till_done()

        assert not lock._push_unsubs

    async def test_teardown_push_unsubscribes_and_cancels_pending(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Teardown calls MQTT unsubscribe and cancels outstanding futures."""
        lock = zigbee2mqtt_lock_with_device
        unsub = MagicMock()
        lock._push_unsubs.append(unsub)
        fut = asyncio.get_running_loop().create_future()
        lock._pending_codes[3] = fut

        lock.teardown_push_subscription()

        unsub.assert_called_once()
        assert not lock._push_unsubs
        assert fut.cancelled()

    async def test_ensure_device_subscription_idempotent(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """A second call is a no-op once a subscription is already active."""
        lock = zigbee2mqtt_lock_with_device
        lock._push_unsubs.append(lambda: None)
        with patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            new_callable=AsyncMock,
        ) as mock_subscribe:
            await lock._async_ensure_device_subscription()

        mock_subscribe.assert_not_called()

    async def test_ensure_device_subscription_raises_when_mqtt_disabled(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Subscribing while MQTT is disabled raises LockDisconnected."""
        lock = zigbee2mqtt_lock_with_device
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected, match="MQTT component not available"),
        ):
            await lock._async_ensure_device_subscription()

    async def test_ensure_device_subscription_raises_when_no_topic(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
    ) -> None:
        """Subscribing without a resolvable Zigbee2MQTT topic raises LockDisconnected."""
        lock = zigbee2mqtt_lock_wrong_identifier
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            pytest.raises(LockDisconnected, match="Cannot subscribe for"),
        ):
            await lock._async_ensure_device_subscription()

    async def test_setup_push_subscribe_home_assistant_error_wrapped_and_deferred(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """A HomeAssistantError from async_subscribe is wrapped as LockDisconnected.

        This happens inside the background reconnect task scheduled by
        ``setup_push_subscription``, which must catch the resulting
        LockDisconnected and defer rather than raise (it runs sync and
        cannot propagate an exception to a caller).
        """
        lock = zigbee2mqtt_lock_connected
        lock.coordinator = MagicMock()
        with patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            new=AsyncMock(side_effect=HomeAssistantError("denied")),
        ):
            lock.setup_push_subscription()
            await hass.async_block_till_done()

        assert not lock._push_unsubs


class TestAsyncGetUsers:
    """Request/response path for async_get_users via MQTT get + pin_code futures."""

    async def test_wait_for_timeout_maps_slot_to_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """
        A slot with no reply in time is UNREADABLE, beside one that answered.

        The answered slot is what keeps this a poll rather than a lost
        transport: a lock that drops one request out of several is a weak
        link, and the read still carries data.
        """
        real_wait_for = asyncio.wait_for

        async def fast_pin_timeout(
            awaitable: object, timeout: float | None = None
        ) -> object:
            """Force a real asyncio timeout so the timeout debug branch executes."""
            return await real_wait_for(awaitable, timeout=0.001)

        lock = zigbee2mqtt_lock_connected

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={11, 12},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=_answering_publish(lock, {12: "1234"}),
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.asyncio.wait_for",
                side_effect=fast_pin_timeout,
            ),
        ):
            users = await lock.async_get_users()

        by_slot = {u.user_id: u for u in users}
        assert by_slot[11].pin_credentials[0].state is SlotCredential.unreadable()
        assert by_slot[12].pin_credentials[0].state == SlotCredential.known("1234")

    async def test_a_scoped_read_asks_about_only_the_slots_asked_for(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """
        One request per index, so the scope is what makes allocation cheap.

        Reading past the scope is not wrong, only slow, which is exactly why
        nothing else would catch it. Allocation widens a few numbers at a
        time so a lock is asked about a handful of indices, not its range.
        """
        lock = zigbee2mqtt_lock_connected

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={1, 2, 3, 11},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=_answering_publish(lock, {3: "1234"}),
            ) as publish,
        ):
            await lock.async_get_users(slots={3})

        asked = "".join(str(call) for call in publish.call_args_list)
        assert '"user": 3' in asked or "'user': 3" in asked
        for absent in (1, 2, 11):
            assert f'"user": {absent}' not in asked
            assert f"'user': {absent}" not in asked

    async def test_publish_failure_maps_slot_to_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """A GET that never left maps that slot to UNREADABLE, not to empty."""
        lock = zigbee2mqtt_lock_connected
        answer = _answering_publish(lock, {8: "1234"})

        async def boom(
            hass_inner: HomeAssistant, topic: str, payload: str, **kwargs: object
        ) -> None:
            if json.loads(payload)["pin_code"]["user"] == 7:
                raise HomeAssistantError("broker unavailable")
            await answer(hass_inner, topic, payload, **kwargs)

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={7, 8},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=boom,
            ),
        ):
            users = await lock.async_get_users()

        by_slot = {u.user_id: u for u in users}
        assert by_slot[7].pin_credentials[0].state is SlotCredential.unreadable()
        assert by_slot[8].pin_credentials[0].state == SlotCredential.known("1234")

    @pytest.mark.parametrize(
        "silence",
        [
            pytest.param(_publish_never_leaves, id="no_get_left_home_assistant"),
            pytest.param(_no_reply_ever_arrives, id="no_reply_came_back"),
        ],
    )
    async def test_every_slot_silent_disconnects_rather_than_reporting_data(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
        silence: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        """
        A read where nothing was learned is not a successful poll.

        Returning all-unreadable looks like data to the coordinator: it
        resets the connectivity breaker, un-suspends every slot, and lets
        them re-fail on the next tick, oscillating on a seconds-scale loop
        that flips every slot in and out of sync. A broker that has stopped
        carrying traffic reaches here with the MQTT integration still
        enabled, so every gate above this read answers yes. Raising leaves
        the lock unreachable so its backoff governs the next attempt.
        """
        lock = zigbee2mqtt_lock_connected
        managed = patch(
            "custom_components.lock_code_manager.providers._base.get_managed_slots",
            return_value={1, 2},
        )

        # The rule only arms once this bridge has proven it answers reads at
        # all, so the outage has to be preceded by a poll that worked.
        with (
            managed,
            patch(
                _PUBLISH, side_effect=_answering_publish(lock, {1: "1234", 2: "5678"})
            ),
        ):
            await lock.async_get_users()
        with (
            managed,
            silence(),
            pytest.raises(LockDisconnected, match="every one of the 2"),
        ):
            await lock.async_get_users()

    @pytest.mark.parametrize(
        "silence",
        [
            pytest.param(_publish_never_leaves, id="no_get_left_home_assistant"),
            pytest.param(_no_reply_ever_arrives, id="no_reply_came_back"),
        ],
    )
    async def test_a_lone_slot_losing_its_reply_is_not_a_dead_transport(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
        silence: Callable[[], AbstractContextManager[Any]],
    ) -> None:
        """
        One slot asked about and one reply lost is noise, not an outage.

        The all-silent rule needs at least two reads to say anything: with a
        single managed slot it fires on the first routine drop, so an entry
        with one user would have its breaker tripped by a network an entry
        with two users rides out untroubled.
        """
        lock = zigbee2mqtt_lock_connected

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={1},
            ),
            silence(),
        ):
            users = await lock.async_get_users()

        assert [user.user_id for user in users] == [1]
        assert users[0].pin_credentials[0].state is SlotCredential.unreadable()

    async def test_every_slot_masked_is_a_healthy_poll(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """
        A lock that withholds every code is answering, not unreachable.

        This is an ordinary ``expose_pin`` configuration, not a fault, so the
        poll has to succeed even though every slot comes back unreadable --
        the very shape the all-silent read raises on. Disconnecting here
        would make such a lock permanently offline no matter how healthy the
        network is.
        """
        lock = zigbee2mqtt_lock_connected

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={1, 2},
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                side_effect=_answering_publish(lock, {1: "****", 2: "****"}),
            ),
        ):
            users = await lock.async_get_users()

        assert [user.user_id for user in users] == [1, 2]
        assert all(
            user.pin_credentials[0].state is SlotCredential.unreadable()
            for user in users
        )

    async def test_async_get_users_raises_when_lock_not_connected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """When the lock is not considered connected, get users does not run."""
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
            await lock.async_get_users()

    async def test_async_get_users_raises_when_not_zigbee2mqtt_bridge(
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
            await lock.async_get_users()

    async def test_async_get_users_raises_when_get_topic_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Missing MQTT get topic aborts before publishing PIN queries."""
        lock = zigbee2mqtt_lock_connected
        with (
            patch.object(lock, "_get_topic", return_value=None),
            pytest.raises(LockDisconnected, match="Could not determine MQTT topic"),
        ):
            await lock.async_get_users()

    async def test_async_get_users_raises_when_device_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Unavailable lock entity aborts before publishing PIN queries."""
        lock = zigbee2mqtt_lock_connected
        hass.states.async_set(lock.lock.entity_id, "unavailable")
        with pytest.raises(LockDisconnected, match="Device not available"):
            await lock.async_get_users()

    async def test_wait_pin_non_timeout_exception_maps_slot_to_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Unexpected errors during wait_for map the slot to UNREADABLE so the coordinator loads."""
        lock = zigbee2mqtt_lock_connected

        async def boom(_awaitable: object, _timeout: float | None = None) -> object:
            raise ValueError("unexpected")

        with (
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
            users = await lock.async_get_users()

        by_slot = {u.user_id: u for u in users}
        assert by_slot[21].pin_credentials[0].state is SlotCredential.unreadable()


class TestAsyncSetClearHardRefresh:
    """Cover async_set_credential, async_delete_credential, mqtt errors, and teardown."""

    async def test_async_get_users_empty_managed_returns_empty_list(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """No managed slots yields an empty list without publishing."""
        lock = zigbee2mqtt_lock_connected
        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value=set(),
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                new_callable=AsyncMock,
            ) as mock_pub,
        ):
            result = await lock.async_get_users()

        assert result == []
        mock_pub.assert_not_called()

    async def test_async_get_users_mqtt_disabled_raises(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT integration disabled raises LockDisconnected."""
        lock = zigbee2mqtt_lock_with_device
        with (
            patch(
                f"{MQTT_BASE}.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected),
        ):
            await lock.async_get_users()

    async def test_async_set_credential_raises_when_mqtt_disabled(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT integration disabled rejects set before connectivity checks."""
        lock = zigbee2mqtt_lock_with_device
        credential = credential_from_slot(1, SlotCredential.known("1234"))
        with (
            patch(
                f"{MQTT_BASE}.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected, match="MQTT component not available"),
        ):
            await lock.async_set_credential(
                1, credential, "1234", name=None, source="direct"
            )

    async def test_async_delete_credential_raises_when_mqtt_disabled(
        self,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """MQTT integration disabled rejects clear before connectivity checks."""
        lock = zigbee2mqtt_lock_with_device
        ref = CredentialRef(user_id=5, type=CredentialType.PIN, slot=5)
        with (
            patch(
                f"{MQTT_BASE}.mqtt_config_entry_enabled",
                return_value=False,
            ),
            pytest.raises(LockDisconnected, match="MQTT component not available"),
        ):
            await lock.async_delete_credential(ref)

    async def test_async_set_credential_raises_when_not_connected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Disconnected lock raises before publishing a set PIN payload."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        credential = credential_from_slot(3, SlotCredential.known("9999"))
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
            await lock.async_set_credential(
                3, credential, "9999", name=None, source="direct"
            )

    async def test_async_delete_credential_raises_when_not_connected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Disconnected lock raises before publishing a clear PIN payload."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        ref = CredentialRef(user_id=9, type=CredentialType.PIN, slot=9)
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
            await lock.async_delete_credential(ref)

    async def test_async_set_credential_raises_when_not_zigbee2mqtt_bridge(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
    ) -> None:
        """MQTT lock without zigbee2mqtt_* id fails set with the same hint as reads."""
        lock = zigbee2mqtt_lock_wrong_identifier
        hass.states.async_set(lock.lock.entity_id, "locked")
        credential = credential_from_slot(1, SlotCredential.known("1234"))
        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            pytest.raises(LockDisconnected, match="not a Zigbee2MQTT lock"),
        ):
            await lock.async_set_credential(
                1, credential, "1234", name=None, source="direct"
            )

    async def test_async_set_credential_raises_when_topic_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """If the MQTT topic cannot be resolved, set PIN fails early."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        credential = credential_from_slot(2, SlotCredential.known("8888"))
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
            await lock.async_set_credential(
                2, credential, "8888", name=None, source="direct"
            )

    async def test_async_delete_credential_raises_when_topic_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """If the MQTT topic cannot be resolved, clear PIN fails early."""
        lock = zigbee2mqtt_lock_with_device
        hass.states.async_set(lock.lock.entity_id, "locked")
        ref = CredentialRef(user_id=6, type=CredentialType.PIN, slot=6)
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
            await lock.async_delete_credential(ref)

    async def test_writes_go_out_for_a_lock_whose_entity_is_unavailable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """
        A write does not wait on the device; a read does, and the split is
        deliberate.

        Zigbee2MQTT takes a ``set_pin_code`` for a device that is not
        currently reachable and delivers it when the device next checks in,
        so refusing the publish would drop a code the bridge would have
        carried. A read is answered by the device itself, so an unavailable
        one is refused up front instead of costing a ten-second timeout per
        slot.
        """
        lock = zigbee2mqtt_lock_connected
        hass.states.async_set(lock.lock.entity_id, "unavailable")
        credential = credential_from_slot(4, SlotCredential.known("4321"))
        ref = CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)

        with patch(_PUBLISH, new_callable=AsyncMock) as publish:
            assert (
                await lock.async_set_credential(
                    4, credential, "4321", name=None, source="direct"
                )
                is WriteResult.CONFIRMED
            )
            assert await lock.async_delete_credential(ref) is True

        assert publish.await_count == 2

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value={4},
            ),
            pytest.raises(LockDisconnected, match="Device not available"),
        ):
            await lock.async_get_users()

    async def test_async_set_credential_without_coordinator_still_true(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Set succeeds without coordinator reference."""
        lock = zigbee2mqtt_lock_connected
        lock.coordinator = None
        credential = credential_from_slot(2, SlotCredential.known("9999"))
        mock_pub = AsyncMock()
        with patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
            mock_pub,
        ):
            assert (
                await lock.async_set_credential(
                    2,
                    credential,
                    "9999",
                    name=None,
                    source="direct",
                )
                is WriteResult.CONFIRMED
            )

    async def test_async_set_credential_publish_oserror_raises_lock_disconnected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Set path maps MQTT OSError publish failures to LockDisconnected.

        OSError reflects a broker-reachability problem; routing it to
        LockDisconnected lets the reconnect/backoff path handle recovery
        instead of breaking per-slot.
        """
        lock = zigbee2mqtt_lock_connected
        credential = credential_from_slot(1, SlotCredential.known("1111"))
        mock_pub = AsyncMock(side_effect=OSError("broker"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(LockDisconnected, match="Failed to set PIN"),
        ):
            await lock.async_set_credential(
                1, credential, "1111", name=None, source="direct"
            )

    async def test_async_set_credential_publish_ha_error_raises_operation_failed(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """HomeAssistantError from publish surfaces as LockOperationFailed."""
        lock = zigbee2mqtt_lock_connected
        credential = credential_from_slot(1, SlotCredential.known("1111"))
        mock_pub = AsyncMock(side_effect=HomeAssistantError("payload rejected"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(LockOperationFailed, match="Failed to set PIN"),
        ):
            await lock.async_set_credential(
                1, credential, "1111", name=None, source="direct"
            )

    async def test_async_delete_credential_publish_failure_raises(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Non-MQTT publish failures propagate for visibility (not masked as disconnected)."""
        lock = zigbee2mqtt_lock_connected
        ref = CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)
        mock_pub = AsyncMock(side_effect=RuntimeError("fail"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(RuntimeError, match="fail"),
        ):
            await lock.async_delete_credential(ref)

    async def test_async_delete_credential_publish_oserror_raises_lock_disconnected(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Clear path maps MQTT OSError publish failures to LockDisconnected."""
        lock = zigbee2mqtt_lock_connected
        ref = CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)
        mock_pub = AsyncMock(side_effect=OSError("broker"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(LockDisconnected, match="Failed to clear PIN"),
        ):
            await lock.async_delete_credential(ref)

    async def test_async_delete_credential_publish_ha_error_raises_operation_failed(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """HomeAssistantError from publish surfaces as LockOperationFailed."""
        lock = zigbee2mqtt_lock_connected
        ref = CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)
        mock_pub = AsyncMock(side_effect=HomeAssistantError("payload rejected"))

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
                mock_pub,
            ),
            pytest.raises(LockOperationFailed, match="Failed to clear PIN"),
        ):
            await lock.async_delete_credential(ref)

    async def test_async_hard_refresh_delegates_to_get_usercodes(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """Hard refresh returns the same data as get_usercodes."""
        lock = zigbee2mqtt_lock_connected

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

        assert refresh == direct == {12: SlotCredential.known("ABC")}


class TestScopedRead:
    """A caller-named scope drives the real per-index read, not a stub."""

    async def test_a_timeout_outside_the_managed_slots_is_still_unreadable(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
    ) -> None:
        """The scope reaches slots no entry manages, and its failures behave.

        Driving the real read rather than a stubbed one is the point: nothing
        else proves that a slot the lock never answers about comes back
        unreadable, and an empty answer there is what lets allocation hand out
        an occupied index.
        """
        real_wait_for = asyncio.wait_for

        async def fast_pin_timeout(
            awaitable: object, timeout: float | None = None
        ) -> object:
            return await real_wait_for(awaitable, timeout=0.001)

        lock = zigbee2mqtt_lock_connected

        with (
            patch(
                "custom_components.lock_code_manager.providers._base.get_managed_slots",
                return_value=set(),
            ),
            patch(_PUBLISH, side_effect=_answering_publish(lock, {2: "1234"})),
            patch(_WAIT_FOR, side_effect=fast_pin_timeout),
        ):
            # Nothing is managed, so the default read would ask about nothing.
            assert await lock.async_get_usercodes() == {}
            codes = await lock.async_get_usercodes(range(1, 3))

        assert codes[1] is SlotCredential.unreadable()
        assert codes[2] == SlotCredential.known("1234")


class TestOccupiedIndices:
    """Occupancy reads span the lock, not just the slots this entry manages."""

    async def test_sees_slots_no_entry_manages(
        self, zigbee2mqtt_lock_connected: Zigbee2MQTTLock
    ) -> None:
        """A code programmed by hand holds its index and must be reported.

        A write-only code counts as occupied: the value cannot be read, but
        the index plainly is taken.
        """
        lock = zigbee2mqtt_lock_connected

        async def _read(slot_num: int, get_topic: str) -> SlotCredential | None:
            if slot_num == 4:
                return SlotCredential.known("9999")
            if slot_num == 5:
                return SlotCredential.unreadable()
            return SlotCredential.empty()

        with (
            patch.object(
                lock, "async_is_integration_connected", new=AsyncMock(return_value=True)
            ),
            patch.object(lock, "_get_topic", return_value="topic/get"),
            patch.object(lock, "_async_read_slot", side_effect=_read),
        ):
            codes = await lock.async_get_usercodes(range(1, 6))
            assert codes[4].is_present
            assert codes[5] is SlotCredential.unreadable()

    async def test_stops_at_the_limit(
        self, zigbee2mqtt_lock_connected: Zigbee2MQTTLock
    ) -> None:
        """The bound is what keeps a per-index lock from being walked end to end."""
        lock = zigbee2mqtt_lock_connected
        read = AsyncMock(return_value=SlotCredential.empty())

        with (
            patch.object(
                lock, "async_is_integration_connected", new=AsyncMock(return_value=True)
            ),
            patch.object(lock, "_get_topic", return_value="topic/get"),
            patch.object(lock, "_async_read_slot", read),
        ):
            codes = await lock.async_get_usercodes(range(1, 4))
            assert all(credential.is_empty for credential in codes.values())

        assert read.await_count == 3

    async def test_an_unanswered_index_is_not_reported_empty(
        self, zigbee2mqtt_lock_connected: Zigbee2MQTTLock
    ) -> None:
        """Calling an unanswered index empty is what overwrites codes."""
        lock = zigbee2mqtt_lock_connected

        async def _read(slot_num: int, get_topic: str) -> SlotCredential:
            return (
                SlotCredential.unreadable() if slot_num == 2 else SlotCredential.empty()
            )

        with (
            patch.object(
                lock, "async_is_integration_connected", new=AsyncMock(return_value=True)
            ),
            patch.object(lock, "_get_topic", return_value="topic/get"),
            patch.object(lock, "_async_read_slot", side_effect=_read),
        ):
            codes = await lock.async_get_usercodes(range(1, 4))
            assert codes[2] is SlotCredential.unreadable()
            assert codes[1].is_empty and codes[3].is_empty

    async def test_raises_when_disconnected_rather_than_answering_empty(
        self, zigbee2mqtt_lock_connected: Zigbee2MQTTLock
    ) -> None:
        """A lock that cannot be reached must not answer at all.

        Raising is what makes occupancy read as unknown; answering with
        nothing would read as an empty lock.
        """
        lock = zigbee2mqtt_lock_connected
        with (
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=False),
            ),
            pytest.raises(LockDisconnected),
        ):
            await lock.async_get_usercodes(range(1, 4))

    async def test_raises_without_a_get_topic(
        self, zigbee2mqtt_lock_connected: Zigbee2MQTTLock
    ) -> None:
        """No topic means no question can be asked, which is not an empty lock."""
        lock = zigbee2mqtt_lock_connected
        with (
            patch.object(
                lock,
                "async_is_integration_connected",
                new=AsyncMock(return_value=True),
            ),
            patch.object(lock, "_get_topic", return_value=None),
            pytest.raises(LockDisconnected),
        ):
            await lock.async_get_usercodes(range(1, 4))


async def test_max_slot_is_the_integrations_limit(
    zigbee2mqtt_lock_connected: Zigbee2MQTTLock,
) -> None:
    """Nothing here asks the bridge, so this lock has no opinion.

    This is the provider where the limit costs the most -- one round trip
    per index -- so it is the one most worth teaching to read the bridge's
    device definition later.
    """
    assert await zigbee2mqtt_lock_connected.async_get_max_slot() is None


def test_mqtt_bridges_declare_per_exchange_budgets_above_their_own_bounds(
    z2m_lock: Zigbee2MQTTLock,
) -> None:
    """
    Each bridge's exchange budget sits strictly above the bound it puts on a read itself.

    zigbee2mqtt gives a slot read 10s and zwave-js-ui gives an API call 60s
    before calling it silent. The outer deadline must never be the one to
    claim that silence: an inner bound that fires first turns one silent slot
    into an unreadable slot, the outer one would turn it into a whole-poll
    disconnect. The flat floor is unchanged; the walk scales it at the call
    site (#1528).
    """
    assert Zigbee2MQTTLock.per_exchange_budget > 10.0
    assert BaseMqttLock.per_exchange_budget > API_CALL_TIMEOUT
    assert Zigbee2MQTTLock.per_exchange_budget < BaseMqttLock.per_exchange_budget
    with patch.object(
        type(z2m_lock),
        "managed_slots",
        property(lambda _self: frozenset(range(1, 200))),
    ):
        assert z2m_lock.operation_timeout_seconds == base_module.OPERATION_TIMEOUT
        assert (
            z2m_lock._operation_budget(199) == 199 * Zigbee2MQTTLock.per_exchange_budget
        )
