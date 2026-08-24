"""Shared fixtures and constants for zwave-js-ui provider tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Generator
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import DEFAULT, MagicMock, patch

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


# Stand-ins for the production budgets, scaled so a whole suite of unanswered
# calls costs a fraction of a second. Kept an order of magnitude apart for the
# same reason the real ones are: a test that pins which budget an api uses can
# only tell them apart if they differ.
FAST_API_CALL_TIMEOUT = 0.5
FAST_GATEWAY_LOCAL_TIMEOUT = 0.05


@pytest.fixture(autouse=True)
def fast_zui_timeouts() -> Generator[None]:
    """Keep the discovery window and api timeouts from costing tests whole seconds."""
    module = "custom_components.lock_code_manager.providers.zwave_js_ui"
    with (
        patch(f"{module}.GATEWAY_DISCOVERY_TIMEOUT", 0.05),
        patch(f"{module}.API_CALL_TIMEOUT", FAST_API_CALL_TIMEOUT),
        patch(f"{module}.GATEWAY_LOCAL_TIMEOUT", FAST_GATEWAY_LOCAL_TIMEOUT),
    ):
        yield


class ZWaveJSUIApiResponder:
    """
    Answer zwave-js-ui api requests the way the gateway would.

    The mocked broker never echoes publishes back, so an api call that nobody
    answers blocks until its timeout. Handlers are registered per api name and
    receive ``(api_base, request payload)``; the api base is what lets one
    instance stand in for several gateways at once. An api name with no
    handler goes unanswered, the same as an api the gateway does not
    implement.

    The response is published on the request topic minus ``/set`` with the
    request payload echoed verbatim under ``origin`` -- the correlation
    channel the client matches its nonce on. Faking that echo would make the
    client's own correlation untestable, so it is always the real request.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize with no handlers registered."""
        self.hass = hass
        self.handlers: dict[
            str, Callable[[str, dict[str, Any]], dict[str, Any] | None]
        ] = {}
        # (api_base, api_name, args) per request seen, in publish order.
        self.requests: list[tuple[str, str, list[Any]]] = []

    def set_handler(
        self,
        api_name: str,
        handler: Callable[[str, dict[str, Any]], dict[str, Any] | None],
    ) -> None:
        """Answer ``api_name`` with whatever the handler builds (None: silence)."""
        self.handlers[api_name] = handler

    def set_result(
        self, api_name: str, result: Any, *, success: bool = True, message: str = ""
    ) -> None:
        """Answer ``api_name`` with one fixed response regardless of the request."""
        self.set_handler(
            api_name,
            lambda _api_base, _request: {
                "success": success,
                "message": message,
                "result": result,
            },
        )

    def __call__(self, topic: str, payload: str, *args: Any, **kwargs: Any) -> Any:
        """Intercept an outbound publish, answering it when it is an api request."""
        if not topic.endswith("/set"):
            return DEFAULT
        response_topic = topic.removesuffix("/set")
        api_base, separator, api_name = response_topic.rpartition("/api/")
        if not separator:
            return DEFAULT
        try:
            body = json.loads(payload)
        except json.JSONDecodeError, TypeError:
            return DEFAULT
        self.requests.append((api_base, api_name, body.get("args", [])))
        handler = self.handlers.get(api_name)
        if handler is None:
            return DEFAULT
        if (response := handler(api_base, body)) is not None:
            async_fire_mqtt_message(
                self.hass, response_topic, json.dumps({**response, "origin": body})
            )
        return DEFAULT


@pytest.fixture
def zui_api_responder(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> Generator[ZWaveJSUIApiResponder]:
    """
    Attach a gateway stand-in to outbound publishes.

    The hook lives on ``mqtt_mock.async_publish`` (the Home Assistant MQTT
    client mock) rather than the paho layer, and returns
    ``unittest.mock.DEFAULT`` so the wrapped real publish still runs.
    """
    responder = ZWaveJSUIApiResponder(hass)
    mqtt_mock.async_publish.side_effect = responder
    yield responder
    mqtt_mock.async_publish.side_effect = None


def fire_zui_gateway_status(
    hass: HomeAssistant, client: str = ZUI_GATEWAY_NAME, prefix: str = ZUI_PREFIX
) -> None:
    """Publish a gateway's retained client status the way zwave-js-ui does."""
    async_fire_mqtt_message(
        hass, f"{prefix}/_CLIENTS/{client}/status", json.dumps({"value": True})
    )


async def async_start_gateway_resolution(
    hass: HomeAssistant, lock: ZWaveJSUILock
) -> asyncio.Task[str]:
    """
    Start gateway resolution and return once it is listening for statuses.

    A real broker replays retained statuses on subscribe; the mocked one
    cannot, so the caller fires them by hand. That only works if resolution
    has already subscribed, hence the flush before the task is handed back.
    """
    task = asyncio.create_task(lock._async_resolve_api_base())
    await hass.async_block_till_done()
    return task


@pytest.fixture
async def zui_gateway_resolved(
    hass: HomeAssistant,
    zui_lock_provider: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> ZWaveJSUILock:
    """Build a provider whose gateway is already resolved via the real discovery path."""
    task = await async_start_gateway_resolution(hass, zui_lock_provider)
    fire_zui_gateway_status(hass)
    assert await task == ZUI_API_BASE
    return zui_lock_provider


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
