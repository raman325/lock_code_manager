"""ZHA provider test fixtures."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
import itertools
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

from homeassistant.components.zha import const as zha_const
from homeassistant.components.zha.helpers import get_zha_gateway
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.zha import ZHALock

# ZHA endpoint signature constants
SIG_EP_INPUT = 1
SIG_EP_OUTPUT = 2
SIG_EP_PROFILE = 3
SIG_EP_TYPE = 4

ZHA_LCM_CONFIG_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
}


# ---------------------------------------------------------------------------
# Zigpy / ZHA infrastructure fixtures
# ---------------------------------------------------------------------------


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


def _patch_zha_cluster(cluster):
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


@pytest.fixture(autouse=True)
def mock_usb() -> Generator[None]:
    """Mock the USB component to avoid aiousbwatcher dependency."""
    if "aiousbwatcher" not in sys.modules:
        mock_mod = MagicMock()
        mock_mod.AIOUSBWatcher = MagicMock()
        mock_mod.InotifyNotAvailableError = Exception
        sys.modules["aiousbwatcher"] = mock_mod
    yield


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
            zigpy.config.CONF_OTA: {zigpy.config.CONF_OTA_ENABLED: False},
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

            class MockSemaphore:
                max_value = 8
                max_concurrency = 8

            mock_app._concurrent_requests_semaphore = MockSemaphore()

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
) -> Callable[..., Awaitable[None]]:
    """Set up ZHA component."""

    async def _setup(config=None) -> None:
        zha_config_entry.add_to_hass(hass)
        config = config or {}
        with patch(
            "homeassistant.components.zha.PLATFORMS",
            (Platform.DEVICE_TRACKER, Platform.LOCK, Platform.SENSOR),
        ):
            status = await async_setup_component(
                hass,
                zha_const.DOMAIN,
                {
                    zha_const.DOMAIN: {
                        zha_const.CONF_ENABLE_QUIRKS: False,
                        **config,
                    }
                },
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
        device = zigpy.quirks.get_device(device)

        if patch_cluster_flag:
            for endpoint in (ep for epid, ep in device.endpoints.items() if epid):
                endpoint.request = AsyncMock(return_value=[0])
                for cluster in itertools.chain(
                    endpoint.in_clusters.values(),
                    endpoint.out_clusters.values(),
                ):
                    _patch_zha_cluster(cluster)

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
    setup_zha: Callable[..., Awaitable[None]],
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


# ---------------------------------------------------------------------------
# ZHALock fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(name="zha_lock")
async def zha_lock_fixture(
    hass: HomeAssistant,
    zha_lock_entity: er.RegistryEntry,
    zha_config_entry: MockConfigEntry,
) -> ZHALock:
    """Create a ZHALock instance for testing."""
    return ZHALock(
        hass=hass,
        dev_reg=dr.async_get(hass),
        ent_reg=er.async_get(hass),
        lock_config_entry=zha_config_entry,
        lock=zha_lock_entity,
    )


@pytest.fixture
async def simple_lcm_config_entry(
    hass: HomeAssistant, zha_lock_entity: er.RegistryEntry
) -> MockConfigEntry:
    """Lightweight LCM config entry for unit tests (no full setup)."""
    config = {
        CONF_LOCKS: [zha_lock_entity.entity_id],
        CONF_SLOTS: ZHA_LCM_CONFIG_SLOTS,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_zha_lcm")
    entry.add_to_hass(hass)
    return entry
