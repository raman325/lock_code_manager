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

# Platform dispatch, and the whole of it. ``CodelessLock`` is deliberately
# absent -- and not imported here either: it answers for the members an entry
# declares it holds the credentials for, which is a fact about that entry's
# configuration rather than about a platform, so the factory that reads the
# declaration imports it. See ``domain.locks.resolve_member_provider_class``.
INTEGRATIONS_CLASS_MAP: dict[str, type[BaseLock]] = {
    "local_akuvox": AkuvoxLock,
    "matter": MatterLock,
    "schlage": SchlageLock,
    "virtual": VirtualLock,
    "zha": ZHALock,
    "zwave_js": ZWaveJSLock,
}


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
    values = [
        str(identifier[1])
        for identifier in device_entry.identifiers
        if len(identifier) >= 2
    ]
    # Zigbee2MQTT is tested first, and has to be: its own prefix is fixed,
    # while the zwave-js-ui identifier is recognized by its tail alone (the
    # head is operator-configurable), so a device whose address happened to
    # end ``0x<hex>_node<n>`` would be claimed by the wrong provider if the
    # order were reversed.
    #
    # Two passes over the whole set, rather than both rules per identifier,
    # because ``identifiers`` is a SET and its iteration order is arbitrary.
    # A device carrying both shapes -- which is what a stale registry row
    # from a re-paired device looks like -- resolved to whichever one the
    # hash happened to yield first, so the same device could dispatch
    # differently across restarts. The precedence has to hold over the
    # device, not over one identifier at a time.
    if any(value.startswith(Z2M_IDENTIFIER_PREFIX) for value in values):
        return Zigbee2MQTTLock
    if any(parse_zwave_js_ui_identifier(value) for value in values):
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
