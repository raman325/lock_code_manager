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
from zwave_js_server.model.driver import Driver
from zwave_js_server.model.node import Node
from zwave_js_server.version import VersionInfo

from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_json_fixture(filename: str) -> dict[str, Any]:
    """Load a fixture JSON file."""
    with open(FIXTURES_DIR / filename, encoding="utf-8") as f:
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
    """Load the schlage lock node state fixture data."""
    return load_json_fixture("lock_schlage_be469_state.json")


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
