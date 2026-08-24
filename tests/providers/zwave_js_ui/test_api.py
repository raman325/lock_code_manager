"""Tests for zwave-js-ui gateway discovery and the correlated api client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields
import json
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock, Mock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.components.mqtt import debug_info as mqtt_debug_info
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.providers import zwave_js_ui
from custom_components.lock_code_manager.providers._base import MIN_OPERATION_DELAY
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    ZWAVE_JS_UI_OPERATION_DELAY,
    ZWaveJSUILock,
)

from .conftest import (
    FAST_API_CALL_TIMEOUT,
    FAST_GATEWAY_LOCAL_TIMEOUT,
    ZUI_API_BASE,
    ZUI_GATEWAY_NAME,
    ZUI_HOME_HEX,
    ZUI_NODE_ID,
    ZUI_PREFIX,
    ZWaveJSUIApiResponder,
    _minimal_lock,
    async_discover_zui_lock,
    async_start_gateway_resolution,
    build_zui_lock,
    fire_zui_gateway_status,
    zui_lock_discovery_payload,
)

ZUI_HOME_ID = int(ZUI_HOME_HEX, 16)
OTHER_GATEWAY_NAME = "ZWAVE_GATEWAY-other"
OTHER_API_BASE = f"{ZUI_PREFIX}/_CLIENTS/{OTHER_GATEWAY_NAME}"
# Door Lock is 98; User Code Command Class is 99.
CC_USER_CODE_ID = 99


def _home_id_handler(
    home_ids: dict[str, int],
) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Build a getInfo handler answering each gateway with its own home id."""

    def _handler(api_base: str, _request: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": {"homeid": home_ids[api_base]}}

    return _handler


@contextmanager
def record_subscribed_topics() -> Iterator[list[str]]:
    """Record every MQTT topic the provider subscribes to inside the block."""
    module = "custom_components.lock_code_manager.providers.zwave_js_ui"
    seen: list[str] = []
    real_subscribe = zwave_js_ui.async_subscribe

    async def _spy(hass: HomeAssistant, topic: str, *args: Any, **kwargs: Any) -> Any:
        seen.append(topic)
        return await real_subscribe(hass, topic, *args, **kwargs)

    with patch(f"{module}.async_subscribe", _spy):
        yield seen


@contextmanager
def record_transport_events(settle_delay: float) -> Iterator[list[str]]:
    """
    Record subscribes, settle waits, and publishes in the order they happen.

    The settle wait is recognized by its exact duration: ``asyncio.sleep`` is
    patched process-wide for the block, so Home Assistant's own sleeps would
    otherwise be indistinguishable from it. ``settle_delay`` therefore has to
    be a value nothing else passes.
    """
    module = "custom_components.lock_code_manager.providers.zwave_js_ui"
    events: list[str] = []
    real_subscribe = zwave_js_ui.async_subscribe
    real_publish = zwave_js_ui.async_publish
    real_sleep = asyncio.sleep

    async def _subscribe(hass: HomeAssistant, topic: str, *args: Any, **kw: Any) -> Any:
        events.append("subscribe")
        return await real_subscribe(hass, topic, *args, **kw)

    async def _publish(hass: HomeAssistant, topic: str, *args: Any, **kw: Any) -> Any:
        events.append("publish")
        return await real_publish(hass, topic, *args, **kw)

    async def _sleep(delay: float, *args: Any, **kw: Any) -> Any:
        if delay == settle_delay:
            events.append("settle")
        return await real_sleep(delay, *args, **kw)

    with (
        patch(f"{module}.async_subscribe", _subscribe),
        patch(f"{module}.async_publish", _publish),
        patch(f"{module}.SUBSCRIBE_SETTLE_DELAY", settle_delay),
        patch("asyncio.sleep", _sleep),
    ):
        yield events


async def test_the_availability_topic_binds_the_gateway_outright(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A discovered lock names its own gateway, so binding costs nothing.

    zwave-js-ui writes its client status topic into the availability list of
    every entity it discovers, which makes the payload this lock arrived on
    an authoritative, per-lock answer. The scan below is a fallback for
    payloads that lack it, and it is strictly worse: it waits out a discovery
    window and, on a broker with two gateways, has to interrogate both.
    """
    lock = zui_lock_provider
    await lock._async_ensure_api_response_subscription()

    with record_subscribed_topics() as topics:
        assert await lock._async_resolve_api_base() == ZUI_API_BASE

    assert topics == []
    assert zui_api_responder.requests == []


async def test_binding_reads_the_availability_list_by_shape_not_by_index(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """
    The gateway's status topic is found among the node's and the driver's.

    All three entries are topics and the gateway decides their order, so an
    implementation that indexed into the list would bind a lock to its own
    node status topic the day that order changes.
    """
    entity = await async_discover_zui_lock(hass)
    lock = build_zui_lock(hass, entity)
    payload = zui_lock_discovery_payload()

    topics = [entry["topic"] for entry in payload["availability"]]
    assert len(topics) == 3
    assert topics.index(f"{ZUI_API_BASE}/status") == 1

    assert lock._gateway_from_availability() == (ZUI_PREFIX, ZUI_API_BASE)


async def test_a_custom_state_topic_still_yields_the_gateway_prefix(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """
    The prefix comes from the availability topic, never from the state topic.

    A MANUAL gateway publishes wherever the user pointed it, so the state
    topic's first segment is that user's naming choice and nothing more.
    Deriving the api base from it would address a prefix no gateway listens
    on -- here, ``attic``.
    """
    entity = await async_discover_zui_lock(hass, state_topic="attic/front_door/state")
    lock = build_zui_lock(hass, entity)

    assert lock._prefix_and_node_topic() is None
    assert lock._gateway_prefix() == ZUI_PREFIX

    await lock._async_ensure_api_response_subscription()
    assert await lock._async_resolve_api_base() == ZUI_API_BASE


async def test_two_gateways_on_one_prefix_bind_per_lock(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """
    Each lock follows its own discovery payload to its own gateway.

    Sharing a prefix is exactly the case the retained-status scan cannot
    settle without interrogating every candidate -- and refuses outright when
    two answer for the same network. The availability entry is per lock, so
    it never has the question to ask.
    """
    ours = build_zui_lock(hass, await async_discover_zui_lock(hass))
    theirs = build_zui_lock(
        hass,
        await async_discover_zui_lock(
            hass, node_id=21, home_hex="0x11111111", gateway_name=OTHER_GATEWAY_NAME
        ),
    )

    for lock in (ours, theirs):
        await lock._async_ensure_api_response_subscription()

    assert await ours._async_resolve_api_base() == ZUI_API_BASE
    assert await theirs._async_resolve_api_base() == OTHER_API_BASE


@pytest.mark.parametrize(
    "availability",
    [
        pytest.param(None, id="absent"),
        pytest.param("zwave/_CLIENTS/ZWAVE_GATEWAY-zui/status", id="not-a-list"),
        pytest.param([], id="empty"),
        pytest.param(
            ["zwave/_CLIENTS/ZWAVE_GATEWAY-zui/status"], id="entry-not-a-dict"
        ),
        pytest.param([{"payload_available": "true"}], id="entry-without-a-topic"),
        pytest.param([{"topic": 5}], id="topic-not-a-string"),
        pytest.param([{"topic": "zwave/nodeID_20/status"}], id="node-status-only"),
        pytest.param(
            [{"topic": "zwave/_CLIENTS/OTHER-thing/status"}], id="non-gateway-client"
        ),
        pytest.param(
            [{"topic": "zwave/_CLIENTS/ZWAVE_GATEWAY-zui/version"}], id="wrong-suffix"
        ),
    ],
)
async def test_an_availability_list_that_names_no_gateway_binds_nothing(
    hass: HomeAssistant,
    zui_lock_discovered: er.RegistryEntry,
    availability: Any,
) -> None:
    """
    Anything but a gateway status topic leaves the fallback to do the work.

    The entry is read from a payload this integration did not write, so every
    shape it can arrive in has to fall through rather than half-parse into a
    topic nothing answers on.
    """
    lock = build_zui_lock(hass, zui_lock_discovered)
    payload = zui_lock_discovery_payload()
    if availability is None:
        payload.pop("availability")
    else:
        payload["availability"] = availability
    info = {
        "entities": [
            {
                "entity_id": zui_lock_discovered.entity_id,
                "discovery_data": {"payload": payload},
            }
        ]
    }

    with patch.object(mqtt_debug_info, "info_for_device", return_value=info):
        assert lock._gateway_from_availability() is None
        # The fallback still has the state topic to work from.
        assert lock._gateway_prefix() == ZUI_PREFIX


async def test_a_single_availability_mapping_is_accepted(
    hass: HomeAssistant, zui_lock_discovered: er.RegistryEntry
) -> None:
    """Home Assistant lets a lone availability entry be a bare mapping."""
    lock = build_zui_lock(hass, zui_lock_discovered)
    payload = zui_lock_discovery_payload()
    payload["availability"] = {"topic": f"{ZUI_API_BASE}/status"}
    info = {
        "entities": [
            {
                "entity_id": zui_lock_discovered.entity_id,
                "discovery_data": {"payload": payload},
            }
        ]
    }

    with patch.object(mqtt_debug_info, "info_for_device", return_value=info):
        assert lock._gateway_from_availability() == (ZUI_PREFIX, ZUI_API_BASE)


async def test_binding_without_discovery_data_falls_through(
    zui_lock_with_device: ZWaveJSUILock,
) -> None:
    """A lock whose entity never went through discovery names no gateway."""
    assert zui_lock_with_device._gateway_from_availability() is None
    assert zui_lock_with_device._gateway_prefix() is None


async def test_a_sole_candidate_is_asked_which_network_it_runs(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """Being the only gateway answering on the prefix earns no exemption."""
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)

    assert await task == ZUI_API_BASE
    assert [base for base, _, _ in zui_api_responder.requests] == [ZUI_API_BASE]


async def test_a_sole_candidate_running_another_network_is_refused(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    The last gateway standing can belong to somebody else's network.

    Offline gateways are dropped at the status, so a user whose own
    controller is down and whose broker is shared with a neighbouring network
    leaves that network's gateway as the only candidate. Binding it writes
    this lock's Personal Identification Numbers into whatever the other
    network calls node 20.
    """
    zui_api_responder.set_result("getInfo", {"homeid": 0x11111111})

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)

    with pytest.raises(LockDisconnected, match=f"{ZUI_HOME_HEX}.*{ZUI_GATEWAY_NAME}"):
        await task

    assert zui_scan_lock_provider._api_base is None


async def test_a_sole_candidate_that_will_not_say_is_refused(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    Silence is not a match either, however local the question is.

    getInfo is assembled from the gateway's own cached driver state and puts
    nothing on the mesh, so a gateway that will not answer it inside the
    local budget is not busy -- it is in no state to be trusted with a write.
    """
    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)

    with pytest.raises(LockDisconnected, match=ZUI_HOME_HEX):
        await task


async def test_resolution_is_cached_after_the_first_window(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    The second call answers from the cache rather than reopening the window.

    Nothing is published this time, so an uncached implementation would find
    no gateway at all and raise instead of returning the same base.
    """
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})
    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    assert await task == ZUI_API_BASE

    assert await zui_scan_lock_provider._async_resolve_api_base() == ZUI_API_BASE


async def test_silent_prefix_is_disconnected(
    zui_scan_lock_provider: ZWaveJSUILock,
) -> None:
    """No gateway status at all means nothing on this prefix can be addressed."""
    await zui_scan_lock_provider._async_ensure_api_response_subscription()

    with pytest.raises(LockDisconnected, match=f"{ZUI_PREFIX}/_CLIENTS"):
        await zui_scan_lock_provider._async_resolve_api_base()


async def test_non_gateway_clients_on_the_prefix_are_ignored(
    hass: HomeAssistant, zui_scan_lock_provider: ZWaveJSUILock
) -> None:
    """
    Only ``ZWAVE_GATEWAY-*`` clients count as gateways.

    ``+`` matches the whole ``_CLIENTS`` level, so the subscription also picks
    up every other client that publishes a status there. Treating one as a
    gateway would point the api client at a topic nothing answers on.
    """
    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass, client="OTHER-thing")

    with pytest.raises(LockDisconnected, match=f"{ZUI_PREFIX}/_CLIENTS"):
        await task


async def test_gateway_whose_retained_status_is_offline_is_never_bound(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A decommissioned gateway's leftover status is not a gateway.

    zwave-js-ui retains the status topic and its last will retains
    ``{"value": false}`` on it, so a client that crashed or was removed leaves
    a status behind indefinitely. Being the only status on the prefix is the
    worst case: it would be bound on the single-candidate path, without even a
    getInfo to expose that nobody is home, and every call would then time out
    into a rediscovery that picks the same corpse again.
    """
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass, online=False)

    with pytest.raises(LockDisconnected, match="No zwave-js-ui gateway"):
        await task

    assert zui_scan_lock_provider._api_base is None
    assert zui_api_responder.requests == []


async def test_a_dead_gateway_is_never_even_asked(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    An offline sibling is dropped at the status, before any probe is spent.

    It cannot be bound even if it happens to sort first, and probing it would
    cost the local budget waiting on a client that is not there.
    """
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME, online=False)
    fire_zui_gateway_status(hass)

    assert await task == ZUI_API_BASE
    assert [base for base, _, _ in zui_api_responder.requests] == [ZUI_API_BASE]


async def test_two_gateways_are_told_apart_by_home_id(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """Both gateways are asked, and the one running this lock's network wins."""
    zui_api_responder.set_handler(
        "getInfo",
        _home_id_handler({ZUI_API_BASE: ZUI_HOME_ID, OTHER_API_BASE: 0x11111111}),
    )

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    assert await task == ZUI_API_BASE
    assert [base for base, _, _ in zui_api_responder.requests] == [
        OTHER_API_BASE,
        ZUI_API_BASE,
    ]


async def test_gateway_that_refuses_getinfo_is_skipped(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A gateway that errors on getInfo is a non-match, not a failed resolution.

    One unhealthy gateway on a shared broker must not make every lock behind
    the healthy one unreachable.
    """

    def _handler(api_base: str, _request: dict[str, Any]) -> dict[str, Any]:
        if api_base == OTHER_API_BASE:
            return {"success": False, "message": "driver not ready"}
        return {"success": True, "result": {"homeid": ZUI_HOME_ID}}

    zui_api_responder.set_handler("getInfo", _handler)

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    assert await task == ZUI_API_BASE


async def test_no_gateway_claiming_the_home_id_is_disconnected(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """When no gateway runs this lock's network, the candidates are named."""
    zui_api_responder.set_handler(
        "getInfo", _home_id_handler({ZUI_API_BASE: 0x22222222, OTHER_API_BASE: 0x11111})
    )

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    with pytest.raises(
        LockDisconnected, match=f"{OTHER_GATEWAY_NAME}, {ZUI_GATEWAY_NAME}"
    ):
        await task


async def test_two_gateways_on_one_home_id_refuse_to_be_tiebroken(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A primary and a secondary controller on one network fail loud.

    Both answer with the same home id and both can write, but only one is the
    controller the user's locks are actually included on. Picking either would
    silently program through a controller nobody asked about.
    """
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    with pytest.raises(
        LockDisconnected, match=f"{OTHER_GATEWAY_NAME}, {ZUI_GATEWAY_NAME}"
    ):
        await task


async def test_lock_without_an_identifier_cannot_be_resolved() -> None:
    """Without a home id there is nothing to disambiguate a gateway against."""
    with pytest.raises(LockDisconnected, match="no zwave-js-ui device identifier"):
        await _minimal_lock()._async_resolve_api_base()


async def test_unresolvable_prefix_cannot_be_resolved(
    zui_lock_with_device: ZWaveJSUILock,
) -> None:
    """A lock with no discovery topic has no prefix to look for gateways under."""
    with pytest.raises(LockDisconnected, match="Cannot resolve gateway prefix"):
        await zui_lock_with_device._async_resolve_api_base()


async def test_api_call_round_trip(
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
    mqtt_mock,
) -> None:
    """A request goes out on ``<api base>/api/<name>/set`` and its answer comes back."""
    zui_api_responder.set_result("sendCommand", {"userIdStatus": 1})

    response = await zui_gateway_resolved._async_api_call_at(
        ZUI_API_BASE, "sendCommand", [{"nodeId": ZUI_NODE_ID}]
    )

    assert response["result"] == {"userIdStatus": 1}
    assert zui_api_responder.requests == [
        (ZUI_API_BASE, "sendCommand", [{"nodeId": ZUI_NODE_ID}])
    ]
    topic, payload = mqtt_mock.async_publish.call_args.args[:2]
    assert topic == f"{ZUI_API_BASE}/api/sendCommand/set"
    # The nonce rides in the request payload precisely so the gateway echoes
    # it back untouched; without it there is nothing to correlate on.
    assert json.loads(payload)["lcmRequestId"]


async def test_responses_for_other_callers_are_ignored(
    hass: HomeAssistant,
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    Only the response echoing this call's nonce resolves it.

    The api response topic is shared by every client of the gateway, so the
    zwave-js-ui UI and other automations publish their own answers onto it.
    The trailing duplicate is the gateway retransmitting an answer already
    delivered, which must not disturb the settled call.
    """
    topic = f"{ZUI_API_BASE}/api/getInfo"

    def _handler(_api_base: str, request: dict[str, Any]) -> dict[str, Any]:
        answer = {"success": True, "result": "ours", "origin": request}
        for payload in (
            "not json at all",
            json.dumps([1, 2, 3]),
            json.dumps({"success": True, "result": "no origin at all"}),
            json.dumps({"success": True, "result": "origin with no id", "origin": {}}),
            json.dumps(
                {
                    "success": True,
                    "result": "somebody else's",
                    "origin": {"lcmRequestId": "not-our-nonce"},
                }
            ),
            json.dumps(answer),
        ):
            async_fire_mqtt_message(hass, topic, payload)
        return answer

    zui_api_responder.set_handler("getInfo", _handler)

    response = await zui_gateway_resolved._async_api_call_at(
        ZUI_API_BASE, "getInfo", []
    )

    assert response["result"] == "ours"
    # The registry is empty again, so a late foreign response is dropped
    # before it is even parsed.
    async_fire_mqtt_message(hass, topic, json.dumps({"success": True}))
    assert zui_gateway_resolved._pending_api_calls == {}


async def test_unanswered_call_is_disconnected(
    zui_gateway_resolved: ZWaveJSUILock, zui_api_responder: ZWaveJSUIApiResponder
) -> None:
    """
    A gateway that never answers reads as a broken link, not a failed operation.

    LockDisconnected is what puts the lock back on the reconnect path;
    LockOperationFailed would retry the same write against a dead broker.
    """
    with pytest.raises(LockDisconnected, match="Timed out waiting"):
        await zui_gateway_resolved._async_api_call_at(ZUI_API_BASE, "getInfo", [])

    # The abandoned call leaves nothing behind to leak.
    assert zui_gateway_resolved._pending_api_calls == {}


async def test_explicit_failure_is_an_operation_failure(
    zui_gateway_resolved: ZWaveJSUILock, zui_api_responder: ZWaveJSUIApiResponder
) -> None:
    """``success: false`` carries the gateway's own message through."""
    zui_api_responder.set_result(
        "sendCommand", None, success=False, message="Node 20 is not alive"
    )

    with pytest.raises(LockOperationFailed, match="Node 20 is not alive"):
        await zui_gateway_resolved._async_api_call_at(ZUI_API_BASE, "sendCommand", [])


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(OSError("broker unreachable"), LockDisconnected, id="network"),
        pytest.param(
            HomeAssistantError("payload rejected"), LockOperationFailed, id="rejected"
        ),
    ],
)
async def test_publish_failures_are_routed_by_kind(
    zui_gateway_resolved: ZWaveJSUILock,
    error: Exception,
    expected: type[Exception],
) -> None:
    """A network-level publish failure disconnects; a rejected one fails the call."""
    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js_ui.async_publish",
            new=AsyncMock(side_effect=error),
        ),
        pytest.raises(expected, match="Failed to publish getInfo"),
    ):
        await zui_gateway_resolved._async_api_call_at(ZUI_API_BASE, "getInfo", [])


async def test_user_code_command_shapes_the_send_command_envelope(
    zui_gateway_resolved: ZWaveJSUILock, zui_api_responder: ZWaveJSUIApiResponder
) -> None:
    """
    User Code CC calls address this node's endpoint 0 and pass the result through.

    The envelope is what zwave-js-ui's sendCommand api expects: a value-id-ish
    header, the driver method name, and that method's own argument list.
    """
    zui_api_responder.set_result("sendCommand", {"userIdStatus": 1, "userCode": "1234"})

    result = await zui_gateway_resolved._async_user_code_command("get", [5])

    assert result == {"userIdStatus": 1, "userCode": "1234"}
    assert zui_api_responder.requests == [
        (
            ZUI_API_BASE,
            "sendCommand",
            [
                {
                    "nodeId": ZUI_NODE_ID,
                    "commandClass": CC_USER_CODE_ID,
                    "endpoint": 0,
                },
                "get",
                [5],
            ],
        )
    ]


async def test_non_api_publishes_pass_through_the_responder(
    hass: HomeAssistant, zui_api_responder: ZWaveJSUIApiResponder
) -> None:
    """
    The responder only answers api requests, leaving other traffic alone.

    This pins the fixture itself: a hook that swallowed ordinary publishes
    would quietly break every test that relies on real MQTT delivery.
    """
    assert zui_api_responder("zwave/nodeID_20/98/0/targetMode/set", "255") is DEFAULT
    assert zui_api_responder(f"{ZUI_API_BASE}/api/getInfo", "{}") is DEFAULT
    assert zui_api_responder(f"{ZUI_API_BASE}/api/getInfo/set", "not json") is DEFAULT
    assert zui_api_responder.requests == []


async def test_gateway_that_never_answers_is_skipped(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A gateway that goes silent on getInfo times out into a non-match.

    Distinct from a gateway that answers an error: nothing comes back at all,
    so the per-gateway timeout is what has to be absorbed. One unresponsive
    gateway must not strand every lock behind a healthy one.
    """

    def _handler(api_base: str, _request: dict[str, Any]) -> dict[str, Any] | None:
        if api_base == OTHER_API_BASE:
            return None
        return {"success": True, "result": {"homeid": ZUI_HOME_ID}}

    zui_api_responder.set_handler("getInfo", _handler)

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    assert await task == ZUI_API_BASE


@contextmanager
def record_wait_budgets() -> Iterator[list[float | None]]:
    """
    Record every ``asyncio.wait_for`` budget spent inside the block.

    Which constant bounds an api call is only observable at the wait itself;
    asserting on the argument the provider hands its own helper would pin the
    plumbing and pass even if the wait ignored it. Home Assistant's own waits
    land in the list too, so callers test for membership of the two provider
    budgets -- which the fixture keeps an order of magnitude apart -- rather
    than for the list's exact contents.
    """
    seen: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _spy(awaitable: Any, timeout: float | None = None) -> Any:
        seen.append(timeout)
        return await real_wait_for(awaitable, timeout)

    with patch.object(asyncio, "wait_for", _spy):
        yield seen


async def test_gateway_probe_waits_on_the_gateway_local_budget(
    hass: HomeAssistant,
    zui_scan_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A silent gateway is abandoned on the local budget, not the mesh one.

    zwave-js-ui answers getInfo out of its own cached driver state without
    touching the mesh, so silence there means the client is gone rather than
    busy. Waiting the mesh-sized budget on each dead candidate would stall
    every lock behind one decommissioned gateway for a minute apiece.
    """
    zui_api_responder.set_handler("getInfo", lambda _api_base, _request: None)

    task = await async_start_gateway_resolution(hass, zui_scan_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    with record_wait_budgets() as budgets, pytest.raises(LockDisconnected):
        await task

    assert FAST_GATEWAY_LOCAL_TIMEOUT in budgets
    assert FAST_API_CALL_TIMEOUT not in budgets


@pytest.mark.parametrize("method", ["get", "getUsersCount"])
async def test_node_commands_wait_on_the_mesh_budget(
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
    method: str,
) -> None:
    """
    Anything sent to the node waits the full mesh budget, ``getUsersCount`` included.

    Its name reads gateway-local but it is a User Code Command Class
    UsersNumberGet addressed to the lock (node-zwave-js
    ``UserCodeCCAPI.getUsersCount``), so it queues behind the same wake
    schedule as a code read and must not be cut short.
    """
    with record_wait_budgets() as budgets, pytest.raises(LockDisconnected):
        await zui_gateway_resolved._async_user_code_command(method, [])

    assert FAST_API_CALL_TIMEOUT in budgets
    assert FAST_GATEWAY_LOCAL_TIMEOUT not in budgets


def test_operations_are_paced_wider_than_the_base_default() -> None:
    """
    This provider paces its own operations wider than BaseLock's default.

    Asserting the delay by waiting one out would cost the suite real seconds
    (and the suite-wide fixture zeroes the delay on every instance anyway),
    so the field default is where the pacing is observable. What it buys:
    every api call lands on zwave-js-ui's single command queue, shared with
    its own UI and every other MQTT client, and a FLiRS lock drains that
    queue at wake speed.
    """
    delay = next(
        f for f in fields(ZWaveJSUILock) if f.name == "_min_operation_delay"
    ).default

    assert delay == ZWAVE_JS_UI_OPERATION_DELAY
    assert delay > MIN_OPERATION_DELAY


async def test_boolean_home_id_never_matches(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown, zui_api_responder
) -> None:
    """
    A gateway answering ``homeid: true`` is a non-match, even on home id 1.

    ``True == 1`` in Python, so on a network whose home id really is 1 a JSON
    boolean would otherwise match and make the choice ambiguous -- turning a
    resolvable lock unreachable. The same trap this module already guards on
    userIdStatus.
    """
    entity = await async_discover_zui_lock(
        hass, home_hex="0x1", include_availability=False
    )
    lock = build_zui_lock(hass, entity)
    zui_api_responder.set_handler(
        "getInfo",
        lambda api_base, _request: {
            "success": True,
            "result": {"homeid": True if api_base == OTHER_API_BASE else 1},
        },
    )

    task = await async_start_gateway_resolution(hass, lock)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    assert await task == ZUI_API_BASE


async def test_api_call_without_a_response_subscription_fails_loud(
    zui_scan_lock_provider: ZWaveJSUILock,
) -> None:
    """
    Calling before the gateway is resolved refuses rather than subscribing.

    Subscribing here and publishing in the same breath is exactly the race
    the persistent subscription exists to avoid: Home Assistant defers the
    wire SUBSCRIBE behind a debouncer, so the answer would arrive before the
    broker knew to route it.
    """
    with pytest.raises(LockDisconnected, match="No zwave-js-ui api response"):
        await zui_scan_lock_provider._async_api_call_at(ZUI_API_BASE, "getInfo", [])


async def test_subscribe_refusal_is_a_disconnect(
    zui_scan_lock_provider: ZWaveJSUILock,
) -> None:
    """
    MQTT refusing a subscription routes to the reconnect path.

    Home Assistant raises HomeAssistantError when MQTT is unloaded, reloading,
    or disabled, and the BaseLock contract forbids that escaping a provider.
    """
    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js_ui.async_subscribe",
            new=AsyncMock(side_effect=HomeAssistantError("mqtt not set up")),
        ),
        pytest.raises(LockDisconnected, match="Failed to subscribe"),
    ):
        await zui_scan_lock_provider._async_resolve_api_base()


async def test_disconnect_drops_the_cached_gateway(
    hass: HomeAssistant,
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A disconnected call invalidates the cached base so the next one rediscovers.

    Without this a gateway that was renamed or replaced stays cached and every
    later call publishes at a topic nobody answers, until the integration is
    reloaded by hand.
    """
    lock = zui_gateway_resolved
    assert lock._api_base == ZUI_API_BASE

    with pytest.raises(LockDisconnected, match="Timed out waiting"):
        await lock._async_api_call("getInfo", [])
    assert lock._api_base is None

    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})
    task = asyncio.create_task(lock._async_api_call("getInfo", []))
    await hass.async_block_till_done()
    fire_zui_gateway_status(hass)

    assert (await task)["result"] == {"homeid": ZUI_HOME_ID}
    assert lock._api_base == ZUI_API_BASE


async def test_teardown_releases_the_response_subscription(
    hass: HomeAssistant, zui_gateway_resolved: ZWaveJSUILock
) -> None:
    """
    Teardown drops the subscription, the cached base, and every waiting call.

    The base goes with the subscription because a reconnect can bring back a
    renamed gateway or a different broker entirely.
    """
    lock = zui_gateway_resolved
    pending = asyncio.create_task(lock._async_api_call_at(ZUI_API_BASE, "getInfo", []))
    await hass.async_block_till_done()
    assert lock._pending_api_calls

    lock.teardown_push_subscription()

    assert lock._api_base is None
    assert lock._api_response_topic is None
    assert lock._api_response_unsub is None
    assert lock._pending_api_calls == {}
    # A call whose transport is pulled out from under it lost its connection,
    # and says so in the provider's own vocabulary. Letting the CancelledError
    # out instead would reach the coordinator as an unhandled task
    # cancellation, past everything that knows how to handle a disconnect.
    with pytest.raises(LockDisconnected, match="transport released"):
        await pending

    # Idempotent, as the base class requires.
    lock.teardown_push_subscription()


async def test_cancelling_the_caller_is_never_converted(
    hass: HomeAssistant, zui_gateway_resolved: ZWaveJSUILock
) -> None:
    """
    A task being torn down stays cancelled, however it was waiting.

    A released transport and a shutdown both arrive as a CancelledError while
    waiting for the reply, and swallowing the second would stall whatever
    teardown asked for it.

    The cancellation is timed off the publish returning, because that is the
    only moment the call is provably waiting on a reply: cancelled any
    earlier it is still inside ``async_publish``, where nothing of this
    reaches and the test would pass on a code path it never ran.
    """
    lock = zui_gateway_resolved
    published = asyncio.Event()
    real_publish = zwave_js_ui.async_publish

    async def _publish(*args: Any, **kwargs: Any) -> None:
        await real_publish(*args, **kwargs)
        published.set()

    with patch.object(zwave_js_ui, "async_publish", _publish):
        pending = asyncio.create_task(
            lock._async_api_call_at(ZUI_API_BASE, "getInfo", [])
        )
        await published.wait()

        pending.cancel()

        with pytest.raises(asyncio.CancelledError):
            await pending


async def test_the_response_subscription_is_established_by_setup(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """
    Setup is what brings the api response subscription up, before anything publishes.

    Home Assistant registers a subscription locally at once but defers the
    wire SUBSCRIBE behind a debouncer, while a publish goes out immediately,
    so subscribing and publishing in one breath loses the answer -- and
    zwave-js-ui sends api responses unretained. Setup runs long before the
    coordinator's first poll, which is the settling time. Establishing it
    inside resolution instead used to work only because resolution held a
    multi-second discovery window; the availability fast path has no window,
    so that placement would race again.
    """
    lock = zui_lock_provider
    assert lock._api_response_topic is None

    await lock.async_setup(MagicMock())

    assert lock._api_response_topic == f"{ZUI_PREFIX}/_CLIENTS/+/api/+"
    assert lock._api_response_unsub is not None


async def test_the_response_subscription_is_established_at_most_once(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """
    Setup runs again on every provider reconnect, so it has to be idempotent.

    A second subscription would double every api response, and only the first
    unsub is tracked -- the other would outlive the provider.
    """
    lock = zui_lock_provider
    await lock._async_ensure_api_response_subscription()
    first_unsub = lock._api_response_unsub

    with record_subscribed_topics() as topics:
        await lock._async_ensure_api_response_subscription()

    assert topics == []
    assert lock._api_response_unsub is first_unsub


async def test_no_prefix_yet_defers_the_response_subscription(
    zui_lock_with_device: ZWaveJSUILock,
) -> None:
    """
    A lock whose discovery data has not landed defers rather than guessing.

    There is no prefix to subscribe under, and inventing one from the device
    name would put every api call on a topic no gateway reads. Setup is
    re-run on the provider integration's reconnect, so deferring is enough.
    """
    await zui_lock_with_device._async_ensure_api_response_subscription()

    assert zui_lock_with_device._api_response_topic is None
    assert zui_lock_with_device._api_response_unsub is None


async def test_the_first_call_on_a_lock_that_never_ran_setup_resolves(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A provider whose ``async_setup`` never ran works on its very first call.

    Throwaway instances -- the config flow's allocation reads, the unmanaged
    sweep -- build a provider, make exactly one call, and drop it. Refusing
    that call in the hope that something retries made a zwave-js-ui lock
    impossible to add through the user interface and skipped it in the sweep.
    """
    lock = zui_lock_provider
    assert lock._api_response_topic is None

    assert await lock._async_resolve_api_base() == ZUI_API_BASE
    assert lock._api_response_topic == f"{ZUI_PREFIX}/_CLIENTS/+/api/+"


async def test_a_fresh_subscription_settles_before_anything_is_published(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    The settle wait falls between subscribing and the first publish.

    Home Assistant defers the wire SUBSCRIBE behind a debouncer while a
    publish goes out immediately, and zwave-js-ui answers unretained, so a
    call published in the same breath as its own subscription loses the
    answer. The mocked broker cannot reproduce that -- it delivers to a
    subscription the instant it is registered -- so the ordering is what has
    to be pinned.
    """
    lock = zui_lock_provider
    zui_api_responder.set_result("sendCommand", 30)

    # A duration nothing else in Home Assistant sleeps for, so the settle
    # wait is identifiable among every other sleep in the block.
    with record_transport_events(0.0123) as events:
        assert await lock.async_get_max_slot() == 30

    assert events.index("subscribe") < events.index("settle") < events.index("publish")


async def test_resolution_fails_loud_when_no_subscription_can_be_established(
    zui_lock_provider: ZWaveJSUILock,
) -> None:
    """
    Discovery data vanishing mid-call leaves nothing to hear the answer on.

    The prefix resolved on the way in, so this is not the "has not arrived
    yet" case ``_async_ensure_api_response_subscription`` tolerates.
    Publishing anyway would spend a whole mesh-sized budget on a call nobody
    could deliver a reply for.
    """
    lock = zui_lock_provider

    with (
        patch.object(lock, "_gateway_prefix", side_effect=[ZUI_PREFIX, None]),
        pytest.raises(LockDisconnected, match="could be established"),
    ):
        await lock._async_resolve_api_base()


async def test_a_changed_gateway_prefix_moves_the_api_subscription(
    hass: HomeAssistant,
    mqtt_mock,
    mqtt_teardown,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    An operator who changes the gateway's prefix must not strand the lock.

    Publishes follow the prefix that resolves now, so a subscription kept on
    "is one set" rather than "does it cover this" stays on the old one: every
    call times out, which drops the cached base and re-resolves it to the
    same new prefix, and the guard passes again. That loop sustains itself.
    An api-only lock cannot even fall out of it the way a push lock
    eventually does, because the connection-transition teardown that would
    release the subscription is gated on ``supports_push``.

    The lock here is api-only for exactly that reason: a MANUAL gateway's
    custom state topic carries no node address, so there is no push
    subscription and no teardown to be rescued by.
    """
    manual_topic = "custom/lock/state"
    new_prefix = "zwave2"
    lock = build_zui_lock(
        hass, await async_discover_zui_lock(hass, state_topic=manual_topic)
    )
    assert lock.supports_push is False

    await lock.async_setup(MagicMock())
    assert lock._api_response_topic == f"{ZUI_PREFIX}/_CLIENTS/+/api/+"

    # Working on the original prefix, with the gateway bound and cached.
    zui_api_responder.set_result("sendCommand", 30)
    assert await lock.async_get_max_slot() == 30
    assert lock._api_base == ZUI_API_BASE

    def _only_the_moved_gateway(
        api_base: str, _request: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Answer as a gateway that now lives under the new prefix only."""
        if not api_base.startswith(f"{new_prefix}/"):
            return None
        return {"success": True, "result": 30}

    zui_api_responder.set_handler("sendCommand", _only_the_moved_gateway)

    # The gateway moved and republished its discovery payload.
    await async_discover_zui_lock(hass, prefix=new_prefix, state_topic=manual_topic)

    # The cached base still names the old prefix, where nothing answers now.
    with pytest.raises(LockDisconnected, match="Timed out waiting"):
        await lock.async_get_max_slot()

    # Healed in one attempt: resolution finds the new prefix, moves the
    # subscription, and lets it settle before this same call publishes.
    assert await lock.async_get_max_slot() == 30
    assert lock._api_response_topic == f"{new_prefix}/_CLIENTS/+/api/+"


async def test_a_transiently_unresolvable_prefix_keeps_the_subscription(
    hass: HomeAssistant, zui_gateway_resolved: ZWaveJSUILock
) -> None:
    """
    Discovery data going missing is not a prefix that moved.

    Tearing a working subscription down because a lookup came back empty
    would drop api traffic for a lock that never changed address, and the
    resubscribe would race the next publish.
    """
    lock = zui_gateway_resolved
    settled = lock._api_response_topic
    unsub = lock._api_response_unsub

    with (
        patch.object(lock, "_gateway_prefix", return_value=None),
        record_subscribed_topics() as topics,
    ):
        await lock._async_ensure_api_response_subscription()

    assert topics == []
    assert lock._api_response_topic == settled
    assert lock._api_response_unsub is unsub


async def test_unload_releases_the_api_subscription(
    zui_gateway_resolved: ZWaveJSUILock,
) -> None:
    """
    Unloading releases the api subscription along with the push one.

    The api transport has its own lifetime and is deliberately not in the
    push-unsub registry, so it is only released because teardown reaches for
    it by name. Miss that and every reload orphans a wildcard subscription
    holding a dead provider.
    """
    lock = zui_gateway_resolved
    assert lock._api_response_unsub is not None

    await lock.async_unload(False)

    assert lock._api_response_unsub is None
    assert lock._api_response_topic is None
    assert lock._api_base is None
    # Observable through behaviour, not just fields: calls refuse again.
    with pytest.raises(LockDisconnected, match="No zwave-js-ui api response"):
        await lock._async_api_call_at(ZUI_API_BASE, "getInfo", [])

    # Idempotent, as teardown must be.
    lock._release_api_subscription()


async def test_release_survives_an_unsubscribe_refusal(
    zui_gateway_resolved: ZWaveJSUILock,
) -> None:
    """
    MQTT torn down ahead of us must not abort the rest of the release.

    Home Assistant raises when a subscription is dropped twice, and the
    cached base and pending calls still have to be cleared.
    """
    lock = zui_gateway_resolved
    real_unsub = lock._api_response_unsub
    assert real_unsub is not None
    lock._api_response_unsub = Mock(side_effect=HomeAssistantError("already gone"))

    lock._release_api_subscription()

    assert lock._api_response_topic is None
    assert lock._api_base is None
    real_unsub()
