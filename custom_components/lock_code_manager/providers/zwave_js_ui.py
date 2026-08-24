"""Module for locks bridged through zwave-js-ui's MQTT gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import timedelta
import json
import re
from typing import Any, Literal
from uuid import uuid4

from zwave_js_server.const import CommandClass

from homeassistant.components.mqtt import (
    DOMAIN as MQTT_DOMAIN,
    async_publish,
    async_subscribe,
)
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError

from ..domain.credentials import (
    Credential,
    CredentialRef,
    User,
    WriteResult,
    user_from_slot,
)
from ..domain.exceptions import LockDisconnected, LockOperationFailed
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import entity_state_is_available, parse_slot_num, resolve_discovery_payload
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
# A FLiRS (battery) lock answers on its own wake schedule, so one command can
# legitimately take the better part of a minute. Giving up early does not
# cancel anything: zwave-js-ui keeps working the queued command, and the retry
# stacks a duplicate behind it, so a short budget turns a slow mesh into a
# gateway queue that jams and drains slower than the lock can answer. Verified
# on live Kwikset FLiRS meshes, where 15 seconds was already too tight.
API_CALL_TIMEOUT = 60.0
# ``getInfo`` is assembled from the gateway's own cached driver state and puts
# nothing on the mesh (zwave-js-ui ``ZwaveClient.getInfo``), so a client that
# has not answered in five seconds is not busy, it is gone. Bounding it apart
# from the mesh budget is what keeps one dead gateway from stalling every
# lock's resolution for a minute per candidate.
#
# Deliberately NOT used for ``getUsersCount``: despite the name, that is a
# User Code Command Class UsersNumberGet sent to the node
# (node-zwave-js ``UserCodeCCAPI.getUsersCount``), so it waits on the same
# wake schedule as every other lock command and keeps the mesh budget.
GATEWAY_LOCAL_TIMEOUT = 5.0
GATEWAY_DISCOVERY_TIMEOUT = 3.0
# Inter-operation pacing, widened from the base default. Every api call this
# provider makes is serialized onto zwave-js-ui's single command queue, which
# it also shares with its own UI and any other MQTT client, and a FLiRS lock
# drains that queue at wake speed. Pacing our own operations wider costs
# nothing on a healthy mesh and keeps LCM from being the client that fills it.
ZWAVE_JS_UI_OPERATION_DELAY = 5.0

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

# Slot values and keypad notifications both publish under
# ``<cc>/<endpoint>/<property>/<propertyKey>``.
NODE_VALUE_SEGMENTS = 4
# Topic segments the two gateway naming styles spell these command classes
# with: VALUEID uses the numeric id, NAMED the zwave-js command class name.
# The literals are the enum values imported above, written out because they
# are compared against topic text.
USER_CODE_CC_SEGMENTS = frozenset({str(int(CC_USER_CODE)), "user_code"})
NOTIFICATION_CC_SEGMENTS = frozenset({str(int(CC_NOTIFICATION)), "notification"})
# Notification CC label for lock operations, sanitized into a topic segment
# (whitespace becomes ``_``); compared case-insensitively.
NOTIFICATION_ACCESS_CONTROL = "access_control"
# The two Access Control events that name the code slot that operated the
# lock. Everything else under the label (manual, RF, jams) names no slot.
KEYPAD_EVENT_TO_LOCKED = {
    "keypad_lock_operation": True,
    "keypad_unlock_operation": False,
}


def _is_endpoint_zero(segment: str) -> bool:
    """Return whether a topic segment addresses endpoint 0 in either naming style."""
    return segment in ("0", "endpoint_0")


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


def _published_code(value: Any) -> str | None:
    """
    Read a published ``userCode`` value as a Personal Identification Number.

    A raw-payload gateway publishes the bare value, so an all-digit code
    arrives as a JSON number and unwraps to an int. ``str`` recovers the
    digits it was published with exactly: JSON has no number form with a
    leading zero, so a code that starts with one never parses as a number
    and reaches here as the string it was written as.

    Booleans are not codes (and ``True`` would otherwise stringify to a
    code of ``"True"``), and neither is an empty or blank string -- which is
    also how a withheld code and a cleared slot both look, indistinguishably.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    return None


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
    # The api response subscription is deliberately NOT in the base's
    # push-unsub registry: that bucket is released only when supports_push
    # is true, and this subscription is the api transport, alive for the
    # provider's whole lifetime rather than the push lifecycle.
    _api_response_unsub: Callable[[], None] | None = field(init=False, default=None)
    # Topic the live subscription covers, or None when there is none.
    _api_response_topic: str | None = field(init=False, default=None)
    # Outstanding api calls by nonce. The single response handler resolves
    # whichever future the gateway's echoed nonce names.
    _pending_api_calls: dict[str, asyncio.Future[dict[str, Any]]] = field(
        init=False, default_factory=dict
    )
    # Node topic the live wildcard subscription covers, or None when there is
    # none. Unlike the api subscription above, this one IS the push lifecycle,
    # so its unsub goes in the base's push-unsub registry.
    _subscribed_node_topic: str | None = field(init=False, default=None)
    _min_operation_delay: float = field(init=False, default=ZWAVE_JS_UI_OPERATION_DELAY)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MQTT_DOMAIN

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return True

    @property
    def usercode_scan_interval(self) -> timedelta:
        """
        Return scan interval for usercodes.

        Inert as long as ``supports_push`` is true: the coordinator leaves its
        update interval unset for a push provider, so nothing schedules a poll
        at this cadence and drift is caught by the hourly hard refresh instead.
        This is the fallback the coordinator would use if push were ever
        unsupported or disabled, spaced out because every slot costs its own
        api round trip through the gateway and the mesh.
        """
        return timedelta(minutes=5)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return interval for hard refresh."""
        return timedelta(hours=1)

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Subscribe to the node topic before the coordinator runs its first poll."""
        await self._async_ensure_node_subscription()

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
        payload = resolve_discovery_payload(self.hass, self.lock)
        if payload is None:
            return None
        state_topic = payload.get("state_topic")
        if isinstance(state_topic, str) and state_topic:
            return state_topic
        LOGGER.debug("Discovery payload for %s has no state topic", self.lock.entity_id)
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
        return entity_state_is_available(self.hass, self.lock.entity_id)

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
    def _process_node_message(self, topic: str, payload: bytes | str) -> None:
        """
        Classify one message from the node's wildcard subscription.

        The subscription covers everything the node publishes, so most
        messages here belong to other command classes and are dropped without
        comment. The node-topic guard is per-instance rather than global:
        Home Assistant hands a message to every matching subscription, and
        two locks on one broker would otherwise read each other's values.

        The ``@callback`` decorator is load-bearing, for the same reason it
        is on ``_api_response_received``: it keeps dispatch on the event
        loop, so the coordinator pushes and event fires below happen where
        Home Assistant expects them.
        """
        node_topic = self._subscribed_node_topic
        if node_topic is None or not topic.startswith(f"{node_topic}/"):
            return
        segments = topic.removeprefix(f"{node_topic}/").split("/")
        if len(segments) != NODE_VALUE_SEGMENTS:
            return
        command_class, endpoint, property_name, property_key = segments
        if not _is_endpoint_zero(endpoint):
            return
        if command_class in USER_CODE_CC_SEGMENTS:
            self._process_user_code_value(property_name, property_key, payload)
        elif command_class in NOTIFICATION_CC_SEGMENTS:
            self._process_notification(topic, property_name, property_key, payload)

    @callback
    def _process_user_code_value(
        self, property_name: str, slot_segment: str, payload: bytes | str
    ) -> None:
        """
        Confirm a slot from a User Code Command Class value publication.

        The two properties are published separately, so each has to stand on
        its own. A ``userCode`` carrying no usable code says nothing at all:
        empty is how the gateway spells both a withheld code and a cleared
        slot, and reading it as cleared would tell sync the code is gone.
        ``userIdStatus`` is what carries occupancy, and only Available means
        the slot holds nothing -- any other status leaves the code unknown.
        """
        if (slot_num := parse_slot_num(slot_segment)) is None:
            return
        value = _unwrap_mqtt_value(payload)
        if property_name == "userCode" and (code := _published_code(value)) is not None:
            self._confirm_slot(slot_num, SlotCredential.known(code))
        elif (
            property_name == "userIdStatus"
            # ``True == 1`` and ``False == 0`` in Python, so a JSON boolean
            # would otherwise report an occupied slot as Available. Same
            # guard ``_project_user_code_result`` and ``homeid`` apply.
            and not isinstance(value, bool)
            and value == USER_ID_STATUS_AVAILABLE
        ):
            self._confirm_slot(slot_num, SlotCredential.empty())

    @callback
    def _process_notification(
        self,
        topic: str,
        label: str,
        event_label: str,
        payload: bytes | str,
    ) -> None:
        """
        Fire a code slot event for a keypad lock or unlock operation.

        The value is the notification's parsed User Code Report, whose
        ``userId`` is the slot that operated the lock. Raw-buffer parameters
        arrive as a hex string instead and name nobody, so anything that is
        not a dict with a numeric user id is dropped rather than guessed at.
        """
        if label.lower() != NOTIFICATION_ACCESS_CONTROL:
            return
        # ``False`` is a valid mapping value (an unlock), so this compares
        # against None -- a truthiness test would drop every unlock event.
        if (to_locked := KEYPAD_EVENT_TO_LOCKED.get(event_label.lower())) is None:
            return
        value = _unwrap_mqtt_value(payload)
        user_id = value.get("userId") if isinstance(value, dict) else None
        if (code_slot := parse_slot_num(user_id)) is None:
            LOGGER.debug(
                "Lock %s: ignoring %s notification naming no code slot: %r",
                self.lock.entity_id,
                event_label,
                value,
            )
            return
        self.async_fire_code_slot_event(
            code_slot=code_slot,
            to_locked=to_locked,
            action_text=event_label,
            source_data={"topic": topic, "value": value},
        )

    @callback
    def _node_subscription_current(self, node_topic: str | None) -> bool:
        """
        Return whether the live subscription already covers what is wanted.

        A topic that cannot be resolved right now leaves a working
        subscription in place rather than tearing it down: the discovery data
        is transiently missing, not pointing somewhere new.
        """
        return bool(self._push_unsubs) and (
            node_topic is None or self._subscribed_node_topic == node_topic
        )

    async def _async_ensure_node_subscription(self) -> None:
        """Subscribe to the node's value tree; idempotent and drift-aware."""
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        resolved = self._prefix_and_node_topic()
        node_topic = resolved[1] if resolved else None
        if self._node_subscription_current(node_topic):
            return
        if node_topic is None:
            raise LockDisconnected(
                f"Cannot subscribe for {self.lock.entity_id} — node topic not "
                "resolvable from MQTT discovery data"
            )

        # Topic changed (a node rename republishes discovery) or first
        # subscribe. The node subscription is the only thing this provider
        # puts in the push-unsub registry -- the api transport is tracked
        # separately -- so clearing all of them clears exactly this one.
        self._clear_push_unsubs()
        self._subscribed_node_topic = None

        @callback
        def message_received(msg: ReceiveMessage) -> None:
            """Hand a node message to the classifier on the event loop."""
            self._process_node_message(msg.topic, msg.payload)

        unsub = await self._async_subscribe(f"{node_topic}/#", message_received)
        self._register_push_unsub(unsub)
        self._subscribed_node_topic = node_topic
        LOGGER.debug(
            "Subscribed to zwave-js-ui node topic %s for %s",
            node_topic,
            self.lock.entity_id,
        )

    @callback
    def setup_push_subscription(self) -> None:
        """
        Subscribe via background task when still unsubscribed (e.g. reconnect).

        Primary subscribe is ``await`` in ``async_setup``.
        """
        resolved = self._prefix_and_node_topic()
        node_topic = resolved[1] if resolved else None
        if self._node_subscription_current(node_topic):
            return

        if node_topic is None:
            raise LockDisconnected(
                f"Cannot subscribe to push updates for {self.lock.entity_id} - "
                "no node topic"
            )

        if not mqtt_config_entry_enabled(self.hass):
            LOGGER.debug(
                "Deferring MQTT push subscribe for %s — MQTT integration disabled",
                self.lock.entity_id,
            )
            return

        async def _subscribe_or_log() -> None:
            """
            Run ``_async_ensure_node_subscription`` from the reconnect task path.

            Log errors only; sync ``setup_push_subscription`` cannot raise.
            """
            try:
                await self._async_ensure_node_subscription()
            except LockDisconnected as err:
                LOGGER.debug(
                    "Lock %s: push subscription deferred (disconnected): %s",
                    self.lock.entity_id,
                    err,
                )
            except Exception:
                LOGGER.exception(
                    "Lock %s: MQTT subscribe failed unexpectedly",
                    self.lock.entity_id,
                )

        self.hass.async_create_task(_subscribe_or_log())

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
            self._api_response_unsub = await self._async_subscribe(
                response_topic, self._api_response_received
            )
            self._api_response_topic = response_topic

        gateways: set[str] = set()

        @callback
        def _status_received(msg: ReceiveMessage) -> None:
            """
            Record a gateway whose retained client status says it is online.

            The status topic is retained and the gateway's last will retains
            ``{"value": false}`` on it, so a client that was decommissioned or
            crashed leaves its status behind indefinitely. Binding one without
            reading the payload picks a corpse -- and when it is the only
            status on the prefix, it is bound outright, with no getInfo to
            expose it, so every call times out and each rediscovery re-picks
            the same dead client.

            The payload is the usual value shape, so a ``{"value": true}``
            wrapper and a bare ``true`` both unwrap to the same thing. This is
            also the one place in this module that does NOT reject a boolean:
            here ``True`` is not a JSON type confusion, it is the online
            signal itself.
            """
            client = msg.topic.split("/")[-2]
            if client.startswith(GATEWAY_CLIENT_PREFIX) and _unwrap_mqtt_value(
                msg.payload
            ):
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
        matches: list[str] = []
        for client in candidates:
            api_base = f"{prefix}/_CLIENTS/{client}"
            try:
                response = await self._async_api_call_at(
                    api_base, "getInfo", [], timeout=GATEWAY_LOCAL_TIMEOUT
                )
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
        self,
        api_base: str,
        api_name: str,
        args: list[Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        Call a zwave-js-ui MQTT api and return the correlated response payload.

        The gateway echoes the request payload in the response's ``origin``;
        the nonce keeps concurrent api users (the UI, other automations) from
        crossing wires. Timeout routes to LockDisconnected so the reconnect
        path runs; an explicit success=false is a per-operation failure.

        ``timeout`` overrides the mesh-sized default for an api the gateway
        answers out of its own memory; it is resolved here rather than in the
        signature so the module constant stays the single source of truth.

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
                response = await asyncio.wait_for(
                    future, API_CALL_TIMEOUT if timeout is None else timeout
                )
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

    async def _async_ensure_operational(self) -> None:
        """
        Refuse to address a lock whose transport, gateway, or entity is down.

        Every public operation shares this preamble, and the order is what
        makes the error useful: MQTT being off explains a missing gateway,
        and a missing gateway explains an unavailable entity, so the
        outermost cause is the one reported.

        This is deliberately stricter than Zigbee2MQTT's equivalent, which
        checks device availability before reads but not before writes: here a
        write is an api round trip that an unavailable node cannot answer, so
        it fails as a disconnect up front instead of as a timeout ten seconds
        later.
        """
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")
        if not await self.async_is_integration_connected():
            raise LockDisconnected("Lock not connected")
        if not await self.async_is_device_available():
            raise LockDisconnected("Device not available")

    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Read Personal Identification Number codes one index at a time.

        Each slot is one api round trip, issued sequentially so the gateway
        and the lock's firmware answer each GET before the next goes out. A
        read that fails produces an unreadable credential rather than an
        empty one, for the same reason Zigbee2MQTT's read does: a transient
        failure taken for a confirmed-empty slot makes sync storm
        reprogramming once the lock answers again.
        """
        await self._async_ensure_operational()

        # Node renames and gateway migrations produce no disconnect, so the
        # hourly hard refresh -- which lands here, and is the only recurring
        # read a push provider makes -- is what notices the subscription has
        # drifted off the topic discovery now points at.
        try:
            await self._async_ensure_node_subscription()
        except LockDisconnected as err:
            LOGGER.debug(
                "Lock %s: could not refresh push subscription before poll: %s",
                self.lock.entity_id,
                err,
            )

        # One request per index, so the caller's scope bounds the work.
        code_slots = self.managed_slots if slots is None else slots
        if not code_slots:
            return []

        slot_states = {
            slot_num: await self._async_read_slot(slot_num)
            for slot_num in sorted(code_slots)
        }
        return [user_from_slot(slot, state) for slot, state in slot_states.items()]

    async def _async_read_slot(self, slot_num: int) -> SlotCredential:
        """
        Ask the lock what one slot holds.

        An api-level refusal says nothing about the slot, so it becomes
        unreadable -- never confirmed-empty. A disconnect is deliberately not
        caught: it is a lost transport rather than a bad slot, and the
        reconnect path only runs if it reaches the caller.
        """
        try:
            result = await self._async_user_code_command("get", [slot_num])
        except LockOperationFailed as err:
            LOGGER.debug(
                "Lock %s: slot %s read refused: %s", self.lock.entity_id, slot_num, err
            )
            return SlotCredential.unreadable()
        return _project_user_code_result(result)

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set a Personal Identification Number credential on a code slot.

        ``user_id`` is ignored; slot-only providers address the credential by
        ``credential.slot``. A successful api response is taken as confirmed
        because zwave-js-ui answers only once the driver's supervised set has
        completed, so success here means the lock acknowledged the write.

        Failures from the api client propagate untouched: the base and sync
        layers decide what a refused or disconnected write means, and the
        optimistic push below is skipped either way.
        """
        code_slot = credential.slot
        await self._async_ensure_operational()
        await self._async_user_code_command(
            "set", [code_slot, USER_ID_STATUS_ENABLED, pin]
        )
        self._push_credential_update(code_slot, SlotCredential.known(pin))
        return WriteResult.CONFIRMED

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Clear a Personal Identification Number from a code slot.

        Mirrors ``async_set_credential``: the api answers after the driver's
        supervised clear, so a success pushes the slot empty and anything
        else propagates without touching the coordinator.
        """
        await self._async_ensure_operational()
        await self._async_user_code_command("clear", [ref.slot])
        self._push_credential_update(ref.slot, SlotCredential.empty())
        return True

    async def async_get_max_slot(self) -> int | None:
        """
        Report the User Code capacity the lock advertises, if it advertises one.

        ``getUsersCount`` is the User Code Command Class's own supported-users
        report, so it is the lock's answer rather than a guess. Only a
        positive integer is an answer: booleans are rejected first because
        ``True == 1`` in Python would otherwise turn a JSON ``true`` into a
        one-slot lock, the same trap this module guards on ``userIdStatus``
        and ``homeid``.
        """
        await self._async_ensure_operational()
        result = await self._async_user_code_command("getUsersCount", [])
        if isinstance(result, int) and not isinstance(result, bool) and result > 0:
            return result
        return None

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """Perform hard refresh and return all codes."""
        return await self.async_get_usercodes()

    @callback
    def _release_api_subscription(self) -> None:
        """
        Drop the api response subscription and everything that depended on it.

        The resolved gateway goes too: whatever brought us here (a reconnect,
        an unload) can be followed by a renamed gateway, a different broker,
        or a rebuilt topology, so the next api call rediscovers rather than
        publishing at a base nobody listens on. Idempotent.
        """
        if (unsub := self._api_response_unsub) is not None:
            self._api_response_unsub = None
            try:
                unsub()
            except HomeAssistantError as err:
                # MQTT torn down ahead of us already dropped the
                # subscription. Same log-and-continue the base registry
                # applies, so one bad unsub cannot abort teardown.
                LOGGER.debug(
                    "Lock %s: api unsubscribe raised, continuing: %s",
                    self.lock.entity_id,
                    err,
                )
        self._api_response_topic = None
        self._api_base = None
        for future in self._pending_api_calls.values():
            if not future.done():
                future.cancel()
        self._pending_api_calls.clear()

    @callback
    def teardown_push_subscription(self) -> None:
        """
        Drop the node subscription and the api transport together.

        The api subscription is not part of the push lifecycle, but it is
        dead in exactly the circumstances that end the push one -- a
        connection-down transition, an unload -- and nothing else runs on
        both paths, so teardown owns both. Idempotent.
        """
        self._clear_push_unsubs()
        self._subscribed_node_topic = None
        self._release_api_subscription()
