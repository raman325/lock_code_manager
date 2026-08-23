"""Shared fixtures and constants for zwave-js-ui provider tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.providers.zwave_js_ui import ZWaveJSUILock

ZUI_PREFIX = "zwave"
ZUI_HOME_HEX = "0xd4ee5a7a"
ZUI_NODE_ID = 20
ZUI_NODE_TOPIC = f"{ZUI_PREFIX}/nodeID_{ZUI_NODE_ID}"
# Door Lock Command Class (98) / endpoint 0 / currentMode, the value a
# VALUEID gateway points a lock's discovery state_topic at.
ZUI_VALUE_PATH = "98/0/currentMode"
ZUI_STATE_TOPIC = f"{ZUI_NODE_TOPIC}/{ZUI_VALUE_PATH}"
ZUI_GATEWAY_NAME = "ZWAVE_GATEWAY-zui"
ZUI_API_BASE = f"{ZUI_PREFIX}/_CLIENTS/{ZUI_GATEWAY_NAME}"
ZUI_DEVICE_IDENTIFIER = f"zwavejs2mqtt_{ZUI_HOME_HEX}_node{ZUI_NODE_ID}"


def zui_lock_discovery_payload(
    *,
    home_hex: str = ZUI_HOME_HEX,
    node_id: int = ZUI_NODE_ID,
    prefix: str = ZUI_PREFIX,
    node_segment: str | None = None,
    value_path: str = ZUI_VALUE_PATH,
    state_topic: str | None = None,
    include_state_topic: bool = True,
) -> dict[str, Any]:
    """
    Build a zwave-js-ui-shaped Home Assistant discovery payload for a lock.

    Mirrors the gateway's ``hass devices`` lock template: state and command
    topics point at Door Lock Command Class value topics under the node topic.

    ``node_segment`` may itself contain slashes (``hallway/front_door``) so a
    NAMED gateway with a location is expressible, and ``value_path`` carries
    that gateway's ``lock/endpoint_0/currentMode`` spelling of the same value.
    ``state_topic`` overrides the derived topic verbatim, which is the only way
    to express a MANUAL gateway's arbitrary custom topic.
    """
    node_topic = f"{prefix}/{node_segment or f'nodeID_{node_id}'}"
    payload: dict[str, Any] = {
        "name": None,
        "command_topic": (
            f"{node_topic}/{value_path.replace('currentMode', 'targetMode')}/set"
        ),
        "payload_lock": "255",
        "payload_unlock": "0",
        "state_locked": "255",
        "state_unlocked": "0",
        "value_template": "{{ value_json.value }}",
        "unique_id": f"zwavejs2mqtt_{home_hex}_{node_id}-98-0-currentMode",
        "device": {
            "identifiers": [f"zwavejs2mqtt_{home_hex}_node{node_id}"],
            "name": f"nodeID_{node_id}",
            "manufacturer": "Test",
            "model": "Test lock",
        },
    }
    if include_state_topic:
        payload["state_topic"] = state_topic or f"{node_topic}/{value_path}"
    return payload


async def async_discover_zui_lock(
    hass: HomeAssistant, **payload_kwargs: Any
) -> er.RegistryEntry:
    """Fire a zwave-js-ui-style discovery config and return the created lock entity."""
    payload = zui_lock_discovery_payload(**payload_kwargs)
    unique_id = payload["unique_id"]
    async_fire_mqtt_message(
        hass, f"homeassistant/lock/{unique_id}/config", json.dumps(payload)
    )
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("lock", "mqtt", unique_id)
    assert entity_id is not None, "discovery did not create the lock entity"
    # Seed a state so the entity is available. zwave-js-ui wraps every value
    # publication in a metadata envelope; the discovery value_template unwraps it.
    if state_topic := payload.get("state_topic"):
        async_fire_mqtt_message(hass, state_topic, json.dumps({"value": 255}))
        await hass.async_block_till_done()
    entry = ent_reg.async_get(entity_id)
    assert entry is not None
    return entry


def build_zui_lock(hass: HomeAssistant, lock_entity: er.RegistryEntry) -> ZWaveJSUILock:
    """Construct a provider instance around a discovered lock entity."""
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    return ZWaveJSUILock(
        hass, dr.async_get(hass), er.async_get(hass), mqtt_entry, lock_entity
    )


def _minimal_lock() -> ZWaveJSUILock:
    """Build a ZWaveJSUILock without the Home Assistant test harness."""
    lock_entity = SimpleNamespace(
        entity_id="lock.test",
        device_id=None,
        platform="mqtt",
        config_entry_id=None,
        unique_id=None,
    )
    return ZWaveJSUILock(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        None,
        lock_entity,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def fast_gateway_discovery() -> Generator[None]:
    """Keep the gateway discovery wait from costing every test whole seconds."""
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js_ui."
        "GATEWAY_DISCOVERY_TIMEOUT",
        0.05,
    ):
        yield


@pytest.fixture
async def zui_lock_discovered(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> er.RegistryEntry:
    """zwave-js-ui lock entity created through real MQTT discovery."""
    return await async_discover_zui_lock(hass)


@pytest.fixture
async def zui_lock_provider(
    hass: HomeAssistant, zui_lock_discovered: er.RegistryEntry
) -> ZWaveJSUILock:
    """Build a provider instance around the discovered lock, with no LCM entry."""
    return build_zui_lock(hass, zui_lock_discovered)


@pytest.fixture
async def zui_lock_with_device(hass: HomeAssistant) -> ZWaveJSUILock:
    """
    Build a lock on a registry-created device that never went through discovery.

    The MQTT config entry is registered but the component is never set up, so
    this is also the shape a lock has while MQTT is still loading.
    """
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", ZUI_DEVICE_IDENTIFIER)},
        name=f"nodeID_{ZUI_NODE_ID}",
    )
    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "mqtt",
        "test_zui_no_discovery",
        config_entry=mqtt_entry,
        device_id=device.id,
    )
    return ZWaveJSUILock(hass, dev_reg, ent_reg, mqtt_entry, lock_entity)


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    zui_lock_discovered: er.RegistryEntry,
    mqtt_teardown,
) -> AsyncGenerator[MockConfigEntry]:
    """
    Set up a full LCM config entry managing the discovered zwave-js-ui lock.

    This runs the real async_setup_entry path, so the lock entity's mqtt
    platform and device identifier are what pick ZWaveJSUILock.
    """
    config = {
        CONF_LOCKS: [zui_lock_discovered.entity_id],
        CONF_SLOTS: {
            1: {"name": "slot1", "pin": "1234", "enabled": True},
            2: {"name": "slot2", "pin": "5678", "enabled": True},
        },
    }
    lcm_entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_zui")
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def zui_lock(lcm_config_entry: MockConfigEntry) -> ZWaveJSUILock:
    """Extract the ZWaveJSUILock from the loaded LCM config entry."""
    locks = lcm_config_entry.runtime_data.locks
    assert len(locks) == 1, f"Expected 1 lock, found {len(locks)}"
    lock = next(iter(locks.values()))
    assert isinstance(lock, ZWaveJSUILock)
    return lock
