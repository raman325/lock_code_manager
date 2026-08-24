"""Tests for zwave-js-ui device identifier and topic resolution."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.components.mqtt import (
    DOMAIN as MQTT_DOMAIN,
    debug_info as mqtt_debug_info,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.providers.zwave_js_ui import (
    ZWaveJSUILock,
    parse_zwave_js_ui_identifier,
)
from tests.common import async_discover_unclaimed_mqtt_lock

from .conftest import (
    ZUI_HOME_HEX,
    ZUI_NODE_ID,
    ZUI_NODE_TOPIC,
    ZUI_STATE_TOPIC,
    _minimal_lock,
    async_discover_zui_lock,
    build_zui_lock,
)


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("zwavejs2mqtt_0xd4ee5a7a_node20", ("0xd4ee5a7a", 20)),
        ("zwavejs2mqtt_0xABCD1234_node1", ("0xabcd1234", 1)),
        # UID_DISCOVERY_PREFIX is an environment variable on the gateway
        # (Gateway.ts) and only defaults to ``zwavejs2mqtt_``. Anchoring on
        # that default would reject every lock behind a renamed gateway, at
        # the config flow, with nothing to debug from.
        ("myzwave_0xd4ee5a7a_node20", ("0xd4ee5a7a", 20)),
        ("0xd4ee5a7a_node20", ("0xd4ee5a7a", 20)),
        ("zigbee2mqtt_0xc0ffee", None),
        ("zwavejs2mqtt_0xd4ee5a7a", None),
        ("zwavejs2mqtt_d4ee5a7a_node20", None),
        ("zwavejs2mqtt_0xd4ee5a7a_node20_extra", None),
        # The loosened head is why the tail has to carry the whole burden of
        # not over-claiming: these are the shapes a foreign bridge produces.
        ("somebridge_1", None),
        ("_node5", None),
        ("somebridge_node5", None),
    ],
)
def test_parse_zwave_js_ui_identifier(identifier, expected):
    """Home hex is normalized to lowercase; malformed identifiers parse to None."""
    assert parse_zwave_js_ui_identifier(identifier) == expected


async def test_a_custom_discovery_prefix_lock_knows_its_node(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """
    A gateway with a renamed UID_DISCOVERY_PREFIX is addressable end to end.

    The prefix reaches the device registry, the entity's unique id, and the
    discovery payload alike, so pinning it on the parse function alone would
    leave the provider free to have hard-coded the default somewhere else.
    """
    entity = await async_discover_zui_lock(hass, uid_prefix="myzwave_")
    lock = build_zui_lock(hass, entity)

    assert lock._parsed_identifier() == (ZUI_HOME_HEX, ZUI_NODE_ID)
    assert lock._prefix_and_node_topic() == ("zwave", ZUI_NODE_TOPIC)
    assert await lock.async_is_integration_connected() is True


async def test_debug_info_shape_pin(
    hass: HomeAssistant, zui_lock_discovered: er.RegistryEntry
) -> None:
    """
    Pin the shape of mqtt.debug_info.info_for_device that the provider relies on.

    The provider reads entities[*].entity_id and
    entities[*].discovery_data.payload.state_topic. If this test fails after a
    Home Assistant bump, _resolve_state_topic() needs updating in the same
    commit -- and the hand-built payloads further down this file, which assume
    this shape, stop meaning anything without it.
    """
    device_id = zui_lock_discovered.device_id
    assert device_id is not None
    info = mqtt_debug_info.info_for_device(hass, device_id)
    entry = next(
        e for e in info["entities"] if e["entity_id"] == zui_lock_discovered.entity_id
    )
    assert entry["discovery_data"]["payload"]["state_topic"] == ZUI_STATE_TOPIC


async def test_lcm_builds_a_zwave_js_ui_lock_that_knows_its_node(
    zui_lock: ZWaveJSUILock,
) -> None:
    """
    A discovered lock reaches LCM as an mqtt provider that can name its node.

    Everything this provider does is addressed by the home id and node id it
    recovers here, so this is the first thing that has to hold once a real
    config entry owns the lock.
    """
    assert zui_lock.domain == MQTT_DOMAIN
    assert zui_lock._parsed_identifier() == (ZUI_HOME_HEX, ZUI_NODE_ID)


def test_parsed_identifier_without_device_entry() -> None:
    """A lock entity with no device row has no identifier to parse."""
    assert _minimal_lock()._parsed_identifier() is None


@pytest.mark.parametrize(
    ("payload_kwargs", "expected"),
    [
        pytest.param({}, ("zwave", ZUI_NODE_TOPIC), id="valueid"),
        pytest.param(
            {
                "node_segment": "hallway/front_door",
                "value_path": "lock/endpoint_0/currentMode",
            },
            ("zwave", "zwave/hallway/front_door"),
            id="named-with-location",
        ),
        pytest.param(
            {"prefix": "z", "node_segment": "n"}, ("z", "z/n"), id="short-names"
        ),
    ],
)
async def test_prefix_and_node_topic_derivation(
    hass: HomeAssistant,
    mqtt_mock,
    mqtt_teardown,
    payload_kwargs: dict,
    expected: tuple[str, str],
) -> None:
    """
    Every gateway naming scheme yields the same split of the published state topic.

    The prefix and the node topic are both cut out of the topic zwave-js-ui
    itself published, so a location segment, a renamed prefix, or the NAMED
    gateway's word-shaped value path change nothing about the arithmetic.
    """
    entity = await async_discover_zui_lock(hass, **payload_kwargs)
    assert build_zui_lock(hass, entity)._prefix_and_node_topic() == expected


@pytest.mark.parametrize(
    "state_topic",
    [
        pytest.param("zwave/custom_lock_state", id="two-segments"),
        # Five segments is the shortest a gateway can build: prefix, a
        # one-segment node topic, and the three value segments. Four leaves no
        # room for a node topic, so this is a custom topic that happens to end
        # in something value-shaped, not a node this provider can address.
        pytest.param("zwave/98/0/currentMode", id="four-segments"),
    ],
)
async def test_manual_gateway_custom_topic_is_unresolvable(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown, state_topic: str
) -> None:
    """
    A custom topic too short to hold a prefix, a node, and a value path resolves
    to nothing.

    MANUAL gateways let the user point a discovery entry at any topic at all,
    and such a topic carries no node address to recover. Splitting it anyway
    would hand the API client a prefix invented from a user's naming choice.
    """
    entity = await async_discover_zui_lock(hass, state_topic=state_topic)
    lock = build_zui_lock(hass, entity)

    assert lock._resolve_state_topic() == state_topic
    assert lock._prefix_and_node_topic() is None
    assert await lock.async_is_integration_connected() is False


async def test_state_topic_missing_from_discovery(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """
    A discovery payload without a state topic resolves to nothing.

    There is no command_topic fallback on purpose: the command topic addresses
    targetMode, not currentMode, so shape-stripping it would produce a topic
    for a value this lock never reads.
    """
    entity = await async_discover_zui_lock(hass, include_state_topic=False)
    lock = build_zui_lock(hass, entity)

    assert lock._resolve_state_topic() is None
    assert lock._prefix_and_node_topic() is None


def test_state_topic_without_device_id() -> None:
    """A lock entity with no device link has no discovery data to read."""
    assert _minimal_lock()._resolve_state_topic() is None


async def test_state_topic_without_mqtt_debug_info(
    zui_lock_with_device: ZWaveJSUILock,
) -> None:
    """A zwave-js-ui device whose entity never went through discovery resolves nothing."""
    assert zui_lock_with_device._parsed_identifier() == (ZUI_HOME_HEX, ZUI_NODE_ID)
    assert zui_lock_with_device._resolve_state_topic() is None


async def test_discovery_data_for_other_entities_only_resolves_nothing(
    hass: HomeAssistant, zui_lock_discovered: er.RegistryEntry
) -> None:
    """
    A device whose discovery data carries no entry for this entity resolves nothing.

    Callers read None as disconnected, which is the safe reading: the
    alternative is reconstructing a node topic from the device name, and a
    gateway with a location or a renamed node publishes nowhere near it.
    """
    lock = build_zui_lock(hass, zui_lock_discovered)
    info = {"entities": [{"entity_id": "lock.other", "discovery_data": {}}]}

    with patch.object(mqtt_debug_info, "info_for_device", return_value=info):
        assert lock._resolve_state_topic() is None


@pytest.mark.parametrize(
    ("discovery_data_override", "reason"),
    [
        pytest.param({}, "discovery data with no payload at all", id="no-payload"),
        pytest.param(
            {"payload": "zwave/nodeID_20/98/0/currentMode"},
            "a payload that is not a mapping",
            id="payload-not-a-mapping",
        ),
        pytest.param(
            {"payload": {}}, "a payload carrying no state topic", id="payload-empty"
        ),
        pytest.param(
            {"payload": {"state_topic": ""}},
            "an empty state topic",
            id="state-topic-empty",
        ),
    ],
)
async def test_unusable_discovery_payload_resolves_nothing(
    hass: HomeAssistant,
    zui_lock_discovered: er.RegistryEntry,
    discovery_data_override: dict,
    reason: str,
) -> None:
    """
    An entry this entity does own, but whose payload is unusable, resolves nothing.

    Same safe reading as an absent entry: None means disconnected, never a
    topic guessed from the device name.
    """
    lock = build_zui_lock(hass, zui_lock_discovered)
    info = {
        "entities": [
            {
                "entity_id": zui_lock_discovered.entity_id,
                "discovery_data": discovery_data_override,
            }
        ]
    }

    with patch.object(mqtt_debug_info, "info_for_device", return_value=info):
        assert lock._resolve_state_topic() is None


async def test_foreign_bridge_lock_is_not_connected(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """An mqtt lock from another bridge is never claimed as a zwave-js-ui node."""
    entity = await async_discover_unclaimed_mqtt_lock(hass)
    lock = build_zui_lock(hass, entity)

    assert lock._parsed_identifier() is None
    assert await lock.async_is_integration_connected() is False


async def test_integration_connected_requires_mqtt_and_a_node_topic(
    zui_lock_provider: ZWaveJSUILock,
) -> None:
    """Connectivity needs MQTT itself, plus an addressable node behind it."""
    assert await zui_lock_provider.async_is_integration_connected() is True

    with patch(
        "custom_components.lock_code_manager.providers.zwave_js_ui."
        "mqtt_config_entry_enabled",
        return_value=False,
    ):
        assert await zui_lock_provider.async_is_integration_connected() is False


async def test_device_available_reflects_entity_state(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """Physical availability follows the lock entity, not topic resolution."""
    entity_id = zui_lock_provider.lock.entity_id
    assert await zui_lock_provider.async_is_device_available() is True

    hass.states.async_set(entity_id, "unavailable")
    assert await zui_lock_provider.async_is_device_available() is False

    hass.states.async_remove(entity_id)
    assert await zui_lock_provider.async_is_device_available() is False
