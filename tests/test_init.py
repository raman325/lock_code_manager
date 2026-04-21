"""Test init module."""

import copy
import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.components.lovelace.const import CONF_RESOURCE_TYPE_WS
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    ATTR_CODE,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
    Platform,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
    EVENT_PIN_USED,
    SERVICE_HARD_REFRESH_USERCODES,
    STRATEGY_PATH,
)
from custom_components.lock_code_manager.models import SyncState
from custom_components.lock_code_manager.repairs import (
    AcknowledgeRepairFlow,
    NumberOfUsesDeprecatedFlow,
    async_create_fix_flow,
)

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_IN_SYNC_ENTITY,
)
from .conftest import (
    async_initial_tick,
    async_trigger_sync_tick,
    get_in_sync_entity_obj,
)

_LOGGER = logging.getLogger(__name__)


@pytest.mark.parametrize("config", [{}])
async def test_entry_setup_and_unload(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test entry setup and unload."""
    mock_lock_entry_id = mock_lock_config_entry.entry_id
    lcm_entry_id = lock_code_manager_config_entry.entry_id
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        device = dev_reg.async_get_device({(DOMAIN, entity_id)})
        assert device
        assert device.config_entries == {mock_lock_entry_id, lcm_entry_id}

    unique_ids = set()
    for slot in range(1, 3):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|2|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 2
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 4
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 2
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 4

    ll_data = hass.data[LL_DOMAIN]
    assert ll_data
    resources = ll_data.resources
    assert resources
    assert resources.loaded
    assert any(data[CONF_URL] == STRATEGY_PATH for data in resources.async_items())

    locks = lock_code_manager_config_entry.runtime_data.locks
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        assert not locks[lock_entity_id].service_calls["hard_refresh_codes"]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    assert locks[LOCK_1_ENTITY_ID].service_calls["hard_refresh_codes"]
    assert not locks[LOCK_2_ENTITY_ID].service_calls["hard_refresh_codes"]

    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][1][CONF_NUMBER_OF_USES] = 5
    new_config[CONF_SLOTS][2].pop(CONF_NUMBER_OF_USES)
    new_config[CONF_SLOTS][3] = {
        CONF_NAME: "test3",
        ATTR_CODE: "4321",
        CONF_ENABLED: True,
    }

    assert hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    unique_ids = set()
    for slot in range(1, 4):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|1|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
        if hass.states.get(entity.entity_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 9
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 3
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 3
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 6

    new_config = copy.deepcopy(new_config)
    new_config[CONF_SLOTS].pop(3)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]

    assert hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    # Validate that the config entry is removed from the device associated with the
    # lock that was removed from the config entry
    device = dev_reg.async_get_device({(DOMAIN, LOCK_2_ENTITY_ID)})
    assert device
    assert device.config_entries == {mock_lock_entry_id}

    unique_ids = set()
    for slot in range(1, 3):
        for key in (ATTR_CODE, ATTR_IN_SYNC):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{LOCK_1_ENTITY_ID}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    unique_ids.add(f"{lcm_entry_id}|1|{CONF_NUMBER_OF_USES}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 4
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 2
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 2
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 2
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 4


async def test_reauth(hass: HomeAssistant, lock_code_manager_config_entry):
    """Test reauth."""
    assert (
        len(
            [
                flow
                for flow in lock_code_manager_config_entry.async_get_active_flows(
                    hass, {SOURCE_REAUTH}
                )
            ]
        )
        == 1
    )


@pytest.mark.parametrize("config", [{}])
async def test_resource_already_loaded_ui(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test when strategy resource is already loaded in UI mode."""
    resources = hass.data[LL_DOMAIN].resources
    assert resources
    await resources.async_load()

    await resources.async_create_item(
        {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: STRATEGY_PATH}
    )

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    items = [
        item for item in resources.async_items() if item[CONF_URL] == STRATEGY_PATH
    ]
    assert len(items) == 1

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "config",
    [{"mode": "yaml", "resources": [{"type": "module", CONF_URL: STRATEGY_PATH}]}],
)
async def test_resource_already_loaded_yaml(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test when strategy resource is already loaded in YAML mode."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    resources = hass.data[LL_DOMAIN].resources
    assert resources
    items = [
        item for item in resources.async_items() if item[CONF_URL] == STRATEGY_PATH
    ]
    assert len(items) == 1

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "config",
    [{"mode": "yaml", "resources": [{"type": "module", CONF_URL: "fake_module.js"}]}],
)
async def test_resource_not_loaded_yaml(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Test when strategy resource is not loaded in YAML mode shows warning."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    resources = hass.data[LL_DOMAIN].resources
    assert resources
    assert not any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    # Verify warning about manual YAML registration was logged
    assert "can't automatically be registered" in caplog.text
    assert "running in YAML mode" in caplog.text

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize(
    "config",
    [{"mode": "yaml", "resources": [{"type": "module", CONF_URL: STRATEGY_PATH}]}],
)
async def test_resource_unload_skips_yaml_mode(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Ensure resource removal is skipped when resources are managed via YAML."""
    caplog.set_level(logging.DEBUG)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Simulate auto-registration bookkeeping being set from a prior run
    hass.data[DOMAIN]["resources"] = True

    resources = hass.data[LL_DOMAIN].resources
    assert resources
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    await hass.config_entries.async_unload(config_entry.entry_id)

    # Resource should remain because YAML mode can't be modified automatically
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    # Verify the YAML mode skip messages were logged
    assert "already in YAML resources" in caplog.text
    assert "skipping automatic removal" in caplog.text


async def test_two_entries_same_locks(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test two entries that use same locks but different slots set up successfully."""
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS] = {3: {CONF_ENABLED: False, CONF_PIN: "0123"}}
    new_entry = MockConfigEntry(
        domain=DOMAIN, data=new_config, unique_id="Mock Title 2", title="Mock Title 2"
    )
    new_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(new_entry.entry_id)
    await hass.async_block_till_done()
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 9
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 3
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 3
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 6


@pytest.mark.parametrize("config", [{}])
async def test_resource_not_loaded_on_unload(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test when strategy resource is not loaded when unloading config entry."""
    resources = hass.data[LL_DOMAIN].resources
    assert resources
    await resources.async_load()

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    await resources.async_delete_item(
        next(
            item["id"]
            for item in resources.async_items()
            if item[CONF_URL] == STRATEGY_PATH
        )
    )

    await hass.config_entries.async_unload(config_entry.entry_id)

    assert not any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert not hass.data[DOMAIN].get(CONF_LOCKS)


@pytest.mark.parametrize("config", [{}])
async def test_resource_reregistered_after_unload_and_new_entry(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test resource is re-registered when new entry added after all entries removed."""
    resources = hass.data[LL_DOMAIN].resources
    assert resources
    await resources.async_load()

    # Set up first config entry
    config_entry_1 = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title 1", title="Mock Title 1"
    )
    config_entry_1.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry_1.entry_id)
    await hass.async_block_till_done()

    # Set up second config entry
    config_entry_2 = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title 2", title="Mock Title 2"
    )
    config_entry_2.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry_2.entry_id)
    await hass.async_block_till_done()

    # Verify resource is registered
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert hass.data[DOMAIN]["resources"] is True

    # Remove first entry - resource should still exist
    await hass.config_entries.async_remove(config_entry_1.entry_id)
    await hass.async_block_till_done()
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())

    # Remove second entry - resource should be cleaned up
    await hass.config_entries.async_remove(config_entry_2.entry_id)
    await hass.async_block_till_done()
    assert not any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert not hass.data[DOMAIN].get(CONF_LOCKS)

    # Set up a new config entry - resource should be re-registered
    config_entry_3 = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title 3", title="Mock Title 3"
    )
    config_entry_3.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry_3.entry_id)
    await hass.async_block_till_done()

    # Verify resource is re-registered
    assert any(item[CONF_URL] == STRATEGY_PATH for item in resources.async_items())
    assert hass.data[DOMAIN]["resources"] is True

    # Clean up
    await hass.config_entries.async_remove(config_entry_3.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_entry_setup_and_unload_before_ha_started(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test entry setup before HA started and safe_unsub on unload."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Startup"
    )
    config_entry.add_to_hass(hass)

    # Setup while HA is "starting" - exercises the startup listener code path
    with patch.object(hass, "state", CoreState.starting):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    # Unload immediately (before started event fires)
    # This exercises the _safe_unsub path that catches ValueError
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_remove(config_entry.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_migration_v1_to_v2_calendar_to_entity_id(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test migration from v1 CONF_CALENDAR to v2 CONF_ENTITY_ID."""
    # Create v1 config with CONF_CALENDAR
    v1_config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "test1",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
            },
            2: {
                CONF_NAME: "test2",
                CONF_PIN: "5678",
                CONF_ENABLED: True,
                CONF_CALENDAR: "calendar.test_1",
            },
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=v1_config,
        unique_id="Migration Test",
        version=1,
    )
    config_entry.add_to_hass(hass)

    # Setup should trigger migration
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Verify migration happened
    assert config_entry.version == 2

    # Get the migrated data (should be in .data after setup moves options to data)
    migrated_data = config_entry.data

    # Slot 1 should be unchanged (no calendar)
    assert CONF_CALENDAR not in migrated_data[CONF_SLOTS][1]
    assert CONF_ENTITY_ID not in migrated_data[CONF_SLOTS][1]

    # Slot 2 should have CONF_ENTITY_ID instead of CONF_CALENDAR
    assert CONF_CALENDAR not in migrated_data[CONF_SLOTS][2]
    assert migrated_data[CONF_SLOTS][2][CONF_ENTITY_ID] == "calendar.test_1"

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.config_entries.async_remove(config_entry.entry_id)


async def test_overlapping_locks_both_entries_get_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Test two config entries sharing a lock both create entities."""
    # First entry is already set up via fixture with slots 1-2 and both locks.
    # Add a second entry that shares the same locks but uses slot 3.
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS] = {
        3: {CONF_NAME: "entry2_slot3", CONF_PIN: "0123", CONF_ENABLED: True},
    }
    entry_2 = MockConfigEntry(
        domain=DOMAIN,
        data=new_config,
        unique_id="Overlap Test 2",
        title="Overlap Test 2",
    )
    entry_2.add_to_hass(hass)
    await hass.config_entries.async_setup(entry_2.entry_id)
    await hass.async_block_till_done()

    # The second entry reuses existing lock instances — verify no warnings
    assert "Coordinator missing" not in caplog.text

    # Both entries should have created their entities
    ent_reg = er.async_get(hass)
    entry_1_entities = er.async_entries_for_config_entry(
        ent_reg, lock_code_manager_config_entry.entry_id
    )
    entry_2_entities = er.async_entries_for_config_entry(ent_reg, entry_2.entry_id)
    assert len(entry_1_entities) > 0
    assert len(entry_2_entities) > 0

    # Reused locks should have a coordinator (setup completed before entity creation)
    for lock in entry_2.runtime_data.locks.values():
        assert lock.coordinator is not None

    await hass.config_entries.async_unload(entry_2.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_reload_after_started_no_listener_error(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Test that unloading after started event fires does not log listener error."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=BASE_CONFIG,
        unique_id="Listener Test",
        title="Listener Test",
    )
    config_entry.add_to_hass(hass)

    # Setup while HA is "starting" so _on_started listener is registered
    with patch.object(hass, "state", CoreState.starting):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    # Fire the started event — listener auto-removes itself
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    # Unload — _safe_unsub should skip unsub since event already fired
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert "Unable to remove unknown job" not in caplog.text

    await hass.config_entries.async_remove(config_entry.entry_id)


async def test_coordinator_exists_after_setup(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that coordinator is created after async_setup completes."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        assert lock.coordinator is not None


@pytest.mark.parametrize("config", [{}])
async def test_lovelace_updated_on_structural_change(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test lovelace_updated event fires when slots are added or removed."""
    events = []
    hass.bus.async_listen("lovelace_updated", events.append)

    # Add a new slot (structural change)
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][3] = {
        CONF_NAME: "test3",
        CONF_PIN: "4321",
        CONF_ENABLED: True,
    }
    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {"url_path": None}


@pytest.mark.parametrize("config", [{}])
async def test_lovelace_not_updated_on_non_structural_change(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test lovelace_updated event does not fire on non-structural changes."""
    events = []
    hass.bus.async_listen("lovelace_updated", events.append)

    # Change a PIN (non-structural change — same slots and locks)
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][1][CONF_PIN] = "9999"
    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    assert len(events) == 0


@pytest.mark.parametrize("config", [{}])
async def test_unload_fires_lock_removed_callbacks(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that unloading an entry fires lock-removed callbacks for each lock."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    callbacks = runtime_data.callbacks

    removed_locks: list[str] = []
    callbacks.register_lock_removed_handler(removed_locks.append)

    # Verify locks are present before unload
    assert set(runtime_data.locks) == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}

    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()

    # Both locks should have had their removed callbacks fired
    assert set(removed_locks) == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}


@pytest.mark.parametrize("config", [{}])
async def test_number_of_uses_repair_issue_created(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test that a repair issue is created when slots have number_of_uses."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            "1": {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            "2": {
                CONF_NAME: "test2",
                CONF_PIN: "5678",
                CONF_ENABLED: True,
                CONF_NUMBER_OF_USES: 5,
            },
        },
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Repair Test", title="Repair Test"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "number_of_uses_deprecated") is not None

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_number_of_uses_no_repair_when_absent(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test that no repair issue is created when no slots have number_of_uses."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            "1": {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="No Repair Test", title="No Repair Test"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "number_of_uses_deprecated") is None

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_number_of_uses_repair_flow_strips_data(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test that the repair flow strips number_of_uses from all entries."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            "1": {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            "2": {
                CONF_NAME: "test2",
                CONF_PIN: "5678",
                CONF_ENABLED: True,
                CONF_NUMBER_OF_USES: 5,
            },
        },
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Repair Flow Test", title="Repair Flow"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Verify repair issue exists
    issue_reg = ir.async_get(hass)
    assert issue_reg.async_get_issue(DOMAIN, "number_of_uses_deprecated") is not None

    # Execute the repair flow (simulate user clicking Submit)
    flow = NumberOfUsesDeprecatedFlow()
    flow.hass = hass
    result = await flow.async_step_init(user_input={})

    assert result["type"] == "create_entry"

    # Listener normalizes slot keys to int before persisting; look up by int.
    assert CONF_NUMBER_OF_USES not in config_entry.data[CONF_SLOTS][2]
    # Slot 1 should be unchanged
    assert CONF_NUMBER_OF_USES not in config_entry.data[CONF_SLOTS][1]

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_number_of_uses_repair_flow_shows_form(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
):
    """Test that the repair flow shows a form on initial step."""
    flow = NumberOfUsesDeprecatedFlow()
    flow.hass = hass
    result = await flow.async_step_init(user_input=None)
    assert result["type"] == "form"
    assert result["step_id"] == "init"


async def test_async_create_fix_flow():
    """Test async_create_fix_flow returns the correct flow."""
    flow = await async_create_fix_flow(None, "number_of_uses_deprecated", None)
    assert isinstance(flow, NumberOfUsesDeprecatedFlow)


async def test_async_create_fix_flow_slot_disabled():
    """Test async_create_fix_flow returns AcknowledgeRepairFlow for slot_disabled."""
    flow = await async_create_fix_flow(None, "slot_disabled_abc_1", None)
    assert isinstance(flow, AcknowledgeRepairFlow)


async def test_acknowledge_repair_flow_steps():
    """Test AcknowledgeRepairFlow shows form then creates entry on confirm."""
    flow = AcknowledgeRepairFlow()
    # First call shows the form
    result = await flow.async_step_init(user_input=None)
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    # Second call with input creates the entry
    result = await flow.async_step_init(user_input={})
    assert result["type"] == "create_entry"


async def test_async_create_fix_flow_pin_required():
    """Test async_create_fix_flow returns AcknowledgeRepairFlow for pin_required."""
    flow = await async_create_fix_flow(None, "pin_required_abc_1", None)
    assert isinstance(flow, AcknowledgeRepairFlow)


async def test_async_create_fix_flow_unknown():
    """Test async_create_fix_flow raises for unknown issue."""
    with pytest.raises(ValueError, match="Unknown issue"):
        await async_create_fix_flow(None, "unknown_issue", None)


async def test_unload_cleans_up_repair_issues(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that unloading an entry deletes slot_disabled and pin_required repair issues."""
    entry_id = lock_code_manager_config_entry.entry_id
    issue_reg = ir.async_get(hass)

    # Create repair issues that should be cleaned up on unload
    for slot_num in (1, 2):
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"slot_disabled_{entry_id}_{slot_num}",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="slot_disabled",
            translation_placeholders={"slot_num": str(slot_num), "reason": "test"},
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"pin_required_{entry_id}_{slot_num}",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="pin_required",
            translation_placeholders={
                "slot_num": str(slot_num),
                "config_entry_title": "test",
            },
        )

    # Create lock_offline issues for both locks
    for lock_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"lock_offline_{lock_id}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="lock_offline",
            translation_placeholders={"lock_entity_id": lock_id},
        )

    # Verify issues exist
    assert issue_reg.async_get_issue(DOMAIN, f"slot_disabled_{entry_id}_1") is not None
    assert issue_reg.async_get_issue(DOMAIN, f"pin_required_{entry_id}_2") is not None
    assert (
        issue_reg.async_get_issue(DOMAIN, f"lock_offline_{LOCK_1_ENTITY_ID}")
        is not None
    )

    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()

    # All issues for this entry should be deleted
    for slot_num in (1, 2):
        assert (
            issue_reg.async_get_issue(DOMAIN, f"slot_disabled_{entry_id}_{slot_num}")
            is None
        )
        assert (
            issue_reg.async_get_issue(DOMAIN, f"pin_required_{entry_id}_{slot_num}")
            is None
        )
    for lock_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        assert issue_reg.async_get_issue(DOMAIN, f"lock_offline_{lock_id}") is None


async def test_reload_resets_sync_state_cleanly(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Config entry reload creates fresh sync managers with clean state."""
    entry = lock_code_manager_config_entry

    # Drive initial sync so _last_set_pin gets populated
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)
    await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    # Get the sync manager reference and verify _last_set_pin has a value
    entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
    old_sync_mgr = entity_obj._sync_manager
    # After sync the _last_set_pin should be set (the initial code was set)
    # or the slot is already in sync without needing a set. Either way we
    # capture the reference for identity comparison later.
    old_mgr_id = id(old_sync_mgr)

    # Unload the config entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    # Re-setup the config entry
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Get new sync manager reference (should be a different object)
    new_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
    new_sync_mgr = new_entity_obj._sync_manager
    assert id(new_sync_mgr) != old_mgr_id, (
        "After reload, sync manager should be a fresh instance"
    )

    # Fresh instance should have _last_set_pin as None
    assert new_sync_mgr._last_set_pin is None

    # Drive initial tick and verify the sync manager reaches a real state
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)
    await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)
    assert new_sync_mgr._state in (
        SyncState.IN_SYNC,
        SyncState.OUT_OF_SYNC,
    ), f"Expected IN_SYNC or OUT_OF_SYNC, got {new_sync_mgr._state}"


async def test_removing_lock_from_config_stops_coordinator_and_sync_managers(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Removing a lock from CONF_LOCKS removes it from runtime_data and cleans up entities."""
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    ent_reg = er.async_get(hass)

    # Verify both locks have coordinators and are running
    assert LOCK_1_ENTITY_ID in runtime_data.locks
    assert LOCK_2_ENTITY_ID in runtime_data.locks
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        lock = runtime_data.locks[lock_entity_id]
        assert lock.coordinator is not None

    # Verify LOCK_2 has in-sync entities in the entity registry
    lock_2_in_sync_entities = [
        entity
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if LOCK_2_ENTITY_ID.split(".")[-1] in entity.unique_id
        and ATTR_IN_SYNC in entity.unique_id
    ]
    assert len(lock_2_in_sync_entities) > 0

    # Update config to remove LOCK_2
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    hass.config_entries.async_update_entry(entry, options=new_config)
    await hass.async_block_till_done()

    # Verify LOCK_2 is gone from runtime_data
    assert LOCK_2_ENTITY_ID not in runtime_data.locks

    # Verify LOCK_2's in-sync entities are removed (state no longer present)
    for entity in lock_2_in_sync_entities:
        state = hass.states.get(entity.entity_id)
        assert state is None, (
            f"Expected {entity.entity_id} to be removed, but state is {state}"
        )

    # Verify LOCK_1 is still running with coordinator intact
    assert LOCK_1_ENTITY_ID in runtime_data.locks
    lock_1 = runtime_data.locks[LOCK_1_ENTITY_ID]
    assert lock_1.coordinator is not None


async def test_two_entries_same_lock_share_suspension_and_recovery(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Suspension on one entry's slot affects another entry's out-of-sync slots on the same lock.

    Lock-level suspension is by design: when one slot's circuit breaker trips,
    ALL sync managers on that lock are blocked from syncing. This prevents a
    misbehaving lock from being hammered by multiple entries. When the coordinator
    recovers, all entries' sync managers resume.
    """
    entry_a = lock_code_manager_config_entry

    # Create entry B managing slot 3 on the same locks
    config_b = copy.deepcopy(BASE_CONFIG)
    config_b[CONF_SLOTS] = {
        3: {CONF_NAME: "entry_b_slot3", CONF_PIN: "0123", CONF_ENABLED: True},
    }
    entry_b = MockConfigEntry(
        domain=DOMAIN, data=config_b, unique_id="Entry B", title="Entry B"
    )
    entry_b.add_to_hass(hass)
    await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    # Both entries share the same coordinator for LOCK_1
    lock_a = entry_a.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock_b = entry_b.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert lock_a.coordinator is lock_b.coordinator, (
        "Both entries should share the same coordinator for the same lock"
    )

    # Get entry B's in-sync entity for slot 3 on lock 1
    entry_b_in_sync_entity = "binary_sensor.test_1_code_slot_3_in_sync"

    # Ensure slot 3 exists in coordinator data so sync manager can resolve state.
    # The mock lock only starts with slots 1 and 2, so push slot 3 data.
    lock_a.coordinator.push_update({3: "0123"})
    await hass.async_block_till_done()

    # Drive initial ticks for entry B's sync managers to reach IN_SYNC
    await async_initial_tick(hass, entry_b_in_sync_entity)
    await async_trigger_sync_tick(hass, entry_b_in_sync_entity)

    entry_b_entity_obj = get_in_sync_entity_obj(hass, entry_b_in_sync_entity)
    assert entry_b_entity_obj._sync_manager._state == SyncState.IN_SYNC

    # Suspend the coordinator (simulating circuit breaker trip from entry A)
    lock_a.coordinator.suspend_slot_sync_mgrs()
    await hass.async_block_till_done()

    # An IN_SYNC slot stays IN_SYNC during suspension (nothing to do), which
    # is correct behavior: it's already synced, no need to block.
    assert entry_b_entity_obj._sync_manager._state == SyncState.IN_SYNC

    # Now make entry B's slot out-of-sync by changing the code on the lock
    # while the coordinator is suspended
    lock_a.coordinator.push_update({3: "different"})
    await hass.async_block_till_done()

    # The push_update call above clears the suspension flag (successful
    # push proves lock is reachable). Re-suspend to test blocking, then
    # let _request_sync_check detect the mismatch naturally.
    lock_a.coordinator.suspend_slot_sync_mgrs()
    await hass.async_block_till_done()

    # The coordinator listener fires _request_sync_check. Since the code
    # changed ("different" != "0123"), the slot should be OUT_OF_SYNC.
    # But wait — suspend_slot_sync_mgrs calls async_update_listeners,
    # which fires _request_sync_check on all managers. For an IN_SYNC
    # manager with a mismatch, it transitions to OUT_OF_SYNC. Then on
    # the next tick, the suspension check blocks it into SUSPENDED.
    await async_trigger_sync_tick(hass, entry_b_in_sync_entity, set_dirty=False)
    await hass.async_block_till_done()
    assert entry_b_entity_obj._sync_manager._state == SyncState.SUSPENDED, (
        "OUT_OF_SYNC slot should be blocked by lock-level suspension"
    )

    # Recovery: push a successful update to reset backoff and clear suspension
    lock_a.coordinator.push_update({3: "0123"})
    await hass.async_block_till_done()

    # After recovery, entry B's sync manager should resume from SUSPENDED
    # via _request_sync_check detecting the cleared suspension flag
    assert entry_b_entity_obj._sync_manager._state == SyncState.OUT_OF_SYNC, (
        f"Entry B's sync manager should have resumed to OUT_OF_SYNC, "
        f"but state is {entry_b_entity_obj._sync_manager._state}"
    )

    await hass.config_entries.async_unload(entry_b.entry_id)
