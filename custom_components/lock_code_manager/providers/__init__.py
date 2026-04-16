"""Lock provider implementations."""

from __future__ import annotations

from ._base import BaseLock
from .akuvox import AkuvoxLock
from .matter import MatterLock
from .schlage import SchlageLock
from .virtual import VirtualLock
from .zwave_js import ZWaveJSLock

INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "local_akuvox": AkuvoxLock,
    "matter": MatterLock,
    "schlage": SchlageLock,
    "virtual": VirtualLock,
    "zwave_js": ZWaveJSLock,
}
