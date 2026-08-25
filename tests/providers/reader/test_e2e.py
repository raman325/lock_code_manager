"""Full lifecycle E2E tests for the reader provider."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_IN_SYNC,
    ATTR_LOCK_ENTITY_ID,
    ATTR_SLOT,
    ATTR_SLOT_NUM,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_READERS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CODE_VALIDATION_FAILED,
    EVENT_CREDENTIAL_USED,
    EVENT_LOCK_STATE_CHANGED,
    STRATEGY_PATH,
)
from custom_components.lock_code_manager.domain.config import (
    build_slot_device_identifier,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.providers.reader import ReaderLock
from custom_components.lock_code_manager.providers.virtual import VirtualLock

from ...conftest import async_initial_tick, async_trigger_sync_tick
from .conftest import READER_ENTITY_ID

VIRTUAL_LOCK_ENTITY_ID = "lock.virtual_test_virtual"

READER_DEVICE_IDENTIFIER = ("esphome", "front_keypad")


async def _create_entry_via_config_flow(
    hass: HomeAssistant,
    *,
    title: str,
    locks: list[str],
    readers: list[str],
    users: Mapping[str, str],
) -> ConfigEntry:
    """
    Build a config entry the way a user does, one form submission at a time.

    Hand-building the entry would validate this test's idea of what the flow
    writes; driving the real steps is the only way the persisted shape and
    the setup path are checked against each other.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: title, CONF_LOCKS: locks, CONF_READERS: readers},
    )
    assert result["type"] == "menu"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    assert result["step_id"] == "ui"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_USERS: len(users)}
    )
    assert result["step_id"] == "code_slot"

    for name, pin in users.items():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: name, CONF_ENABLED: True, CONF_PIN: pin}
        )

    assert result["type"] == "create_entry"
    entry = result["result"]
    await hass.async_block_till_done()
    return entry


def _entity_id_for(hass: HomeAssistant, entry: ConfigEntry, unique_id: str) -> str:
    """
    Resolve one slot entity by the unique ID the backend built it from.

    Entity IDs are derived from names a test would have to reconstruct;
    the unique ID is the identifier the integration actually issued.
    """
    entity_id = next(
        (
            registry_entry.entity_id
            for registry_entry in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
            if registry_entry.unique_id == unique_id
        ),
        None,
    )
    assert entity_id, f"no entity with unique ID {unique_id}"
    return entity_id


async def _async_set_pin(hass: HomeAssistant, entity_id: str, pin: str) -> None:
    """Type a PIN into the slot's text entity, as the dashboard does."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: pin},
        blocking=True,
    )
    await hass.async_block_till_done()


@pytest.fixture
async def reader_device(
    hass: HomeAssistant, esphome_config_entry: MockConfigEntry
) -> dr.DeviceEntry:
    """Register the keypad's own device, as its integration would."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=esphome_config_entry.entry_id,
        identifiers={READER_DEVICE_IDENTIFIER},
        name="Front Keypad",
    )


@pytest.fixture
async def anchored_reader_entity(
    hass: HomeAssistant,
    esphome_config_entry: MockConfigEntry,
    reader_device: dr.DeviceEntry,
) -> er.RegistryEntry:
    """Register the anchor sensor on the keypad's device."""
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "keypad_code",
        suggested_object_id="keypad_code",
        config_entry=esphome_config_entry,
        device_id=reader_device.id,
    )
    assert entity.entity_id == READER_ENTITY_ID
    # A cleared keypad idles on an empty state; set it before LCM subscribes
    # so no test starts with a phantom submission.
    hass.states.async_set(READER_ENTITY_ID, "")
    return entity


@pytest.fixture
async def virtual_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Register a lock entity the virtual provider claims."""
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)

    lock_entity = er.async_get(hass).async_get_or_create(
        "lock",
        "virtual",
        "test_virtual",
        config_entry=virtual_entry,
    )
    assert lock_entity.entity_id == VIRTUAL_LOCK_ENTITY_ID
    hass.states.async_set(VIRTUAL_LOCK_ENTITY_ID, "locked")
    return lock_entity


@pytest.fixture
async def reader_only_entry(
    hass: HomeAssistant, anchored_reader_entity: er.RegistryEntry
) -> ConfigEntry:
    """Create and set up a reader-only entry through the real config flow."""
    entry = await _create_entry_via_config_flow(
        hass,
        title="Front Keypad",
        locks=[],
        readers=[READER_ENTITY_ID],
        users={"Alice": "1234"},
    )
    assert entry.state is ConfigEntryState.LOADED
    return entry


class TestReaderOnlyEntryLifecycle:
    """A reader with no lock beside it is a complete entry on its own."""

    async def test_reader_only_entry_sets_up_from_the_config_flow(
        self, hass: HomeAssistant, reader_only_entry: ConfigEntry
    ) -> None:
        """The flow's reader field alone produces a loaded entry driving a ReaderLock."""
        assert reader_only_entry.data[CONF_LOCKS] == [READER_ENTITY_ID]
        # CONF_READERS is a form field, never a persisted key: a second
        # source of truth for which entities the entry manages.
        assert CONF_READERS not in reader_only_entry.data

        lock = reader_only_entry.runtime_data.locks[READER_ENTITY_ID]
        assert isinstance(lock, ReaderLock)

    async def test_slot_entities_live_on_the_integrations_own_device(
        self,
        hass: HomeAssistant,
        reader_only_entry: ConfigEntry,
        reader_device: dr.DeviceEntry,
    ) -> None:
        """
        Every entity lands on the per-user device, not the keypad's.

        A device belongs to one config entry, so an entity parked on the
        keypad's device is one this integration could not show.
        """
        ent_reg = er.async_get(hass)
        dev_reg = dr.async_get(hass)

        slot_device = dev_reg.async_get_device(
            {(DOMAIN, build_slot_device_identifier(reader_only_entry.entry_id, 1))}
        )
        assert slot_device
        assert slot_device.config_entries == {reader_only_entry.entry_id}
        assert {
            entry.unique_id
            for entry in er.async_entries_for_device(ent_reg, slot_device.id)
        } == {
            f"{reader_only_entry.entry_id}|1|{key}"
            for key in (
                CONF_ENABLED,
                CONF_NAME,
                CONF_PIN,
                ATTR_ACTIVE,
                EVENT_CREDENTIAL_USED,
            )
        } | {
            f"{reader_only_entry.entry_id}|1|{key}|{READER_ENTITY_ID}"
            for key in (ATTR_CODE, ATTR_IN_SYNC)
        }

        assert not [
            entry
            for entry in er.async_entries_for_device(ent_reg, reader_device.id)
            if entry.config_entry_id == reader_only_entry.entry_id
        ]

    async def test_a_pin_typed_into_the_slot_validates_at_the_reader(
        self, hass: HomeAssistant, reader_only_entry: ConfigEntry
    ) -> None:
        """A PIN set through the text entity is the one the keypad accepts."""
        failures = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)
        event_entity_id = _entity_id_for(
            hass,
            reader_only_entry,
            f"{reader_only_entry.entry_id}|1|{EVENT_CREDENTIAL_USED}",
        )
        assert hass.states.get(event_entity_id).state == STATE_UNKNOWN

        pin_entity_id = _entity_id_for(
            hass, reader_only_entry, f"{reader_only_entry.entry_id}|1|{CONF_PIN}"
        )
        await _async_set_pin(hass, pin_entity_id, "4321")

        # The PIN the flow collected is no longer the live one.
        hass.states.async_set(READER_ENTITY_ID, "1234")
        await hass.async_block_till_done()
        assert len(failures) == 1
        assert hass.states.get(event_entity_id).state == STATE_UNKNOWN

        hass.states.async_set(READER_ENTITY_ID, "")
        hass.states.async_set(READER_ENTITY_ID, "4321")
        await hass.async_block_till_done()

        state = hass.states.get(event_entity_id)
        assert state.state != STATE_UNKNOWN
        assert state.attributes["event_type"] == READER_ENTITY_ID
        assert len(failures) == 1


class TestMixedLockAndReaderEntry:
    """One entry can hold a lock to write to and a keypad to read from."""

    async def test_one_pin_reaches_the_lock_and_opens_at_the_reader(
        self,
        hass: HomeAssistant,
        virtual_lock_entity: er.RegistryEntry,
        anchored_reader_entity: er.RegistryEntry,
    ) -> None:
        """The slot's PIN is written to the lock and accepted by the keypad."""
        entry = await _create_entry_via_config_flow(
            hass,
            title="Front Door",
            locks=[VIRTUAL_LOCK_ENTITY_ID],
            readers=[READER_ENTITY_ID],
            users={"Bob": "2468"},
        )
        assert entry.state is ConfigEntryState.LOADED

        locks = entry.runtime_data.locks
        assert isinstance(locks[READER_ENTITY_ID], ReaderLock)
        virtual_lock = locks[VIRTUAL_LOCK_ENTITY_ID]
        assert isinstance(virtual_lock, VirtualLock)

        slot_num = get_entry_config(entry).assignment.slot("Bob")
        in_sync_entity_id = _entity_id_for(
            hass,
            entry,
            f"{entry.entry_id}|{slot_num}|{ATTR_IN_SYNC}|{VIRTUAL_LOCK_ENTITY_ID}",
        )
        await async_initial_tick(hass, in_sync_entity_id)
        await async_trigger_sync_tick(hass, in_sync_entity_id)

        codes = await virtual_lock.async_get_usercodes()
        assert codes[slot_num] == SlotCredential.known("2468")

        usages = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)
        failures = async_capture_events(hass, EVENT_CODE_VALIDATION_FAILED)

        hass.states.async_set(READER_ENTITY_ID, "2468")
        await hass.async_block_till_done()

        assert not failures
        assert [event.data[ATTR_CODE_SLOT] for event in usages] == [slot_num]
        # The submission is attributed to the keypad, never to the lock the
        # PIN was written to.
        assert usages[0].data[ATTR_ENTITY_ID] == READER_ENTITY_ID


class TestDashboardTolerance:
    """The frontend's surfaces must not choke on a lock that is a sensor."""

    async def test_websocket_apis_answer_for_a_reader_only_entry(
        self,
        hass: HomeAssistant,
        reader_only_entry: ConfigEntry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """Every command the dashboard issues succeeds with a sensor as the lock."""
        ws_client = await hass_ws_client(hass)

        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/get_config_entry_data",
                ATTR_CONFIG_ENTRY_ID: reader_only_entry.entry_id,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]
        result = msg["result"]
        assert [lock[ATTR_ENTITY_ID] for lock in result[CONF_LOCKS]] == [
            READER_ENTITY_ID
        ]
        assert result[CONF_SLOTS] == {"1": {CONF_NAME: "Alice", "condition": None}}

        await ws_client.send_json(
            {
                "id": 2,
                "type": "lock_code_manager/subscribe_lock_codes",
                ATTR_LOCK_ENTITY_ID: READER_ENTITY_ID,
                "reveal": True,
            }
        )
        assert (await ws_client.receive_json())["success"]
        event = await ws_client.receive_json()
        assert event["event"][ATTR_LOCK_ENTITY_ID] == READER_ENTITY_ID

        await ws_client.send_json(
            {
                "id": 3,
                "type": "lock_code_manager/subscribe_code_slot",
                ATTR_CONFIG_ENTRY_ID: reader_only_entry.entry_id,
                ATTR_SLOT: 1,
                "reveal": True,
            }
        )
        assert (await ws_client.receive_json())["success"]
        slot_event = await ws_client.receive_json()
        assert slot_event["type"] == "event"
        assert slot_event["event"][ATTR_SLOT_NUM] == 1
        assert slot_event["event"][CONF_NAME] == "Alice"
        # The card draws one row per lock, and the reader is the only one.
        assert [lock[ATTR_ENTITY_ID] for lock in slot_event["event"][CONF_LOCKS]] == [
            READER_ENTITY_ID
        ]

    @pytest.mark.parametrize("config", [{}])
    async def test_the_strategy_resource_registers_for_a_reader_only_entry(
        self,
        hass: HomeAssistant,
        setup_lovelace_ui,
        reader_only_entry: ConfigEntry,
    ) -> None:
        """Setup registers the dashboard module even with no lock in the entry."""
        resources = hass.data[LL_DOMAIN].resources
        assert resources.loaded
        assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    async def test_an_offline_anchor_still_serializes_as_a_lock(
        self,
        hass: HomeAssistant,
        reader_only_entry: ConfigEntry,
        hass_ws_client: WebSocketGenerator,
    ) -> None:
        """
        A keypad that drops offline is still a lock the card can draw.

        Serialization reads the anchor's state for the lock's name; an
        unavailable one has no friendly name to read.
        """
        hass.states.async_set(READER_ENTITY_ID, STATE_UNAVAILABLE)
        await hass.async_block_till_done()

        ws_client = await hass_ws_client(hass)
        await ws_client.send_json(
            {
                "id": 1,
                "type": "lock_code_manager/get_config_entry_data",
                ATTR_CONFIG_ENTRY_ID: reader_only_entry.entry_id,
            }
        )
        msg = await ws_client.receive_json()
        assert msg["success"]
        assert [lock[ATTR_ENTITY_ID] for lock in msg["result"][CONF_LOCKS]] == [
            READER_ENTITY_ID
        ]

        await ws_client.send_json(
            {
                "id": 2,
                "type": "lock_code_manager/subscribe_code_slot",
                ATTR_CONFIG_ENTRY_ID: reader_only_entry.entry_id,
                ATTR_SLOT: 1,
            }
        )
        assert (await ws_client.receive_json())["success"]
        slot_event = await ws_client.receive_json()
        assert [lock[ATTR_ENTITY_ID] for lock in slot_event["event"][CONF_LOCKS]] == [
            READER_ENTITY_ID
        ]
