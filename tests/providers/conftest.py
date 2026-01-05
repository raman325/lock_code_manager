"""Provide common provider fixtures for Lock Code Manager tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Generator
import copy
import itertools
import json
from pathlib import Path
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
import warnings

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zigpy.application import ControllerApplication
import zigpy.config
import zigpy.device
from zigpy.profiles import zha as zha_profile
import zigpy.quirks
import zigpy.state
import zigpy.types
from zigpy.zcl.clusters import closures, general
from zigpy.zcl.foundation import Status
import zigpy.zdo.types as zdo_t
from zwave_js_server.model.driver import Driver
from zwave_js_server.model.node import Node
from zwave_js_server.version import VersionInfo

from homeassistant.components.zha import const as zha_const
from homeassistant.components.zha.helpers import get_zha_gateway
from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

ZWAVE_JS_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "zwave_js"

# ZHA endpoint signature constants
SIG_EP_INPUT = 1
SIG_EP_OUTPUT = 2
SIG_EP_PROFILE = 3
SIG_EP_TYPE = 4


def load_json_fixture(filename: str, provider: str = "zwave_js") -> dict[str, Any]:
    """Load a fixture JSON file for a provider."""
    fixtures_dir = Path(__file__).parent / "fixtures" / provider
    with open(fixtures_dir / filename, encoding="utf-8") as f:
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


@pytest.fixture(name="mock_usb", autouse=True)
def mock_usb_fixture() -> Generator[None]:
    """Mock the USB component to avoid aiousbwatcher dependency."""
    # Create a mock aiousbwatcher module if it doesn't exist
    if "aiousbwatcher" not in sys.modules:
        mock_aiousbwatcher = MagicMock()
        mock_aiousbwatcher.AIOUSBWatcher = MagicMock()
        mock_aiousbwatcher.InotifyNotAvailableError = Exception
        sys.modules["aiousbwatcher"] = mock_aiousbwatcher
    yield


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


# =============================================================================
# ZHA Fixtures
# =============================================================================


@pytest.fixture
def mock_zha_radio_delays() -> Generator[None]:
    """Mock ZHA radio manager delays to speed up tests."""
    with (
        patch(
            "homeassistant.components.zha.radio_manager.CONNECT_DELAY_S",
            0,
        ),
        patch(
            "homeassistant.components.zha.radio_manager.RETRY_DELAY_S",
            0,
        ),
    ):
        yield


class _FakeZigbeeApp(ControllerApplication):
    """Fake Zigbee application controller for testing."""

    async def add_endpoint(self, descriptor: zdo_t.SimpleDescriptor):
        """Add endpoint."""

    async def connect(self):
        """Connect."""

    async def disconnect(self):
        """Disconnect."""

    async def force_remove(self, dev: zigpy.device.Device):
        """Force remove device."""

    async def load_network_info(self, *, load_devices: bool = False):
        """Load network info."""

    async def permit_ncp(self, time_s: int = 60):
        """Permit NCP."""

    async def permit_with_link_key(
        self,
        node: zigpy.types.EUI64,
        link_key: zigpy.types.KeyData,
        time_s: int = 60,
    ):
        """Permit with link key."""

    async def reset_network_info(self):
        """Reset network info."""

    async def send_packet(self, packet: zigpy.types.ZigbeePacket):
        """Send packet."""

    async def start_network(self):
        """Start network."""

    async def write_network_info(
        self,
        *,
        network_info: zigpy.state.NetworkInfo,
        node_info: zigpy.state.NodeInfo,
    ) -> None:
        """Write network info."""

    async def request(
        self,
        device: zigpy.device.Device,
        profile: zigpy.types.uint16_t,
        cluster: zigpy.types.uint16_t,
        src_ep: zigpy.types.uint8_t,
        dst_ep: zigpy.types.uint8_t,
        sequence: zigpy.types.uint8_t,
        data: bytes,
        *,
        expect_reply: bool = True,
        use_ieee: bool = False,
        extended_timeout: bool = False,
    ):
        """Request."""

    async def move_network_to_channel(
        self, new_channel: int, *, num_broadcasts: int = 5
    ) -> None:
        """Move network to channel."""

    def _persist_coordinator_model_strings_in_db(self) -> None:
        """Persist coordinator model strings."""


def _wrap_mock_instance(obj: Any) -> MagicMock:
    """Auto-mock every attribute and method in an object."""
    mock = create_autospec(obj, spec_set=True, instance=True)

    for attr_name in dir(obj):
        if attr_name.startswith("__") and attr_name not in {"__getitem__"}:
            continue

        real_attr = getattr(obj, attr_name)
        mock_attr = getattr(mock, attr_name)

        if callable(real_attr) and not hasattr(real_attr, "__aenter__"):
            mock_attr.side_effect = real_attr
        else:
            setattr(mock, attr_name, real_attr)

    return mock


def patch_zha_cluster(cluster):
    """Patch a ZHA cluster for testing."""
    cluster.PLUGGED_ATTR_READS = {}
    cluster.bind = AsyncMock(return_value=[0])
    cluster.configure_reporting = AsyncMock(return_value=[[]])
    cluster.configure_reporting_multiple = AsyncMock(return_value=[])
    cluster.handle_cluster_request = MagicMock()
    cluster.read_attributes = AsyncMock(return_value=[{}, {}])
    cluster.read_attributes_raw = AsyncMock(return_value=[])
    cluster.unbind = AsyncMock(return_value=[0])
    cluster.write_attributes = AsyncMock(return_value=[])
    cluster._write_attributes = AsyncMock(return_value=[])


@pytest.fixture
async def zigpy_app_controller():
    """Zigpy ApplicationController fixture."""
    app = _FakeZigbeeApp(
        {
            zigpy.config.CONF_DATABASE: None,
            zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: "/dev/null"},
            zigpy.config.CONF_STARTUP_ENERGY_SCAN: False,
            zigpy.config.CONF_NWK_BACKUP_ENABLED: False,
            zigpy.config.CONF_TOPO_SCAN_ENABLED: False,
            zigpy.config.CONF_OTA: {
                zigpy.config.CONF_OTA_ENABLED: False,
            },
        }
    )

    app.state.node_info.nwk = 0x0000
    app.state.node_info.ieee = zigpy.types.EUI64.convert("00:15:8d:00:02:32:4f:32")
    app.state.node_info.manufacturer = "Coordinator Manufacturer"
    app.state.node_info.model = "Coordinator Model"
    app.state.network_info.pan_id = 0x1234
    app.state.network_info.extended_pan_id = app.state.node_info.ieee
    app.state.network_info.channel = 15
    app.state.network_info.network_key.key = zigpy.types.KeyData(range(16))
    app.state.counters = zigpy.state.CounterGroups()

    # Create a fake coordinator device
    dev = app.add_device(nwk=app.state.node_info.nwk, ieee=app.state.node_info.ieee)
    dev.node_desc = zdo_t.NodeDescriptor()
    dev.node_desc.logical_type = zdo_t.LogicalType.Coordinator
    dev.manufacturer = "Coordinator Manufacturer"
    dev.model = "Coordinator Model"

    ep = dev.add_endpoint(1)
    ep.profile_id = zha_profile.PROFILE_ID
    ep.add_input_cluster(general.Basic.cluster_id)
    ep.add_input_cluster(general.Groups.cluster_id)

    with patch("zigpy.device.Device.request", return_value=[Status.SUCCESS]):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            mock_app = _wrap_mock_instance(app)
            mock_app.backups = _wrap_mock_instance(app.backups)

            # Ensure _concurrent_requests_semaphore has a proper max_value
            # This is needed by ZHA gateway's radio_concurrency property
            mock_semaphore = MagicMock()
            mock_semaphore.max_value = 8  # Default concurrent requests limit
            mock_app._concurrent_requests_semaphore = mock_semaphore

        yield mock_app


@pytest.fixture(name="zha_config_entry")
async def zha_config_entry_fixture() -> MockConfigEntry:
    """Fixture representing a ZHA config entry."""
    return MockConfigEntry(
        version=5,
        domain=zha_const.DOMAIN,
        data={
            zigpy.config.CONF_DEVICE: {
                zigpy.config.CONF_DEVICE_PATH: "/dev/ttyUSB0",
                zigpy.config.CONF_DEVICE_BAUDRATE: 115200,
                zigpy.config.CONF_DEVICE_FLOW_CONTROL: "hardware",
            },
            zha_const.CONF_RADIO_TYPE: "ezsp",
        },
        options={},
    )


@pytest.fixture
def mock_zigpy_connect(
    zigpy_app_controller: ControllerApplication,
) -> Generator[ControllerApplication]:
    """Patch the zigpy radio connection with our mock application."""
    with (
        patch(
            "bellows.zigbee.application.ControllerApplication.new",
            return_value=zigpy_app_controller,
        ),
        patch(
            "bellows.zigbee.application.ControllerApplication",
            return_value=zigpy_app_controller,
        ),
    ):
        yield zigpy_app_controller


@pytest.fixture
def setup_zha(
    hass: HomeAssistant,
    zha_config_entry: MockConfigEntry,
    mock_zigpy_connect: ControllerApplication,
    mock_zha_radio_delays: None,
) -> Callable[..., Coroutine[None]]:
    """Set up ZHA component."""

    async def _setup(config=None) -> None:
        zha_config_entry.add_to_hass(hass)
        config = config or {}

        # Only set up lock platform to speed up tests
        with patch(
            "homeassistant.components.zha.PLATFORMS",
            (Platform.DEVICE_TRACKER, Platform.LOCK, Platform.SENSOR),
        ):
            status = await async_setup_component(
                hass,
                zha_const.DOMAIN,
                {zha_const.DOMAIN: {zha_const.CONF_ENABLE_QUIRKS: False, **config}},
            )
            assert status is True
            await hass.async_block_till_done()

    return _setup


@pytest.fixture
def zigpy_device_mock(
    zigpy_app_controller,
) -> Callable[..., zigpy.device.Device]:
    """Make a fake device using the specified cluster classes."""

    def _mock_dev(
        endpoints,
        ieee="00:0d:6f:00:0a:90:69:e7",
        manufacturer="FakeManufacturer",
        model="FakeModel",
        node_descriptor=b"\x02@\x807\x10\x7fd\x00\x00*d\x00\x00",
        nwk=0xB79C,
        patch_cluster_flag=True,
    ):
        """Make a fake device using the specified cluster classes."""
        device = zigpy.device.Device(
            zigpy_app_controller, zigpy.types.EUI64.convert(ieee), nwk
        )
        device.manufacturer = manufacturer
        device.model = model
        device.node_desc = zdo_t.NodeDescriptor.deserialize(node_descriptor)[0]
        device.last_seen = time.time()

        for epid, ep in endpoints.items():
            endpoint = device.add_endpoint(epid)
            endpoint.device_type = ep[SIG_EP_TYPE]
            endpoint.profile_id = ep.get(SIG_EP_PROFILE, 0x0104)
            endpoint.request = AsyncMock()

            for cluster_id in ep.get(SIG_EP_INPUT, []):
                endpoint.add_input_cluster(cluster_id)

            for cluster_id in ep.get(SIG_EP_OUTPUT, []):
                endpoint.add_output_cluster(cluster_id)

        device.status = zigpy.device.Status.ENDPOINTS_INIT

        # Allow zigpy to apply quirks
        device = zigpy.quirks.get_device(device)

        if patch_cluster_flag:
            for endpoint in (ep for epid, ep in device.endpoints.items() if epid):
                endpoint.request = AsyncMock(return_value=[0])
                for cluster in itertools.chain(
                    endpoint.in_clusters.values(), endpoint.out_clusters.values()
                ):
                    patch_zha_cluster(cluster)

        return device

    return _mock_dev


@pytest.fixture
def zigpy_lock_device(
    zigpy_device_mock: Callable[..., zigpy.device.Device],
) -> zigpy.device.Device:
    """Create a mock Zigbee lock device."""
    return zigpy_device_mock(
        {
            1: {
                SIG_EP_INPUT: [
                    closures.DoorLock.cluster_id,
                    general.Basic.cluster_id,
                ],
                SIG_EP_OUTPUT: [],
                SIG_EP_TYPE: zha_profile.DeviceType.DOOR_LOCK,
                SIG_EP_PROFILE: zha_profile.PROFILE_ID,
            }
        },
        ieee="01:2d:6f:00:0a:90:69:e8",
        manufacturer="Yale",
        model="YRD256",
    )


@pytest.fixture
async def zha_lock_entity(
    hass: HomeAssistant,
    setup_zha: Callable[..., Coroutine[None]],
    zigpy_lock_device: zigpy.device.Device,
    zha_config_entry: MockConfigEntry,
) -> er.RegistryEntry:
    """Set up ZHA with a lock device and return the lock entity."""
    await setup_zha()
    gateway = get_zha_gateway(hass)

    gateway.get_or_create_device(zigpy_lock_device)
    await gateway.async_device_initialized(zigpy_lock_device)
    await hass.async_block_till_done(wait_background_tasks=True)

    ent_reg = er.async_get(hass)
    entries = list(
        er.async_entries_for_config_entry(ent_reg, zha_config_entry.entry_id)
    )
    lock_entries = [e for e in entries if e.domain == "lock"]
    assert len(lock_entries) == 1, f"Expected 1 lock entity, found {len(lock_entries)}"
    return lock_entries[0]
