"""
Integrations module.

There should be one file per integration, named after the integration.
"""

from __future__ import annotations

from ._base import BaseLock
from .akuvox import AkuvoxLock
from .matter import MatterLock
from .virtual import VirtualLock
from .zwave_js import ZWaveJSLock

INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "local_akuvox": AkuvoxLock,
    "matter": MatterLock,
    "virtual": VirtualLock,
    "zwave_js": ZWaveJSLock,
}
