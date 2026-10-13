"""Self-tests: the Zigbee2MQTT bridge fake answers as Zigbee2MQTT does."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from homeassistant.components.mqtt import async_publish, async_subscribe
from homeassistant.core import HomeAssistant, callback

from .conftest import Z2M_FULL_TOPIC, Z2M_GET_TOPIC, Z2M_SET_TOPIC
from .fake_bridge import AVAILABLE, DISABLED, ENABLED, FakeZ2MBridge


async def _exchange(
    hass: HomeAssistant,
    topic: str | None = None,
    body: dict[str, Any] | None = None,
    *,
    act: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Publish ``body`` to ``topic`` or run ``act``; return what the device topic carried."""
    received: list[dict[str, Any]] = []

    @callback
    def _record(msg: Any) -> None:
        received.append(json.loads(msg.payload))

    unsub = await async_subscribe(hass, Z2M_FULL_TOPIC, _record)
    if topic is not None:
        await async_publish(hass, topic, json.dumps(body))
    if act is not None:
        act()
    await hass.async_block_till_done()
    unsub()
    return received


async def test_a_set_is_applied_and_publishes_nothing(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """TZ:442-454: setPinCode stores the code enabled; the converter returns no state."""
    received = await _exchange(
        hass, Z2M_SET_TOPIC, {"pin_code": {"user": 3, "pin_code": "1234"}}
    )

    assert received == []
    assert fake_z2m_bridge.users[3] == (ENABLED, "1234")


async def test_a_set_with_the_user_disabled_stores_it_disabled(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """TZ:449: ``user_enabled`` false is userstatus 3."""
    await _exchange(
        hass,
        Z2M_SET_TOPIC,
        {"pin_code": {"user": 3, "pin_code": "1234", "user_enabled": False}},
    )

    assert fake_z2m_bridge.users[3] == (DISABLED, "1234")


async def test_a_set_without_a_code_clears_the_user(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """TZ:442-443: no ``pin_code`` is clearPinCode."""
    fake_z2m_bridge.set_user(3, "1234")

    await _exchange(
        hass,
        Z2M_SET_TOPIC,
        {"pin_code": {"user": 3, "user_enabled": False, "pin_code": None}},
    )

    assert fake_z2m_bridge.users[3] == (AVAILABLE, None)


async def test_a_user_out_of_range_is_refused_quietly(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """TZ:440, 471: outside 0..pinCodeCount-1 nothing reaches the lock or the topic."""
    received = await _exchange(
        hass, Z2M_GET_TOPIC, {"pin_code": {"user": fake_z2m_bridge.pin_code_count}}
    )
    received += await _exchange(
        hass,
        Z2M_SET_TOPIC,
        {"pin_code": {"user": fake_z2m_bridge.pin_code_count, "pin_code": "1"}},
    )

    assert received == []
    assert fake_z2m_bridge.users == {}


async def test_a_read_is_answered_in_users_with_the_whole_cache(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """FZ:380-400 and Z2M controller.ts:382-391: the answer is the cached state."""
    fake_z2m_bridge.set_user(3, "1234")

    received = await _exchange(hass, Z2M_GET_TOPIC, {"pin_code": {"user": 3}})

    assert received == [
        {"state": "LOCKED", "users": {"3": {"status": "enabled", "pin_code": "1234"}}}
    ]


async def test_a_code_is_left_out_unless_exposed(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """FZ:395: ``pin_code`` only with ``expose_pin`` (off by default in Zigbee2MQTT)."""
    fake_z2m_bridge.expose_pin = False
    fake_z2m_bridge.set_user(3, "1234")

    received = await _exchange(hass, Z2M_GET_TOPIC, {"pin_code": {"user": 3}})

    assert received[-1]["users"] == {"3": {"status": "enabled"}}


async def test_a_new_answer_merges_into_the_old_entry(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """Z2M state.ts:115-126: a key the new answer leaves out keeps its cached value."""
    fake_z2m_bridge.set_user(3, "1234")
    await _exchange(hass, Z2M_GET_TOPIC, {"pin_code": {"user": 3}})
    fake_z2m_bridge.set_user(3, None)

    received = await _exchange(hass, Z2M_GET_TOPIC, {"pin_code": {"user": 3}})

    assert received[-1]["users"]["3"] == {"status": "available", "pin_code": "1234"}


async def test_a_report_republishes_users_as_last_read(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """#1335: the cache restates a users entry the lock has since changed, after the write."""
    await _exchange(hass, Z2M_GET_TOPIC, {"pin_code": {"user": 3}})
    fake_z2m_bridge.report_after_write = True

    received = await _exchange(
        hass, Z2M_SET_TOPIC, {"pin_code": {"user": 3, "pin_code": "1234"}}
    )

    assert received == [
        {"state": "LOCKED", "users": {"3": {"status": "available"}}, "battery": 90}
    ]


async def test_actions_ride_along_and_are_never_cached(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """Z2M state.ts:10-28: ``action`` and ``action_*`` are on their message only."""
    received = await _exchange(
        hass, act=lambda: fake_z2m_bridge.operation_event("unlock", 3)
    )
    received += await _exchange(hass, act=fake_z2m_bridge.report)

    assert received[0]["action"] == "unlock"
    assert received[0]["action_user"] == 3
    assert received[0]["action_source_name"] == "keypad"
    assert "action" not in received[1]


async def test_a_silent_bridge_answers_no_read_and_still_writes(
    hass: HomeAssistant, fake_z2m_bridge: FakeZ2MBridge
) -> None:
    """``silent``: the read is recorded and never answered; a write lands."""
    fake_z2m_bridge.silent = True

    received = await _exchange(hass, Z2M_GET_TOPIC, {"pin_code": {"user": 3}})
    received += await _exchange(
        hass, Z2M_SET_TOPIC, {"pin_code": {"user": 3, "pin_code": "1234"}}
    )

    assert received == []
    assert fake_z2m_bridge.count("get") == 1
    assert fake_z2m_bridge.users[3] == (ENABLED, "1234")
