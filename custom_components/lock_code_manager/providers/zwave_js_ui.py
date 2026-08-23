"""Module for locks bridged through zwave-js-ui's MQTT gateway."""

from __future__ import annotations

from dataclasses import dataclass
import re

from zwave_js_server.const import CommandClass

from homeassistant.components.mqtt import (
    DOMAIN as MQTT_DOMAIN,
    debug_info as mqtt_debug_info,
)
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled

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

# A zwave-js-ui value topic ends with ``<cc>/<endpoint>/<property>`` and, for
# values that have one, a fourth ``<propertyKey>`` segment. Door Lock state
# values have no propertyKey, so a lock's state topic always ends with exactly
# these three segments and everything before them is the node topic.
VALUE_TOPIC_SEGMENTS = 3


def parse_zwave_js_ui_identifier(identifier: str) -> tuple[str, int] | None:
    """Parse ``(home_hex, node_id)`` out of a zwave-js-ui device identifier."""
    if match := ZWAVE_JS_UI_IDENTIFIER_RE.match(identifier):
        return match["home_hex"].lower(), int(match["node_id"])
    return None


@dataclass(repr=False, eq=False)
class ZWaveJSUILock(BaseLock):
    """Lock bridged through zwave-js-ui's MQTT gateway (api-driven)."""

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
        last three are the value's ``<cc>/<endpoint>/<property>``. A topic too
        short to hold both is a MANUAL-gateway custom topic, whose shape says
        nothing about the node — unresolvable, never guessed.
        """
        if not (state_topic := self._resolve_state_topic()):
            return None
        parts = state_topic.split("/")
        if len(parts) < VALUE_TOPIC_SEGMENTS + 1:
            return None
        return parts[0], "/".join(parts[:-VALUE_TOPIC_SEGMENTS])

    async def async_is_integration_connected(self) -> bool:
        """Return whether MQTT is usable and this lock maps to a zwave-js-ui node."""
        if not mqtt_config_entry_enabled(self.hass):
            return False
        return bool(self._parsed_identifier() and self._prefix_and_node_topic())

    async def async_is_device_available(self) -> bool:
        """Return whether the lock entity reports an operational state."""
        state = self.hass.states.get(self.lock.entity_id)
        return not (state is None or state.state == "unavailable")
