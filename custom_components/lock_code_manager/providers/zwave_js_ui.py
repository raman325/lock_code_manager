"""Module for locks bridged through zwave-js-ui's MQTT gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import re
from typing import Any
from uuid import uuid4

from zwave_js_server.const import CommandClass

from homeassistant.components.mqtt import (
    DOMAIN as MQTT_DOMAIN,
    async_publish,
    async_subscribe,
    debug_info as mqtt_debug_info,
)
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError

from ..domain.exceptions import LockDisconnected, LockOperationFailed
from ..domain.models import SlotCredential
from ._base import BaseLock
from .const import LOGGER

# HA discovery device identifier published by zwave-js-ui
# (Gateway.ts UID_DISCOVERY_PREFIX): zwavejs2mqtt_<homeHex>_node<id>.
ZWAVE_JS_UI_IDENTIFIER_RE = re.compile(
    r"^zwavejs2mqtt_(?P<home_hex>0x[0-9a-fA-F]+)_node(?P<node_id>\d+)$"
)

CC_USER_CODE = CommandClass.USER_CODE
CC_NOTIFICATION = CommandClass.NOTIFICATION
# zwave-js UserIDStatus enum (UserCodeCC).
USER_ID_STATUS_AVAILABLE = 0
USER_ID_STATUS_ENABLED = 1
API_CALL_TIMEOUT = 10.0
GATEWAY_DISCOVERY_TIMEOUT = 3.0

# Every zwave-js-ui gateway publishes its retained client status under
# ``<prefix>/_CLIENTS/ZWAVE_GATEWAY-<name>/status``. Other clients share the
# ``_CLIENTS`` level, so the gateway prefix is what tells them apart.
GATEWAY_CLIENT_PREFIX = "ZWAVE_GATEWAY-"
# Key added to every api request payload. zwave-js-ui echoes the request
# verbatim under the response's ``origin``, so an extra key of our own comes
# back untouched and correlates the reply.
REQUEST_ID_KEY = "lcmRequestId"

# A zwave-js-ui value topic ends with ``<cc>/<endpoint>/<property>`` and, for
# values that have one, a fourth ``<propertyKey>`` segment. Door Lock state
# values have no propertyKey, so a lock's state topic always ends with exactly
# these three segments and everything before them is the node topic.
VALUE_TOPIC_SEGMENTS = 3
# The shortest topic a gateway can build is ``<prefix>/<node>/<cc>/<endpoint>/
# <property>``: the three value segments plus a prefix and a one-segment node
# topic. Anything shorter was not built by the gateway's naming scheme.
MIN_VALUE_TOPIC_SEGMENTS = VALUE_TOPIC_SEGMENTS + 2


def parse_zwave_js_ui_identifier(identifier: str) -> tuple[str, int] | None:
    """Parse ``(home_hex, node_id)`` out of a zwave-js-ui device identifier."""
    if match := ZWAVE_JS_UI_IDENTIFIER_RE.match(identifier):
        return match["home_hex"].lower(), int(match["node_id"])
    return None


def _unwrap_mqtt_value(raw: bytes | str) -> Any:
    """
    Unwrap a zwave-js-ui value payload to the bare value.

    The gateway's payload-type setting produces one of three shapes on a
    value topic: the raw value, a ``{time, value}`` wrapper, or the entire
    valueId object (which also carries ``value`` among other keys). All dict
    shapes therefore carry the value under ``value``; a payload that is not
    JSON at all IS the raw value (for example a bare PIN string).

    An empty payload is a fourth case and not a value at all: it is how MQTT
    clears a retained message, so it unwraps to None rather than to ``""``.
    """
    if isinstance(raw, bytes):
        raw = raw.decode(errors="replace")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict) and "value" in data:
        return data["value"]
    return data


def _project_user_code_result(result: Any) -> SlotCredential:
    """
    Project a User Code CC ``get`` result to a SlotCredential.

    Only Available means the slot holds nothing. Enabled without a usable
    string code (withheld, or a serialized Buffer for a binary code) is
    occupied-but-unreadable -- reporting it empty would make sync reprogram a
    slot that already holds the right code. Every other status, and any
    result shape that is not the expected dict, says nothing usable.

    ``userIdStatus`` is compared with ``==`` against the int constants below,
    which is unsafe for JSON ``true``/``false``: in Python ``True == 1``, so a
    boolean status would otherwise masquerade as Enabled. Booleans are
    therefore rejected up front.
    """
    if not isinstance(result, dict):
        return SlotCredential.unreadable()
    status = result.get("userIdStatus")
    if isinstance(status, bool):
        return SlotCredential.unreadable()
    if status == USER_ID_STATUS_AVAILABLE:
        return SlotCredential.empty()
    code = result.get("userCode")
    if status == USER_ID_STATUS_ENABLED and isinstance(code, str) and code.strip():
        return SlotCredential.known(code)
    return SlotCredential.unreadable()


@dataclass(repr=False, eq=False)
class ZWaveJSUILock(BaseLock):
    """Lock bridged through zwave-js-ui's MQTT gateway (api-driven)."""

    _api_base: str | None = field(init=False, default=None)
    # Topic of the live api response subscription, or None when there is
    # none. Mirrors zigbee2mqtt's ``_subscribed_topic``: the unsub itself is
    # held by the base registry, and this records what it covers.
    _api_response_topic: str | None = field(init=False, default=None)
    # Outstanding api calls by nonce. The single response handler resolves
    # whichever future the gateway's echoed nonce names.
    _pending_api_calls: dict[str, asyncio.Future[dict[str, Any]]] = field(
        init=False, default_factory=dict
    )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MQTT_DOMAIN

    def _parsed_identifier(self) -> tuple[str, int] | None:
        """Return ``(home_hex, node_id)`` from the device registry entry."""
        if not self.device_entry:
            return None
        return next(
            (
                parsed
                for identifier in self.device_entry.identifiers
                if len(identifier) >= 2
                and (parsed := parse_zwave_js_ui_identifier(str(identifier[1])))
            ),
            None,
        )

    def _resolve_state_topic(self) -> str | None:
        """
        Resolve this lock's discovery ``state_topic``; None means disconnected.

        zwave-js-ui publishes the value topic verbatim, so VALUEID, NAMED, and
        location-prefixed gateways all work with no reconstruction. There is
        deliberately no ``command_topic`` fallback: the command topic addresses
        a different value (``targetMode``) than the state topic, so stripping
        its shape would double-count the ``/set`` suffix and leave a topic that
        points at the wrong value.
        """
        device_id = self.lock.device_id
        if not device_id:
            return None
        try:
            info = mqtt_debug_info.info_for_device(self.hass, device_id)
        except KeyError:
            # MQTT integration data not loaded.
            LOGGER.debug("MQTT debug info unavailable for %s", self.lock.entity_id)
            return None
        for entity_info in info.get("entities", []):
            if entity_info.get("entity_id") != self.lock.entity_id:
                continue
            discovery_data = entity_info.get("discovery_data") or {}
            payload = discovery_data.get("payload")
            if not isinstance(payload, dict):
                LOGGER.debug("No discovery payload for %s", self.lock.entity_id)
                return None
            state_topic = payload.get("state_topic")
            if isinstance(state_topic, str) and state_topic:
                return state_topic
            LOGGER.debug(
                "Discovery payload for %s has no state topic", self.lock.entity_id
            )
            return None
        return None

    def _prefix_and_node_topic(self) -> tuple[str, str] | None:
        """
        Split the state topic into ``(gateway prefix, node topic)``.

        The first segment is always the gateway's own topic prefix, and the
        last three are the value's ``<cc>/<endpoint>/<property>``. A topic with
        fewer than five segments leaves no room for both plus a node topic, so
        it can only be a MANUAL-gateway custom topic, whose shape says nothing
        about the node — unresolvable, never guessed.
        """
        if not (state_topic := self._resolve_state_topic()):
            return None
        parts = state_topic.split("/")
        if len(parts) < MIN_VALUE_TOPIC_SEGMENTS:
            return None
        return parts[0], "/".join(parts[:-VALUE_TOPIC_SEGMENTS])

    def _require_node(self) -> tuple[str, int]:
        """
        Return ``(home_hex, node_id)``, refusing a lock that has neither.

        Both halves are addresses: the home id picks this lock's gateway out
        of several on one broker, and the node id picks the lock out of that
        gateway's network. Neither can be inferred from anything else, so a
        lock missing them is not reachable at all.
        """
        if (parsed := self._parsed_identifier()) is None:
            raise LockDisconnected(
                f"{self.lock.entity_id} carries no zwave-js-ui device identifier"
            )
        return parsed

    async def async_is_integration_connected(self) -> bool:
        """Return whether MQTT is usable and this lock maps to a zwave-js-ui node."""
        if not mqtt_config_entry_enabled(self.hass):
            return False
        return bool(self._parsed_identifier() and self._prefix_and_node_topic())

    async def async_is_device_available(self) -> bool:
        """Return whether the lock entity reports an operational state."""
        state = self.hass.states.get(self.lock.entity_id)
        return not (state is None or state.state == "unavailable")

    async def _async_subscribe(
        self, topic: str, handler: Callable[[ReceiveMessage], None]
    ) -> CALLBACK_TYPE:
        """
        Subscribe, routing a refusal onto the reconnect path.

        Home Assistant raises HomeAssistantError when MQTT is unloaded,
        reloading, or disabled. The BaseLock contract forbids a bare
        HomeAssistantError escaping a provider, and this failure is exactly
        a lost connection, so it becomes LockDisconnected.
        """
        try:
            return await async_subscribe(self.hass, topic, handler)
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Failed to subscribe to {topic} for {self.lock.entity_id}: {err}"
            ) from err

    @callback
    def _api_response_received(self, msg: ReceiveMessage) -> None:
        """
        Resolve whichever pending call the response's echoed nonce names.

        The ``@callback`` decorator is load-bearing: it keeps dispatch on the
        event loop, so touching the future here is safe. Without it Home
        Assistant runs the handler in a worker thread and ``set_result``
        races the loop.
        """
        if not self._pending_api_calls:
            # These topics carry every client's api traffic, and some
            # responses (getNodes) are large. With nothing outstanding there
            # is nothing to correlate, so skip the parse entirely.
            return
        try:
            payload = json.loads(msg.payload)
        except ValueError:
            LOGGER.debug("Ignoring non-JSON payload on %s", msg.topic)
            return
        origin = payload.get("origin") if isinstance(payload, dict) else None
        nonce = origin.get(REQUEST_ID_KEY) if isinstance(origin, dict) else None
        if not isinstance(nonce, str):
            return
        future = self._pending_api_calls.get(nonce)
        if future is not None and not future.done():
            future.set_result(payload)

    async def _async_resolve_api_base(self) -> str:
        """
        Resolve and cache ``<prefix>/_CLIENTS/ZWAVE_GATEWAY-<name>``.

        MQTT ``+`` matches a whole level, so the ZWAVE_GATEWAY- prefix filter
        is applied client-side on the received topic segment. Retained
        statuses arrive immediately after subscribing; the window exists only
        to collect them all before deciding.

        The api response subscription is established here, before the window,
        rather than per call. Home Assistant registers a subscription locally
        straight away but defers the wire SUBSCRIBE through a ~0.1s debouncer,
        while a publish goes out immediately -- so a subscribe/publish pair in
        one breath loses every api response the gateway sends before the
        broker has been told to route it, and it sends them unretained. The
        discovery window is the one place already long enough to outlast that
        debounce, so every later call publishes into a subscription that has
        been live for seconds.
        """
        if self._api_base:
            return self._api_base

        home_hex, _ = self._require_node()
        if (resolved := self._prefix_and_node_topic()) is None:
            raise LockDisconnected("Cannot resolve gateway prefix from discovery data")
        prefix = resolved[0]

        if self._api_response_topic is None:
            # One wildcard covers every gateway on the prefix, so the
            # disambiguation getInfo calls below are already correlated too.
            response_topic = f"{prefix}/_CLIENTS/+/api/+"
            self._register_push_unsub(
                await self._async_subscribe(response_topic, self._api_response_received)
            )
            self._api_response_topic = response_topic

        gateways: set[str] = set()

        @callback
        def _status_received(msg: ReceiveMessage) -> None:
            """Record the gateway that published a retained client status."""
            client = msg.topic.split("/")[-2]
            if client.startswith(GATEWAY_CLIENT_PREFIX):
                gateways.add(client)

        unsub = await self._async_subscribe(
            f"{prefix}/_CLIENTS/+/status", _status_received
        )
        try:
            await asyncio.sleep(GATEWAY_DISCOVERY_TIMEOUT)
        finally:
            unsub()

        if not gateways:
            raise LockDisconnected(
                f"No zwave-js-ui gateway published a client status under "
                f"{prefix}/_CLIENTS"
            )

        candidates = sorted(gateways)
        if len(candidates) == 1:
            self._api_base = f"{prefix}/_CLIENTS/{candidates[0]}"
            return self._api_base

        # Several gateways share the prefix, so ask each one which network it
        # runs and keep the one whose home id matches this lock's identifier.
        home_id = int(home_hex, 16)
        matches = []
        for client in candidates:
            api_base = f"{prefix}/_CLIENTS/{client}"
            try:
                response = await self._async_api_call_at(api_base, "getInfo", [])
            except (LockDisconnected, LockOperationFailed) as err:
                LOGGER.debug("Gateway %s did not answer getInfo: %s", api_base, err)
                continue
            result = response.get("result")
            homeid = result.get("homeid") if isinstance(result, dict) else None
            # ``True == 1`` in Python, so a JSON boolean would otherwise be
            # able to match a home id of 1. Same guard as
            # ``_project_user_code_result`` applies to userIdStatus.
            if isinstance(homeid, bool) or not isinstance(homeid, int):
                continue
            if homeid == home_id:
                matches.append(api_base)

        if len(matches) != 1:
            # Two gateways answering for the same home id are a primary and a
            # secondary controller on one network. Writing through the wrong
            # one silently programs a lock we were not asked about, so this
            # never tiebreaks.
            raise LockDisconnected(
                f"Could not identify which zwave-js-ui gateway serves "
                f"{self.lock.entity_id} among {', '.join(candidates)}"
            )
        self._api_base = matches[0]
        return self._api_base

    async def _async_api_call_at(
        self, api_base: str, api_name: str, args: list[Any]
    ) -> dict[str, Any]:
        """
        Call a zwave-js-ui MQTT api and return the correlated response payload.

        The gateway echoes the request payload in the response's ``origin``;
        the nonce keeps concurrent api users (the UI, other automations) from
        crossing wires. Timeout routes to LockDisconnected so the reconnect
        path runs; an explicit success=false is a per-operation failure.

        Requires the response subscription ``_async_resolve_api_base`` sets
        up. Subscribing here instead would publish into a subscription the
        broker has not been told about yet and lose the answer.
        """
        if self._api_response_topic is None:
            raise LockDisconnected(
                f"No zwave-js-ui api response subscription for {self.lock.entity_id}; "
                f"cannot call {api_name}"
            )

        nonce = uuid4().hex
        response_topic = f"{api_base}/api/{api_name}"
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending_api_calls[nonce] = future
        try:
            try:
                await async_publish(
                    self.hass,
                    f"{response_topic}/set",
                    json.dumps({"args": args, REQUEST_ID_KEY: nonce}),
                )
            except OSError as err:
                # Broker unreachable: route to disconnect so the reconnect
                # path runs instead of failing this one operation.
                raise LockDisconnected(
                    f"Failed to publish {api_name} for {self.lock.entity_id}: {err}"
                ) from err
            except HomeAssistantError as err:
                raise LockOperationFailed(
                    f"Failed to publish {api_name} for {self.lock.entity_id}: {err}"
                ) from err

            try:
                response = await asyncio.wait_for(future, timeout=API_CALL_TIMEOUT)
            except TimeoutError as err:
                raise LockDisconnected(
                    f"Timed out waiting for zwave-js-ui {api_name} response for "
                    f"{self.lock.entity_id}"
                ) from err
        finally:
            self._pending_api_calls.pop(nonce, None)

        if not response.get("success"):
            raise LockOperationFailed(
                f"zwave-js-ui {api_name} failed for {self.lock.entity_id}: "
                f"{response.get('message')}"
            )
        return response

    async def _async_api_call(self, api_name: str, args: list[Any]) -> dict[str, Any]:
        """
        Call an api on this lock's resolved gateway.

        A disconnect against the cached base drops it, so the next attempt
        rediscovers. The gateway can be renamed, replaced, or moved to
        another broker, and without this a base that nobody answers on any
        more would stick until the integration reloads.
        """
        api_base = await self._async_resolve_api_base()
        try:
            return await self._async_api_call_at(api_base, api_name, args)
        except LockDisconnected:
            self._api_base = None
            raise

    async def _async_user_code_command(self, method: str, args: list[Any]) -> Any:
        """Invoke a User Code CC API method on this node and return its result."""
        _, node_id = self._require_node()
        response = await self._async_api_call(
            "sendCommand",
            [
                # CC_USER_CODE is an IntEnum, which json.dumps would serialize
                # anyway; the cast keeps the wire payload's shape obvious.
                {"nodeId": node_id, "commandClass": int(CC_USER_CODE), "endpoint": 0},
                method,
                args,
            ],
        )
        return response.get("result")

    @callback
    def teardown_push_subscription(self) -> None:
        """
        Drop the api response subscription and everything that depended on it.

        The resolved gateway goes too: a reconnect can bring back a renamed
        gateway, a different broker, or a rebuilt topology, so the next api
        call rediscovers rather than publishing at a base nobody listens on.
        Idempotent, as the base class requires.
        """
        self._clear_push_unsubs()
        self._api_response_topic = None
        self._api_base = None
        for future in self._pending_api_calls.values():
            if not future.done():
                future.cancel()
        self._pending_api_calls.clear()
