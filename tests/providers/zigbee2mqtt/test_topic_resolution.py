"""Topic resolution from MQTT discovery data for the Zigbee2MQTT provider."""

from __future__ import annotations

from homeassistant.components.mqtt import debug_info as mqtt_debug_info
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .conftest import Z2M_FULL_TOPIC


async def test_debug_info_shape_pin(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """
    Pin the shape of mqtt.debug_info.info_for_device that the provider relies on.

    The provider reads entities[*].entity_id and
    entities[*].discovery_data.payload.state_topic / command_topic. If this
    test fails after a Home Assistant bump, _resolve_device_topic() needs
    updating in the same commit.
    """
    device_id = mqtt_lock_discovered.device_id
    assert device_id is not None
    info = mqtt_debug_info.info_for_device(hass, device_id)
    entities = info["entities"]
    entry = next(
        e for e in entities if e["entity_id"] == mqtt_lock_discovered.entity_id
    )
    payload = entry["discovery_data"]["payload"]
    assert payload["state_topic"] == Z2M_FULL_TOPIC
    assert payload["command_topic"] == f"{Z2M_FULL_TOPIC}/set"
