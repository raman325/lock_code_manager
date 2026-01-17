"""
Integrations module.

There should be one file per integration, named after the integration.
"""

from __future__ import annotations

from ._base import BaseLock
from .virtual import VirtualLock
from .zha import ZHALock
from .zwave_js import ZWaveJSLock

INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "virtual": VirtualLock,
    "zha": ZHALock,
    "zwave_js": ZWaveJSLock,
}
