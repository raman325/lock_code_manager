"""Tests for centralized provider class resolution."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.lock_code_manager.providers import (
    SUPPORTED_PLATFORMS,
    Zigbee2MQTTLock,
    ZWaveJSLock,
    ZWaveJSUILock,
    resolve_provider_class,
)


def test_single_provider_platform_ignores_device():
    """Non-mqtt platforms resolve from the map; device entry is irrelevant."""
    assert resolve_provider_class("zwave_js", None) is ZWaveJSLock


def test_unknown_platform_resolves_none():
    """An unrecognized platform resolves to None."""
    assert resolve_provider_class("not_a_platform", None) is None


async def test_mqtt_dispatches_on_identifier(hass: HomeAssistant) -> None:
    """mqtt resolves per-device by identifier prefix; unclaimed devices resolve None."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)

    z2m_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zigbee2mqtt_0xc0ffee")},
        name="Z2MLock",
    )
    zui_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zwavejs2mqtt_0xd4ee5a7a_node20")},
        name="ZUILock",
    )
    unclaimed_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "somebridge_1")},
        name="UnclaimedLock",
    )

    assert resolve_provider_class("mqtt", z2m_device) is Zigbee2MQTTLock
    assert resolve_provider_class("mqtt", zui_device) is ZWaveJSUILock
    assert resolve_provider_class("mqtt", unclaimed_device) is None
    assert resolve_provider_class("mqtt", None) is None


def test_supported_platforms():
    """SUPPORTED_PLATFORMS covers every map key plus mqtt exactly once."""
    assert "mqtt" in SUPPORTED_PLATFORMS
    assert "zwave_js" in SUPPORTED_PLATFORMS
    assert len(SUPPORTED_PLATFORMS) == len(set(SUPPORTED_PLATFORMS))
