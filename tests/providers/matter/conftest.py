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
from custom_components.lock_code_manager.providers.matter import MatterLock

from .helpers import create_node_from_fixture, setup_matter_integration_with_node

MOCK_FABRIC_ID = 12341234
MOCK_COMPR_FABRIC_ID = 1234

SIMPLE_LOCK_ENTITY_ID = "lock.matter_test_matter_lock"

# Module path where lock_helpers functions are imported in the provider
_PROVIDER_MODULE = "custom_components.lock_code_manager.providers.matter"


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
    entry = MockConfigEntry(domain="matter")
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
async def simple_lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Lock Code Manager config entry that manages slots 1 and 2.

    This is a lightweight entry for unit tests that don't need the full LCM
    setup path. It only adds slot configuration data so that managed_slots
    is populated on the provider.
    """
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


# ---------------------------------------------------------------------------
# E2E fixtures -- set up a full LCM config entry on top of the real Matter
# integration so the provider is discovered and initialised through the real
# async_setup_entry path.
# ---------------------------------------------------------------------------

MATTER_LCM_CONFIG_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
}


@pytest.fixture
async def lock_entity(hass: HomeAssistant, matter_node: MatterNode) -> er.RegistryEntry:
    """Return the lock entity registry entry created by the real Matter integration."""
    ent_reg = er.async_get(hass)
    lock_entries = [
        entry
        for entry in ent_reg.entities.values()
        if entry.domain == "lock" and entry.platform == "matter"
    ]
    assert len(lock_entries) == 1, f"Expected 1 lock entity, found {len(lock_entries)}"
    return lock_entries[0]


@pytest.fixture
def matter_mock_helpers() -> dict[str, AsyncMock]:
    """Provide mock lock_helpers functions and patch them into the provider module.

    Returns a dict of helper name to AsyncMock so E2E tests can inspect
    call counts or swap side effects.
    """
    helpers: dict[str, AsyncMock] = {}

    # get_lock_info: called by MatterLock.async_setup() and availability checks
    helpers["get_lock_info"] = AsyncMock(
        return_value={
            "supports_user_management": True,
            "supported_credential_types": ["pin"],
        }
    )

    # get_lock_users: called by async_get_usercodes (coordinator refresh)
    helpers["get_lock_users"] = AsyncMock(
        return_value={
            "max_users": 10,
            "users": [],
        }
    )

    # set_lock_credential: called by async_set_usercode
    helpers["set_lock_credential"] = AsyncMock(
        return_value={"credential_index": 1, "user_index": 1},
    )

    # set_lock_user: called by async_set_usercode when a name is provided
    helpers["set_lock_user"] = AsyncMock(return_value={})

    # get_lock_credential_status: called by async_clear_usercode
    helpers["get_lock_credential_status"] = AsyncMock(
        return_value={"credential_exists": True}
    )

    # clear_lock_credential: called by async_clear_usercode
    helpers["clear_lock_credential"] = AsyncMock(return_value={})

    return helpers


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    matter_node: MatterNode,
    lock_entity: er.RegistryEntry,
    matter_mock_helpers: dict[str, AsyncMock],
) -> MockConfigEntry:
    """Set up a full LCM config entry managing the Matter lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the matter platform and instantiates MatterLock.
    The lock_helpers functions are patched at the provider module level.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: MATTER_LCM_CONFIG_SLOTS,
        },
        unique_id="test_matter_e2e",
    )
    lcm_entry.add_to_hass(hass)
    with (
        patch(
            f"{_PROVIDER_MODULE}.get_lock_info", matter_mock_helpers["get_lock_info"]
        ),
        patch(
            f"{_PROVIDER_MODULE}.get_lock_users",
            matter_mock_helpers["get_lock_users"],
        ),
        patch(
            f"{_PROVIDER_MODULE}.set_lock_credential",
            matter_mock_helpers["set_lock_credential"],
        ),
        patch(
            f"{_PROVIDER_MODULE}.set_lock_user",
            matter_mock_helpers["set_lock_user"],
        ),
        patch(
            f"{_PROVIDER_MODULE}.get_lock_credential_status",
            matter_mock_helpers["get_lock_credential_status"],
        ),
        patch(
            f"{_PROVIDER_MODULE}.clear_lock_credential",
            matter_mock_helpers["clear_lock_credential"],
        ),
    ):
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()

        yield lcm_entry

        await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_matter_lock(
    hass: HomeAssistant,
    lcm_entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
) -> MatterLock:
    """Extract the MatterLock from a loaded LCM config entry."""
    lock = lcm_entry.runtime_data.locks.get(lock_entity.entity_id)
    assert lock is not None, f"Lock {lock_entity.entity_id} not found in runtime data"
    assert isinstance(lock, MatterLock)
    return lock


@pytest.fixture
def e2e_matter_lock(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
) -> MatterLock:
    """Extract the MatterLock from the LCM config entry."""
    return get_matter_lock(hass, lcm_config_entry, lock_entity)
