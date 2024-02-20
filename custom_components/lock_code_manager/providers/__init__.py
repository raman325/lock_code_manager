"""
Integrations module.

There should be one file per integration, named after the integration.
"""

from ._base import BaseLock
from .zwave_js import ZWaveJSLock

INTEGRATIONS_CLASS_MAP: dict[str, BaseLock] = {
    "zwave_js": ZWaveJSLock,
}
