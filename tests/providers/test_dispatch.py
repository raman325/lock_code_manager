"""Tests for centralized provider class resolution."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.allocation import (
    LockQuerySkipped,
    build_lock_instance,
)
from custom_components.lock_code_manager.domain.locks import async_create_lock_instance
from custom_components.lock_code_manager.providers import (
    CONFIG_FLOW_PLATFORMS,
    INTEGRATIONS_CLASS_MAP,
    Zigbee2MQTTLock,
    ZWaveJSLock,
    ZWaveJSUILock,
    resolve_provider_class,
)

from ..common import async_discover_unclaimed_mqtt_lock
from ..conftest import TEST_DOMAIN


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


async def test_mqtt_dispatch_skips_malformed_identifier(hass: HomeAssistant) -> None:
    """A malformed short identifier tuple is skipped, not treated as a crash."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt",), ("mqtt", "zwavejs2mqtt_0xd4ee5a7a_node20")},
        name="MalformedIdentifierLock",
    )
    # Identifiers are a set, so the well-formed one may be visited first and
    # never exercise the guard. A device carrying only a malformed tuple has
    # to walk through it -- and that tuple must not collide with the first
    # device's ("mqtt",), or the registry merges the two devices and the
    # well-formed identifier leaks into this one.
    malformed_only = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt_bridge",)},
        name="OnlyMalformedIdentifierLock",
    )

    assert resolve_provider_class("mqtt", device) is ZWaveJSUILock
    assert resolve_provider_class("mqtt", malformed_only) is None


def test_config_flow_platforms():
    """CONFIG_FLOW_PLATFORMS covers every shipped map key plus mqtt exactly once."""
    # The harness injects a mock provider into the map at runtime, while
    # CONFIG_FLOW_PLATFORMS is a tuple built from it at import. Only the
    # shipped platforms have to be selectable.
    assert set(INTEGRATIONS_CLASS_MAP) - {TEST_DOMAIN} <= set(CONFIG_FLOW_PLATFORMS)
    assert "mqtt" in CONFIG_FLOW_PLATFORMS
    assert len(CONFIG_FLOW_PLATFORMS) == len(set(CONFIG_FLOW_PLATFORMS))


async def test_factory_rejects_unclaimed_mqtt_lock(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """The lock factory refuses an mqtt lock no provider claims."""
    lock_entry = await async_discover_unclaimed_mqtt_lock(hass)
    lcm_entry = MockConfigEntry(domain=DOMAIN, unique_id="unclaimed-mqtt")
    lcm_entry.add_to_hass(hass)

    with pytest.raises(
        HomeAssistantError, match="No Lock Code Manager provider claims"
    ):
        async_create_lock_instance(
            hass,
            dr.async_get(hass),
            er.async_get(hass),
            lcm_entry,
            lock_entry.entity_id,
        )


async def test_allocation_skips_unclaimed_mqtt_lock(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """Allocation treats an unclaimed mqtt lock as one it will never write to."""
    lock_entry = await async_discover_unclaimed_mqtt_lock(hass)

    with pytest.raises(LockQuerySkipped) as raised:
        build_lock_instance(
            hass, dr.async_get(hass), er.async_get(hass), lock_entry.entity_id
        )

    assert raised.value.managed is False
