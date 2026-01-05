"""
Integrations module.

There should be one file per integration, named after the integration.
"""

from __future__ import annotations

from ._base import BaseLock
from .virtual import VirtualLock
from .zigbee2mqtt import Zigbee2MQTTLock
from .zwave_js import ZWaveJSLock

INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "mqtt": Zigbee2MQTTLock,  # MQTT locks are Zigbee2MQTT
    "virtual": VirtualLock,
    "zwave_js": ZWaveJSLock,
}
