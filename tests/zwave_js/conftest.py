"""Fixtures for lock_code_manager tests."""

import asyncio
import copy
import json
from unittest.mock import DEFAULT, AsyncMock, patch

import pytest
from zwave_js_server.model.driver import Driver
from zwave_js_server.model.node import Node
from zwave_js_server.version import VersionInfo

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.helpers import load_fixture


# Z-Wave JS fixtures


@pytest.fixture(name="controller_state", scope="session")
def controller_state_fixture():
    """Load the controller state fixture data."""
    return json.loads(load_fixture("controller_state.json"))


@pytest.fixture(name="controller_node_state", scope="session")
def controller_node_state_fixture():
    """Load the controller node state fixture data."""
    return json.loads(load_fixture("controller_node_state.json"))


@pytest.fixture(name="version_state", scope="session")
def version_state_fixture():
    """Load the version state fixture data."""
    return {
        "type": "version",
        "driverVersion": "6.0.0-beta.0",
        "serverVersion": "1.0.0",
        "homeId": 1234567890,
    }


@pytest.fixture(name="log_config_state")
def log_config_state_fixture():
    """Return log config state fixture data."""
    return {
        "enabled": True,
        "level": "info",
        "logToFile": False,
        "filename": "",
        "forceConsole": False,
    }


@pytest.fixture(name="lock_schlage_be469_state", scope="session")
def lock_schlage_be469_state_fixture():
    """Load the schlage lock node state fixture data."""
    return json.loads(
        load_fixture("lock_schlage_be469_state.json")
    )


@pytest.fixture(name="lock_august_asl03_state", scope="session")
def lock_august_asl03_state_fixture():
    """Load the August Pro lock node state fixture data."""
    return json.loads(
        load_fixture("lock_august_asl03_state.json")
    )


@pytest.fixture(name="lock_id_lock_as_id150_state", scope="session")
def lock_id_lock_as_id150_state_fixture():
    """Load the id lock id-150 lock node state fixture data."""
    return json.loads(
        load_fixture("/lock_id_lock_as_id150_state.json")
    )


# model fixtures


@pytest.fixture(name="listen_block")
def mock_listen_block_fixture():
    """Mock a listen block."""
    return asyncio.Event()


@pytest.fixture(name="client")
def mock_client_fixture(
    controller_state,
    controller_node_state,
    version_state,
    log_config_state,
    listen_block,
):
    """Mock a client."""
    with patch(
        "homeassistant.components.zwave_js.ZwaveClient", autospec=True
    ) as client_class:
        client = client_class.return_value

        async def connect():
            await asyncio.sleep(0)
            client.connected = True

        async def listen(driver_ready: asyncio.Event) -> None:
            driver_ready.set()
            await listen_block.wait()

        async def disconnect():
            client.connected = False

        client.connect = AsyncMock(side_effect=connect)
        client.listen = AsyncMock(side_effect=listen)
        client.disconnect = AsyncMock(side_effect=disconnect)
        client.driver = Driver(
            client, copy.deepcopy(controller_state), copy.deepcopy(log_config_state)
        )
        node = Node(client, copy.deepcopy(controller_node_state))
        client.driver.controller.nodes[node.node_id] = node

        client.version = VersionInfo.from_message(version_state)
        client.ws_server_url = "ws://test:3000/zjs"

        async def async_send_command_side_effect(message, require_schema=None):
            """Return the command response."""
            if message["command"] == "node.has_device_config_changed":
                return {"changed": False}
            return DEFAULT

        client.async_send_command.return_value = {
            "result": {"success": True, "status": 255}
        }
        client.async_send_command.side_effect = async_send_command_side_effect

        yield client


@pytest.fixture(name="lock_schlage_be469")
def lock_schlage_be469_fixture(client, lock_schlage_be469_state):
    """Mock a schlage lock node."""
    node = Node(client, copy.deepcopy(lock_schlage_be469_state))
    client.driver.controller.nodes[node.node_id] = node
    return node


@pytest.fixture(name="lock_august_pro")
def lock_august_asl03_fixture(client, lock_august_asl03_state):
    """Mock a August Pro lock node."""
    node = Node(client, copy.deepcopy(lock_august_asl03_state))
    client.driver.controller.nodes[node.node_id] = node
    return node


@pytest.fixture(name="lock_id_lock_as_id150")
def lock_id_lock_as_id150(client, lock_id_lock_as_id150_state):
    """Mock an id lock id-150 lock node."""
    node = Node(client, copy.deepcopy(lock_id_lock_as_id150_state))
    client.driver.controller.nodes[node.node_id] = node
    return node


@pytest.fixture(name="integration")
async def integration_fixture(hass: HomeAssistant, client):
    """Set up the zwave_js integration."""
    entry = MockConfigEntry(domain="zwave_js", data={"url": "ws://test.org"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    client.async_send_command.reset_mock()

    return entry


@pytest.fixture(name="lock_id_lock_as_id150_not_ready")
def node_not_ready(client, lock_id_lock_as_id150_state):
    """Mock an id lock id-150 lock node that's not ready."""
    state = copy.deepcopy(lock_id_lock_as_id150_state)
    state["ready"] = False
    node = Node(client, state)
    client.driver.controller.nodes[node.node_id] = node
    return node
