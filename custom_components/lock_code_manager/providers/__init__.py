"""Lock provider implementations."""

from __future__ import annotations

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.helpers import device_registry as dr, entity_registry as er

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

# Selector allowlist; wider than what resolves, because mqtt is per-device.
CONFIG_FLOW_PLATFORMS: tuple[str, ...] = (*INTEGRATIONS_CLASS_MAP, MQTT_DOMAIN)


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
    if platform != MQTT_DOMAIN:
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


def resolve_provider_class_for_entity(
    dev_reg: dr.DeviceRegistry, lock_entry: er.RegistryEntry
) -> type[BaseLock] | None:
    """Resolve a provider for an entity, looking up its device for mqtt dispatch."""
    device_entry = (
        dev_reg.async_get(lock_entry.device_id) if lock_entry.device_id else None
    )
    return resolve_provider_class(lock_entry.platform, device_entry)
