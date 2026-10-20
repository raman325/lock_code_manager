"""Matter provider test fixtures."""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import DEFAULT, AsyncMock, MagicMock, patch

from matter_server.client.models.node import MatterNode
from matter_server.common.const import SCHEMA_VERSION
from matter_server.common.models import ServerInfoMessage
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.matter.const import CREDENTIAL_TYPE_REVERSE_MAP
from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers import matter as matter_provider
from custom_components.lock_code_manager.providers.matter import MatterLock

from ...common import user_subentries
from .fake_node import DoorLock, FakeMatterLock
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
    """
    Create a Lock Code Manager config entry that manages slots 1 and 2.

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
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="test_matter_lcm",
        subentries_data=user_subentries(config[CONF_SLOTS]),
    )
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
    """
    Provide mock lock_helpers functions and patch them into the provider module.

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

    # get_lock_users: called by async_get_usercodes / async_get_users (coordinator refresh)
    helpers["get_lock_users"] = AsyncMock(
        return_value={
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "user_name": "slot1",
                    "credentials": [{"type": "pin", "index": 1}],
                },
                {
                    "user_index": 2,
                    "user_name": "slot2",
                    "credentials": [{"type": "pin", "index": 2}],
                },
            ],
        }
    )

    # set_lock_credential: called by async_set_credential. Echoes the request,
    # as the real helper does, so a write for any slot is answered for its
    # own user.
    last_write: dict[str, Any] = {}

    async def _set_lock_credential(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        last_write.update(kwargs)
        return {
            "credential_index": kwargs.get("credential_index") or 1,
            "user_index": kwargs.get("user_index"),
        }

    helpers["set_lock_credential"] = AsyncMock(side_effect=_set_lock_credential)

    # get_lock_credential_status: the pairing check after a set asks which user
    # holds the written credential; the lock answers with the user it was
    # written for, so no honest write is judged misfiled.
    async def _get_lock_credential_status(
        *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        return {"credential_exists": True, "user_index": last_write.get("user_index")}

    helpers["get_lock_credential_status"] = AsyncMock(
        side_effect=_get_lock_credential_status
    )

    # set_lock_user: called by async_set_user
    helpers["set_lock_user"] = AsyncMock(return_value={"user_index": 1})

    # clear_lock_credential: called by async_delete_credential
    helpers["clear_lock_credential"] = AsyncMock(return_value={})

    # clear_lock_user: called by async_delete_user after last credential is removed
    helpers["clear_lock_user"] = AsyncMock(return_value=None)

    return helpers


@contextmanager
def matter_helpers_patched(
    helpers: dict[str, AsyncMock], client: MagicMock
) -> Iterator[None]:
    """
    Answer every Matter lock helper the provider calls from ``helpers``.

    Patched where the provider imports them, for as long as anything the test
    sets up -- an entry, or a flow creating one -- may call them. The
    provider's own GetUser, sent through ``client``, answers from the
    ``get_lock_users`` mock too, so a test sees one lock whichever way the
    provider reads it.
    """
    with ExitStack() as stack:
        for name, mock in helpers.items():
            stack.enter_context(patch(f"{_PROVIDER_MODULE}.{name}", mock))
        if "get_lock_users" in helpers:
            stack.enter_context(get_user_answered_from_listing(client))
        yield


def get_user_answer(user_index: int, users: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Return the GetUserResponse a lock listing ``users`` gives for ``user_index``.

    In the shape the Matter server relays it, and ``FakeMatterLock`` answers
    it: the response's fields by their camelCase names, all of them, with an
    empty index carrying no user-specific fields. ``users`` is what
    ``get_lock_users`` lists.
    """
    answer: dict[str, Any] = {
        "userIndex": user_index,
        "userName": None,
        "userUniqueID": None,
        "userStatus": None,
        "userType": None,
        "credentialRule": None,
        "credentials": None,
        "creatorFabricIndex": None,
        "lastModifiedFabricIndex": None,
        "nextUserIndex": min(
            (
                index
                for user in users
                if (index := user.get("user_index")) is not None and index > user_index
            ),
            default=None,
        ),
    }
    user = next((user for user in users if user.get("user_index") == user_index), None)
    if user is None:
        return answer
    return answer | {
        "userName": user.get("user_name"),
        "userStatus": DoorLock.Enums.UserStatusEnum.kOccupiedEnabled,
        "userType": DoorLock.Enums.UserTypeEnum.kUnrestrictedUser,
        "credentialRule": DoorLock.Enums.CredentialRuleEnum.kSingle,
        "credentials": [
            {
                "credentialType": CREDENTIAL_TYPE_REVERSE_MAP[credential["type"]],
                "credentialIndex": credential["index"],
            }
            for credential in user.get("credentials") or []
        ],
        "creatorFabricIndex": user.get("creator_fabric_index"),
        "lastModifiedFabricIndex": user.get("creator_fabric_index"),
    }


@contextmanager
def get_user_answered_from_listing(client: MagicMock) -> Iterator[None]:
    """
    Answer a GetUser sent through ``client`` from ``get_lock_users`` as patched.

    For tests that stand in for the lock with a ``get_lock_users`` mock: the
    provider's own read sends GetUser and parses the answer for real, and
    finds the same users, or fails the same way, as the listing. Looked up
    when called, so a test's own patch counts. Any other command is answered
    as the client mock answered it before.
    """

    async def _send_device_command(*_args: Any, command: Any, **_kwargs: Any) -> Any:
        if not isinstance(command, DoorLock.Commands.GetUser):
            return DEFAULT
        lock_data = await matter_provider.get_lock_users(None, None)
        return get_user_answer(command.userIndex, lock_data["users"])

    with patch.object(
        client,
        "send_device_command",
        AsyncMock(
            side_effect=_send_device_command,
            return_value=client.send_device_command.return_value,
        ),
    ):
        yield


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    matter_node: MatterNode,
    lock_entity: er.RegistryEntry,
    matter_mock_helpers: dict[str, AsyncMock],
    matter_client: MagicMock,
) -> MockConfigEntry:
    """
    Set up a full LCM config entry managing the Matter lock.

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
    with matter_helpers_patched(matter_mock_helpers, matter_client):
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


# ---------------------------------------------------------------------------
# Stateful lock fixtures -- the lock faked where Home Assistant's client meets
# the server (see fake_node.py), so the client's encoding, Home Assistant's
# lock_helpers and the provider all run for real. New tests should prefer
# these to ``matter_mock_helpers``, which patches the helpers with canned
# answers and changes nothing when written to.
# ---------------------------------------------------------------------------

# A real lock that manages users (FeatureMap 385: PIN, USR), from Home
# Assistant core's Matter fixtures (core e4c72622600d, secuyou_smart_lock.json):
# ten users, ten PIN credentials, three per user, PINs of six to eight digits.
FAKE_MATTER_NODE_FIXTURE = "secuyou_smart_lock"

FAKE_MATTER_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "123456", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "567890", CONF_ENABLED: True},
}


@pytest.fixture
def fake_matter_lock_options() -> dict[str, Any]:
    """Keyword arguments for ``FakeMatterLock``; override to change the lock."""
    return {}


@pytest.fixture
def fake_matter_node_attributes() -> dict[str, Any]:
    """
    Attribute values, by path, set on the node before anything reads it.

    Override to change what the lock advertises, its Door Lock FeatureMap
    included.
    """
    return {}


@pytest.fixture
async def fake_matter_lock(
    hass: HomeAssistant,
    matter_client: MagicMock,
    fake_matter_lock_options: dict[str, Any],
    fake_matter_node_attributes: dict[str, Any],
) -> FakeMatterLock:
    """
    Set the Matter integration up on a stateful fake of a lock that manages users.

    Installed before the integration starts, so its own setup already talks
    to the fake.
    """
    node = create_node_from_fixture(FAKE_MATTER_NODE_FIXTURE)
    for path, value in fake_matter_node_attributes.items():
        node.update_attribute(path, value)
    fake = FakeMatterLock(matter_client, node, **fake_matter_lock_options)
    await setup_matter_integration_with_node(hass, matter_client, node)
    return fake


@pytest.fixture
def fake_matter_lock_entity(
    hass: HomeAssistant, fake_matter_lock: FakeMatterLock
) -> er.RegistryEntry:
    """Return the lock entity the Matter integration created for the fake lock."""
    [lock_entity] = [
        entry
        for entry in er.async_get(hass).entities.values()
        if entry.domain == "lock" and entry.platform == "matter"
    ]
    return lock_entity


@pytest.fixture
async def fake_matter_lock_entry(
    hass: HomeAssistant,
    fake_matter_lock: FakeMatterLock,
    fake_matter_lock_entity: er.RegistryEntry,
) -> AsyncGenerator[MockConfigEntry]:
    """Set up a Lock Code Manager entry on the fake lock, slots 1 and 2."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [fake_matter_lock_entity.entity_id]},
        subentries_data=user_subentries(FAKE_MATTER_SLOTS),
        unique_id="test_matter_fake_lock",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yield entry

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def fake_e2e_matter_lock(
    hass: HomeAssistant,
    fake_matter_lock_entry: MockConfigEntry,
    fake_matter_lock_entity: er.RegistryEntry,
) -> MatterLock:
    """Return the provider ``fake_matter_lock_entry`` set up on the fake lock."""
    return get_matter_lock(hass, fake_matter_lock_entry, fake_matter_lock_entity)
