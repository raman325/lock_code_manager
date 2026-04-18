"""Matter provider test fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

from matter_server.client.models.node import MatterNode
from matter_server.common.const import SCHEMA_VERSION
from matter_server.common.models import ServerInfoMessage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.matter import (
    MATTER_DOMAIN,
    MatterLock,
)

from .helpers import create_node_from_fixture, setup_matter_integration_with_node

MOCK_FABRIC_ID = 12341234
MOCK_COMPR_FABRIC_ID = 1234

SIMPLE_LOCK_ENTITY_ID = "lock.matter_test_matter_lock"


@pytest.fixture(name="matter_client")
async def matter_client_fixture() -> AsyncGenerator[MagicMock]:
    """Fixture for a Matter client — mirrors HA's Matter conftest."""
    with patch(
        "homeassistant.components.matter.MatterClient", autospec=True
    ) as client_class:
        client = client_class.return_value

        async def connect() -> None:
            await asyncio.sleep(0)

        async def listen(init_ready: asyncio.Event | None) -> None:
            if init_ready is not None:
                init_ready.set()
            listen_block = asyncio.Event()
            await listen_block.wait()
            pytest.fail("Listen was not cancelled!")

        client.connect = AsyncMock(side_effect=connect)
        client.check_node_update = AsyncMock(return_value=None)
        client.start_listening = AsyncMock(side_effect=listen)
        client.server_info = ServerInfoMessage(
            fabric_id=MOCK_FABRIC_ID,
            compressed_fabric_id=MOCK_COMPR_FABRIC_ID,
            schema_version=1,
            sdk_version="2022.11.1",
            wifi_credentials_set=True,
            thread_credentials_set=True,
            min_supported_schema_version=SCHEMA_VERSION,
            bluetooth_enabled=False,
        )

        yield client


@pytest.fixture
async def matter_node(hass: HomeAssistant, matter_client: MagicMock) -> MatterNode:
    """Set up Matter integration with a mock door lock node."""
    node = create_node_from_fixture("mock_door_lock")
    await setup_matter_integration_with_node(hass, matter_client, node)
    return node


@pytest.fixture
async def matter_lock(hass: HomeAssistant, matter_node: MatterNode) -> MatterLock:
    """Create a MatterLock from the integration-created device."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    lock_entity = next(
        entry
        for entry in ent_reg.entities.values()
        if entry.domain == "lock" and entry.platform == "matter"
    )

    matter_entry = next(entry for entry in hass.config_entries.async_entries("matter"))

    return MatterLock(hass, dev_reg, ent_reg, matter_entry, lock_entity)


@pytest.fixture
async def matter_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a standalone Matter config entry (for tests that don't need the full integration)."""
    entry = MockConfigEntry(domain=MATTER_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def matter_lock_simple(
    hass: HomeAssistant, matter_config_entry: MockConfigEntry
) -> MatterLock:
    """Create a simple MatterLock without full Matter integration (for service-level tests)."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "matter",
        "test_matter_lock",
        config_entry=matter_config_entry,
    )
    return MatterLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        matter_config_entry,
        lock_entity,
    )


@pytest.fixture
async def lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Lock Code Manager config entry that manages slots 1 and 2."""
    config = {
        CONF_LOCKS: [SIMPLE_LOCK_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_matter_lcm")
    entry.add_to_hass(hass)
    return entry


# Aliases for shared test mixins
@pytest.fixture
def provider_lock(matter_lock_simple: MatterLock) -> MatterLock:
    """Alias for shared test mixins."""
    return matter_lock_simple
