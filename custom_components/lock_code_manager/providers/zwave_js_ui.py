"""Module for locks bridged through zwave-js-ui's MQTT gateway."""

from __future__ import annotations

from dataclasses import dataclass
import re

from zwave_js_server.const import CommandClass

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN

from ._base import BaseLock

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
