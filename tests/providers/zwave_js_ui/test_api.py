"""Tests for zwave-js-ui gateway discovery and the correlated api client."""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.providers.zwave_js_ui import ZWaveJSUILock

from .conftest import (
    ZUI_API_BASE,
    ZUI_GATEWAY_NAME,
    ZUI_HOME_HEX,
    ZUI_NODE_ID,
    ZUI_PREFIX,
    ZWaveJSUIApiResponder,
    _minimal_lock,
    async_start_gateway_resolution,
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
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
    mqtt_mock,
) -> None:
    """A request goes out on ``<api base>/api/<name>/set`` and its answer comes back."""
    zui_api_responder.set_result("sendCommand", {"userIdStatus": 1})

    response = await zui_lock_provider._async_api_call_at(
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
    zui_lock_provider: ZWaveJSUILock,
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

    response = await zui_lock_provider._async_api_call_at(ZUI_API_BASE, "getInfo", [])

    assert response["result"] == "ours"


async def test_unanswered_call_is_disconnected(
    zui_lock_provider: ZWaveJSUILock, zui_api_responder: ZWaveJSUIApiResponder
) -> None:
    """
    A gateway that never answers reads as a broken link, not a failed operation.

    LockDisconnected is what puts the lock back on the reconnect path;
    LockOperationFailed would retry the same write against a dead broker.
    """
    with pytest.raises(LockDisconnected, match="Timed out waiting"):
        await zui_lock_provider._async_api_call_at(ZUI_API_BASE, "getInfo", [])


async def test_explicit_failure_is_an_operation_failure(
    zui_lock_provider: ZWaveJSUILock, zui_api_responder: ZWaveJSUIApiResponder
) -> None:
    """``success: false`` carries the gateway's own message through."""
    zui_api_responder.set_result(
        "sendCommand", None, success=False, message="Node 20 is not alive"
    )

    with pytest.raises(LockOperationFailed, match="Node 20 is not alive"):
        await zui_lock_provider._async_api_call_at(ZUI_API_BASE, "sendCommand", [])


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
    zui_lock_provider: ZWaveJSUILock,
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
        await zui_lock_provider._async_api_call_at(ZUI_API_BASE, "getInfo", [])


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
