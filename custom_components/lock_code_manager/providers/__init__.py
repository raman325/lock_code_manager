"""Lock provider implementations."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr

from ._base import BaseLock
from .akuvox import AkuvoxLock
from .matter import MatterLock
from .schlage import SchlageLock
from .virtual import VirtualLock
from .zha import ZHALock
from .zigbee2mqtt import Z2M_IDENTIFIER_PREFIX, Zigbee2MQTTLock
from .zwave_js import ZWaveJSLock
from .zwave_js_ui import ZWaveJSUILock, parse_zwave_js_ui_identifier

INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "local_akuvox": AkuvoxLock,
    "matter": MatterLock,
    "schlage": SchlageLock,
    "virtual": VirtualLock,
    "zha": ZHALock,
    "zwave_js": ZWaveJSLock,
}

# Platform allowlist for the config-flow entity selector and membership checks.
# Wider than what resolves to a provider: an mqtt lock is only selectable if
# its device identifier names a bridge some provider speaks.
SUPPORTED_PLATFORMS: tuple[str, ...] = (*INTEGRATIONS_CLASS_MAP, "mqtt")


def resolve_provider_class(
    platform: str, device_entry: dr.DeviceEntry | None
) -> type[BaseLock] | None:
    """
    Resolve the provider class for a lock entity's platform and device.

    The mqtt platform is a transport, not a device family: the device
    registry identifier prefix says which bridge published the discovery
    payload, and therefore which provider speaks the device's protocol.
    Unrecognized mqtt devices resolve to None — callers must reject them,
    never fall back to a guessed provider.
    """
    if platform != "mqtt":
        return INTEGRATIONS_CLASS_MAP.get(platform)
    if device_entry is None:
        return None
    for identifier in device_entry.identifiers:
        if len(identifier) < 2:
            continue
        value = str(identifier[1])
        if value.startswith(Z2M_IDENTIFIER_PREFIX):
            return Zigbee2MQTTLock
        if parse_zwave_js_ui_identifier(value):
            return ZWaveJSUILock
    return None
