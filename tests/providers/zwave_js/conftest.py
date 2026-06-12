"""Provide common Z-Wave JS fixtures for Lock Code Manager tests."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const.command_class.access_control import (
    SetCredentialResult,
    UserCredentialType,
)
from zwave_js_server.exceptions import NotFoundError
from zwave_js_server.model.driver import Driver
from zwave_js_server.model.node import Node
from zwave_js_server.version import VersionInfo

from homeassistant.components.zwave_js import lock_helpers
from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_json_fixture(filename: str) -> dict[str, Any]:
    """Load a fixture JSON file."""
    with (FIXTURES_DIR / filename).open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(name="controller_state")
def controller_state_fixture() -> dict[str, Any]:
    """Load the controller state fixture data."""
    return load_json_fixture("controller_state.json")


@pytest.fixture(name="controller_node_state")
def controller_node_state_fixture() -> dict[str, Any]:
    """Load the controller node state fixture data."""
    return load_json_fixture("controller_node_state.json")


@pytest.fixture(name="lock_schlage_be469_state")
def lock_schlage_be469_state_fixture() -> dict[str, Any]:
    """Load the Schlage lock node state fixture data."""
    return load_json_fixture("lock_schlage_be469_state.json")


@pytest.fixture(name="lock_schlage_be469_v2_state")
def lock_schlage_be469_v2_state_fixture() -> dict[str, Any]:
    """Load the Schlage lock node state with User Code CC V2."""
    return load_json_fixture("lock_schlage_be469_v2_state.json")


@pytest.fixture(name="lock_schlage_be469_v2")
def lock_schlage_be469_v2_fixture(
    zwave_client: MagicMock,
    lock_schlage_be469_v2_state: dict[str, Any],
) -> Node:
    """Mock a Schlage BE469 lock node with User Code CC V2."""
    node = Node(zwave_client, copy.deepcopy(lock_schlage_be469_v2_state))
    zwave_client.driver.controller.nodes[node.node_id] = node
    return node


@pytest.fixture(name="version_state")
def version_state_fixture() -> dict[str, Any]:
    """Load the version state fixture data."""
    return {
        "type": "version",
        "driverVersion": "6.0.0-beta.0",
        "serverVersion": "1.0.0",
        "homeId": 1234567890,
    }


@pytest.fixture(name="log_config_state")
def log_config_state_fixture() -> dict[str, Any]:
    """Return log config state fixture data."""
    return {
        "enabled": True,
        "level": "info",
        "logToFile": False,
        "filename": "",
        "forceConsole": False,
    }


@pytest.fixture(name="listen_block")
def mock_listen_block_fixture() -> asyncio.Event:
    """Mock a listen block."""
    return asyncio.Event()


@pytest.fixture(name="zwave_client")
def mock_zwave_client_fixture(
    controller_state: dict[str, Any],
    controller_node_state: dict[str, Any],
    version_state: dict[str, Any],
    log_config_state: dict[str, Any],
    listen_block: asyncio.Event,
) -> Generator[MagicMock]:
    """Mock a Z-Wave JS client."""
    with patch(
        "homeassistant.components.zwave_js.ZwaveClient", autospec=True
    ) as client_class:
        client = client_class.return_value

        async def connect():
            listen_block.clear()
            await asyncio.sleep(0)
            client.connected = True

        async def listen(driver_ready: asyncio.Event) -> None:
            driver_ready.set()
            await listen_block.wait()

        async def disconnect():
            listen_block.set()
            client.connected = False

        client.connect = AsyncMock(side_effect=connect)
        client.listen = AsyncMock(side_effect=listen)
        client.disconnect = AsyncMock(side_effect=disconnect)
        client.disable_server_logging = MagicMock()
        client.driver = Driver(
            client, copy.deepcopy(controller_state), copy.deepcopy(log_config_state)
        )
        node = Node(client, copy.deepcopy(controller_node_state))
        client.driver.controller.nodes[node.node_id] = node

        client.version = VersionInfo.from_message(version_state)
        client.ws_server_url = "ws://test:3000/zjs"
        client.connected = True

        async def async_send_command_side_effect(message, require_schema=None):
            """Return the command response."""
            if message["command"] == "node.has_device_config_changed":
                return {"changed": False}
            if message["command"] == "endpoint.invoke_cc_api":
                return {"response": None}
            if message["command"] in (
                "endpoint.access_control.get_users_cached",
                "endpoint.access_control.get_users",
            ):
                return {"users": []}
            if message["command"] in (
                "endpoint.access_control.get_all_credentials_cached",
                "endpoint.access_control.get_all_credentials",
            ):
                return {"credentials": []}
            if message["command"] == "endpoint.access_control.get_user_cached":
                return {"user": None}
            if message["command"] == "endpoint.access_control.is_supported":
                return {"supported": True}
            if message["command"] in (
                "endpoint.access_control.set_user",
                "endpoint.access_control.delete_user",
            ):
                return {"result": {"success": True, "userId": message.get("userId", 1)}}
            if message["command"] in (
                "endpoint.access_control.set_credential",
                "endpoint.access_control.delete_credential",
            ):
                return {"result": {"success": True}}
            return {"result": {"success": True, "status": 255}}

        client.async_send_command = AsyncMock(
            side_effect=async_send_command_side_effect
        )
        client.async_send_command.return_value = {
            "result": {"success": True, "status": 255}
        }

        yield client


@pytest.fixture(name="server_version_side_effect")
def server_version_side_effect_fixture() -> Any | None:
    """Return the server version side effect."""
    return None


@pytest.fixture(name="get_server_version", autouse=True)
def mock_get_server_version(
    server_version_side_effect: Any | None,
) -> Generator[AsyncMock]:
    """Mock server version."""
    version_info = VersionInfo(
        driver_version="mock-driver-version",
        server_version="mock-server-version",
        home_id=1234,
        min_schema_version=0,
        max_schema_version=1,
    )
    with patch(
        "homeassistant.components.zwave_js.helpers.get_server_version",
        side_effect=server_version_side_effect,
        return_value=version_info,
    ) as mock_version:
        yield mock_version


@pytest.fixture(name="lock_schlage_be469")
def lock_schlage_be469_fixture(
    zwave_client: MagicMock,
    lock_schlage_be469_state: dict[str, Any],
) -> Node:
    """Mock a Schlage BE469 lock node."""
    node = Node(zwave_client, copy.deepcopy(lock_schlage_be469_state))
    zwave_client.driver.controller.nodes[node.node_id] = node
    return node


@pytest.fixture(name="zwave_integration")
async def zwave_integration_fixture(
    hass: HomeAssistant,
    zwave_client: MagicMock,
    lock_schlage_be469: Node,
) -> MockConfigEntry:
    """Set up the zwave_js integration with a lock."""
    entry = MockConfigEntry(
        domain=ZWAVE_JS_DOMAIN,
        data={"url": "ws://test.org"},
        unique_id=str(zwave_client.driver.controller.home_id),
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    zwave_client.async_send_command.reset_mock()
    return entry


@pytest.fixture(name="lock_entity")
async def lock_entity_fixture(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> er.RegistryEntry:
    """Return the lock entity registry entry."""
    ent_reg = er.async_get(hass)
    # Find the lock entity created by the integration
    entries = er.async_entries_for_config_entry(ent_reg, zwave_integration.entry_id)
    lock_entries = [e for e in entries if e.domain == "lock"]
    assert len(lock_entries) == 1, f"Expected 1 lock entity, found {len(lock_entries)}"
    return lock_entries[0]


# ---------------------------------------------------------------------------
# E2E fixtures — set up a full LCM config entry on top of the Z-Wave JS
# integration so the provider is discovered and initialised through the real
# async_setup_entry path.
# ---------------------------------------------------------------------------

# LCM config: one Z-Wave JS lock, two slots
ZWAVE_JS_LCM_CONFIG_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "9999", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "1234", CONF_ENABLED: True},
}


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> MockConfigEntry:
    """
    Set up a full LCM config entry managing the Z-Wave JS lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the zwave_js platform and instantiates ZWaveJSLock.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS,
        },
        unique_id="test_zwave_js_e2e",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_zwave_lock(
    hass: HomeAssistant,
    lcm_entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
) -> ZWaveJSLock:
    """Extract the ZWaveJSLock from a loaded LCM config entry."""
    lock = lcm_entry.runtime_data.locks.get(lock_entity.entity_id)
    assert lock is not None, f"Lock {lock_entity.entity_id} not found in runtime data"
    assert isinstance(lock, ZWaveJSLock)
    return lock


@pytest.fixture
def e2e_zwave_lock(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
) -> ZWaveJSLock:
    """Extract the ZWaveJSLock from the LCM config entry."""
    return get_zwave_lock(hass, lcm_config_entry, lock_entity)


# ---------------------------------------------------------------------------
# Shared provider fixtures (used by both test_provider.py and test_events.py)
# ---------------------------------------------------------------------------


@pytest.fixture(name="zwave_js_lock")
async def zwave_js_lock_fixture(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
) -> ZWaveJSLock:
    """Create a ZWaveJSLock instance (User Code CC V1) for testing."""
    return ZWaveJSLock(
        hass=hass,
        dev_reg=dr.async_get(hass),
        ent_reg=er.async_get(hass),
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )


@pytest.fixture(name="zwave_js_lock_v2")
async def zwave_js_lock_v2_fixture(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469_v2: Node,
) -> ZWaveJSLock:
    """Create a ZWaveJSLock with User Code CC V2 for testing."""
    return ZWaveJSLock(
        hass=hass,
        dev_reg=dr.async_get(hass),
        ent_reg=er.async_get(hass),
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )


@pytest.fixture
def mock_coordinator():
    """
    Return a factory for mock coordinators preloaded with slot state.

    Replaces the repeated ``MagicMock(); .data = {...}; lock.coordinator = ...``
    pattern. Call with a slot->SlotCredential dict; assign the result to
    ``lock.coordinator``.
    """

    def _make(data: dict[int, SlotCredential] | None = None) -> MagicMock:
        coordinator = MagicMock()
        coordinator.data = data or {}
        return coordinator

    return _make


@pytest.fixture
async def simple_lcm_config_entry(
    hass: HomeAssistant,
    lock_entity: er.RegistryEntry,
) -> MockConfigEntry:
    """
    Register a lightweight LCM config entry managing slots 1 and 2.

    Mirrors the Matter provider's ``simple_lcm_config_entry``: it only adds slot
    configuration data so ``managed_slots`` is populated on the provider. It does
    NOT go through ``async_setup_internal`` — method-level tests that need
    managed slots use this instead of the setup/unload lifecycle boilerplate.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: {
                1: {CONF_NAME: "slot1", CONF_PIN: "9999", CONF_ENABLED: True},
                2: {CONF_NAME: "slot2", CONF_PIN: "1234", CONF_ENABLED: True},
            },
        },
        unique_id="test_zwave_js_simple_lcm",
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_lock_helpers():
    """Patch the write/capability lock_helpers the provider calls."""
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    mocks = {
        "async_get_credential_capabilities": AsyncMock(
            return_value={
                "supports_user_management": True,
                "max_users": 30,
                "supported_user_types": [],
                "max_user_name_length": 10,
                "supported_credential_rules": [],
                "supported_credential_types": {
                    pin_type_str: {
                        "num_slots": 30,
                        "min_length": 4,
                        "max_length": 8,
                        "supports_learn": False,
                    }
                },
            }
        ),
        "async_set_user": AsyncMock(return_value={"user_id": 1}),
        "async_delete_user": AsyncMock(),
        "async_set_credential": AsyncMock(
            return_value={"credential_slot": 1, "user_id": 1}
        ),
        "async_delete_credential": AsyncMock(),
    }
    with patch.multiple(
        "custom_components.lock_code_manager.providers.zwave_js.lock_helpers",
        **mocks,
    ):
        yield mocks


@pytest.fixture
def mock_access_control(lock_schlage_be469: Node):
    """
    Give the node a mock access_control with READ + WRITE methods.

    ``access_control`` is a property on ``Node``, so this patches it at the
    CLASS level for the fixture's scope -- every ``Node`` instance sees the
    mock while the fixture is active, not just ``lock_schlage_be469``.

    The provider's unified-mode reads go through this object; its write
    primitives are mocked too so UC-fallback tests can assert they are
    NOT called (UC mode must route through the User Code CC utilities
    instead -- see issue #1251).
    """
    ac = MagicMock()
    ac.get_user_cached = AsyncMock(return_value=None)
    ac.get_users_cached = AsyncMock(return_value=[])
    ac.get_all_credentials_cached = AsyncMock(return_value=[])
    ac.get_users = AsyncMock(return_value=[])
    ac.get_all_credentials = AsyncMock(return_value=[])
    ac.set_credential = AsyncMock(return_value=SetCredentialResult.OK)
    ac.delete_credential = AsyncMock(return_value=SetCredentialResult.OK)
    with patch.object(type(lock_schlage_be469), "access_control", ac):
        yield ac


def uc_only_caps_response() -> dict:
    """Return a degenerate credential-capabilities response (issue #1251).

    This is what ``lock_helpers.async_get_credential_capabilities`` returns
    when the unified ``access_control`` API has no usable PIN data for the
    lock: the helper hardcodes ``supports_user_management=True`` but the
    PIN credential type is missing (or advertises ``num_slots=0``).
    """
    return {
        "supports_user_management": True,
        "max_users": 0,
        "supported_user_types": [],
        "max_user_name_length": 0,
        "supported_credential_rules": [],
        "supported_credential_types": {},
    }


def uc_slot_walk(
    num_slots: int, occupied: dict[int, str | None] | None = None
) -> list[dict]:
    """Build a fake ``get_usercodes`` value-DB walk.

    ``occupied`` maps slot -> usercode (None for an occupied slot whose
    code is not cached); all other slots are returned as not in use.
    """
    occupied = occupied or {}
    return [
        {
            "code_slot": slot,
            "name": f"Slot {slot}",
            "in_use": slot in occupied,
            "usercode": occupied.get(slot),
        }
        for slot in range(1, num_slots + 1)
    ]


@pytest.fixture
def mock_uc_utils() -> Generator[dict]:
    """Patch the User Code CC utilities the UC-fallback path calls.

    ``get_usercode`` defaults to raising ``NotFoundError`` (no cached
    value), which the provider treats as "proceed with the write".
    """
    mocks = {
        "get_usercodes": MagicMock(return_value=[]),
        "get_usercode": MagicMock(side_effect=NotFoundError("no cached value")),
        "get_usercode_from_node": AsyncMock(),
        "set_usercode": AsyncMock(return_value=None),
        "clear_usercode": AsyncMock(return_value=None),
    }
    with patch.multiple(
        "custom_components.lock_code_manager.providers.zwave_js", **mocks
    ):
        yield mocks


@pytest.fixture
def uc_fallback_lock(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> ZWaveJSLock:
    """Arrange a lock whose capability probe lands in UC-fallback mode.

    The unified API reports no usable PIN capabilities while the User
    Code CC value DB walk finds 30 slots. Detection itself runs lazily
    on the first capability probe or credential operation.
    """
    mock_lock_helpers[
        "async_get_credential_capabilities"
    ].return_value = uc_only_caps_response()
    mock_uc_utils["get_usercodes"].return_value = uc_slot_walk(30)
    return zwave_js_lock
