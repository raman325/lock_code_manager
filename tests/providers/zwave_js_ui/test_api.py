"""Tests for zwave-js-ui gateway discovery and the correlated api client."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields
import json
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, Mock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
)
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


async def test_single_gateway_resolves_without_asking_it_anything(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    One gateway on the prefix is the answer; no api round trip is spent.

    getInfo exists to break a tie, so a broker running a single gateway must
    resolve even when that gateway is too busy (or too old) to answer it.
    """
    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass)

    assert await task == ZUI_API_BASE
    assert zui_api_responder.requests == []


async def test_resolution_is_cached_after_the_first_window(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """
    The second call answers from the cache rather than reopening the window.

    Nothing is published this time, so an uncached implementation would find
    no gateway at all and raise instead of returning the same base.
    """
    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass)
    assert await task == ZUI_API_BASE

    assert await zui_lock_provider._async_resolve_api_base() == ZUI_API_BASE


async def test_silent_prefix_is_disconnected(zui_lock_provider: ZWaveJSUILock) -> None:
    """No gateway status at all means nothing on this prefix can be addressed."""
    with pytest.raises(LockDisconnected, match=f"{ZUI_PREFIX}/_CLIENTS"):
        await zui_lock_provider._async_resolve_api_base()


async def test_non_gateway_clients_on_the_prefix_are_ignored(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """
    Only ``ZWAVE_GATEWAY-*`` clients count as gateways.

    ``+`` matches the whole ``_CLIENTS`` level, so the subscription also picks
    up every other client that publishes a status there. Treating one as a
    gateway would point the api client at a topic nothing answers on.
    """
    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass, client="OTHER-thing")

    with pytest.raises(LockDisconnected, match=f"{ZUI_PREFIX}/_CLIENTS"):
        await task


async def test_gateway_whose_retained_status_is_offline_is_never_bound(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
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

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass, online=False)

    with pytest.raises(LockDisconnected, match="No zwave-js-ui gateway"):
        await task

    assert zui_lock_provider._api_base is None
    assert zui_api_responder.requests == []


async def test_a_live_gateway_wins_over_a_dead_one_without_a_probe(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    An offline sibling does not make resolution ambiguous.

    Dropping it at the status leaves exactly one candidate, so the lock binds
    on the cheap path rather than paying a getInfo round trip -- and cannot
    bind to the dead one even if it happens to sort first.
    """
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME, online=False)
    fire_zui_gateway_status(hass)

    assert await task == ZUI_API_BASE
    assert zui_api_responder.requests == []


async def test_two_gateways_are_told_apart_by_home_id(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """Both gateways are asked, and the one running this lock's network wins."""
    zui_api_responder.set_handler(
        "getInfo",
        _home_id_handler({ZUI_API_BASE: ZUI_HOME_ID, OTHER_API_BASE: 0x11111111}),
    )

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    assert await task == ZUI_API_BASE
    assert [base for base, _, _ in zui_api_responder.requests] == [
        OTHER_API_BASE,
        ZUI_API_BASE,
    ]


async def test_gateway_that_refuses_getinfo_is_skipped(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
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

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    assert await task == ZUI_API_BASE


async def test_no_gateway_claiming_the_home_id_is_disconnected(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """When no gateway runs this lock's network, the candidates are named."""
    zui_api_responder.set_handler(
        "getInfo", _home_id_handler({ZUI_API_BASE: 0x22222222, OTHER_API_BASE: 0x11111})
    )

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass)
    fire_zui_gateway_status(hass, client=OTHER_GATEWAY_NAME)

    with pytest.raises(
        LockDisconnected, match=f"{OTHER_GATEWAY_NAME}, {ZUI_GATEWAY_NAME}"
    ):
        await task


async def test_two_gateways_on_one_home_id_refuse_to_be_tiebroken(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A primary and a secondary controller on one network fail loud.

    Both answer with the same home id and both can write, but only one is the
    controller the user's locks are actually included on. Picking either would
    silently program through a controller nobody asked about.
    """
    zui_api_responder.set_result("getInfo", {"homeid": ZUI_HOME_ID})

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
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
    zui_lock_provider: ZWaveJSUILock,
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

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
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
    zui_lock_provider: ZWaveJSUILock,
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

    task = await async_start_gateway_resolution(hass, zui_lock_provider)
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
    entity = await async_discover_zui_lock(hass, home_hex="0x1")
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
    zui_lock_provider: ZWaveJSUILock,
) -> None:
    """
    Calling before the gateway is resolved refuses rather than subscribing.

    Subscribing here and publishing in the same breath is exactly the race
    the persistent subscription exists to avoid: Home Assistant defers the
    wire SUBSCRIBE behind a debouncer, so the answer would arrive before the
    broker knew to route it.
    """
    with pytest.raises(LockDisconnected, match="No zwave-js-ui api response"):
        await zui_lock_provider._async_api_call_at(ZUI_API_BASE, "getInfo", [])


async def test_subscribe_refusal_is_a_disconnect(
    zui_lock_provider: ZWaveJSUILock,
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
        await zui_lock_provider._async_resolve_api_base()


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
    with pytest.raises(asyncio.CancelledError):
        await pending

    # Idempotent, as the base class requires.
    lock.teardown_push_subscription()


async def test_response_subscription_predates_the_discovery_window(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """
    The api response subscription is live before the discovery window opens.

    This is the entire reason it lives in resolution rather than in each call.
    Home Assistant registers a subscription locally at once but defers the
    wire SUBSCRIBE behind a debouncer, and the window is the only stretch
    long enough to outlast it. Established after the window -- or per call --
    a publish would go out before the broker had been told to route the
    reply, and zwave-js-ui sends api responses unretained, so it would be
    lost outright.
    """
    lock = zui_lock_provider
    assert lock._api_response_topic is None

    task = await async_start_gateway_resolution(hass, lock)

    # Execution is parked inside the window right now -- the status fired
    # below is collected by it -- and the subscription is already up.
    assert lock._api_response_topic == f"{ZUI_PREFIX}/_CLIENTS/+/api/+"
    assert lock._api_response_unsub is not None

    fire_zui_gateway_status(hass)
    assert await task == ZUI_API_BASE


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
