"""Test init module."""

import asyncio
import copy
import logging
from unittest.mock import AsyncMock, patch

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from custom_components.lock_code_manager import (
    _setup_entry_after_start,
    async_remove_config_entry_device,
    async_remove_entry,
    async_unload_lock,
)
from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    ATTR_TEXT,
    BACKOFF_FAILURE_THRESHOLD,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_PIN_USED,
    SERVICE_DEOBFUSCATE_LOG,
    SERVICE_HARD_REFRESH_USERCODES,
    STRATEGY_PATH,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.models import SlotCredential, SyncState
from custom_components.lock_code_manager.repairs import (
    AcknowledgeRepairFlow,
    async_create_fix_flow,
)

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_IN_SYNC_ENTITY,
    MockLCMLock,
)
from .conftest import (
    async_initial_tick,
    async_trigger_sync_tick,
    get_in_sync_entity_obj,
)

_LOGGER = logging.getLogger(__name__)

# Legacy slot field name; the constant was deleted but the migration tests
# still need to plant it in mock configs to verify it gets stripped.
LEGACY_NUMBER_OF_USES_KEY = "number_of_uses"


def _loaded_lcm_lock_entity_ids(hass: HomeAssistant) -> set[str]:
    """Return entity IDs of locks held by any loaded Lock Code Manager entry."""
    return {
        entity_id
        for entry in hass.config_entries.async_entries(DOMAIN)
        if (runtime_data := getattr(entry, "runtime_data", None)) is not None
        for entity_id in runtime_data.locks
    }


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
        # LCM links its per-lock entities to the lock's own device but no
        # longer adds its config entry to it -- a device belongs to a single
        # config entry as of HA 2026.8.
        assert device.config_entries == {mock_lock_entry_id}
        lcm_device_entities = {
            entry.unique_id
            for entry in er.async_entries_for_device(ent_reg, device.id)
            if entry.config_entry_id == lcm_entry_id
        }
        assert lcm_device_entities == {
            f"{lcm_entry_id}|{name}|{key}|{entity_id}"
            for name in ("test1", "test2")
            for key in (ATTR_CODE, ATTR_IN_SYNC)
        }

    unique_ids = set()
    for slot in range(1, 3):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|test{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|test{slot}|{key}")

    # BASE_CONFIG slot 2 includes number_of_uses, but the migration strips it
    # before platform forwarding so the number entity is never created.

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
    # number_of_uses values in options are accepted (for backward compatibility
    # with stored configs) but no entity is created — entity creation for the
    # deprecated key was removed.
    new_config[CONF_SLOTS][1][LEGACY_NUMBER_OF_USES_KEY] = 5
    new_config[CONF_SLOTS][2].pop(LEGACY_NUMBER_OF_USES_KEY)
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
                unique_ids.add(f"{lcm_entry_id}|test{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|test{slot}|{key}")

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

    # LOCK_2 was removed from the LCM config entry; its device keeps its own
    # config entry and no longer has any LCM entities linked to it.
    device = dev_reg.async_get_device({(DOMAIN, LOCK_2_ENTITY_ID)})
    assert device
    assert device.config_entries == {mock_lock_entry_id}
    assert not [
        entry
        for entry in er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        )
        if entry.config_entry_id == lcm_entry_id
    ]

    unique_ids = set()
    for slot in range(1, 3):
        for key in (ATTR_CODE, ATTR_IN_SYNC):
            unique_ids.add(f"{lcm_entry_id}|test{slot}|{key}|{LOCK_1_ENTITY_ID}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_PIN_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|test{slot}|{key}")

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
            list(
                lock_code_manager_config_entry.async_get_active_flows(
                    hass, {SOURCE_REAUTH}
                )
            )
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
    new_config[CONF_SLOTS] = {
        3: {CONF_NAME: "User 3", CONF_ENABLED: False, CONF_PIN: "0123"}
    }
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
    assert not _loaded_lcm_lock_entity_ids(hass)


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
    assert not _loaded_lcm_lock_entity_ids(hass)

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
            3: {
                CONF_NAME: "test3",
                CONF_PIN: "9012",
                CONF_ENABLED: True,
                # Both fields already set (e.g. a partially-migrated config) --
                # the migration must drop the stale calendar key and leave the
                # already-set entity_id alone rather than overwriting it.
                CONF_CALENDAR: "calendar.stale",
                CONF_ENTITY_ID: "calendar.test_2",
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

    # Verify migration happened (v1 -> v2 calendar, then v2 -> v3 number_of_uses)
    assert config_entry.version == 4

    # Get the migrated data (should be in .data after setup moves options to data)
    migrated_data = config_entry.data

    # Slot 1 should be unchanged (no calendar)
    assert CONF_CALENDAR not in migrated_data[CONF_SLOTS][1]
    assert CONF_ENTITY_ID not in migrated_data[CONF_SLOTS][1]

    # Slot 2 should have CONF_ENTITY_ID instead of CONF_CALENDAR
    assert CONF_CALENDAR not in migrated_data[CONF_SLOTS][2]
    assert migrated_data[CONF_SLOTS][2][CONF_ENTITY_ID] == "calendar.test_1"

    # Slot 3 already had both fields set -- calendar is dropped and the
    # pre-existing entity_id value is preserved untouched.
    assert CONF_CALENDAR not in migrated_data[CONF_SLOTS][3]
    assert migrated_data[CONF_SLOTS][3][CONF_ENTITY_ID] == "calendar.test_2"

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


async def test_no_slot_coordinator_warning_during_initial_setup(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression test for issue #1213.

    Before the fix, per-lock entities (``code`` sensor, ``in_sync`` binary
    sensor) were scheduled by ``_async_setup_new_locks`` while the slot
    coordinators had not yet been created. The await on
    ``async_internal_is_reachable`` between locks let the event
    loop drain the entity-add tasks for prior locks, whose
    ``async_added_to_hass`` then warned about the missing coordinator.

    The race requires the connection check to actually yield to the event
    loop -- the default mock returns synchronously and so cannot reproduce
    it. We patch it to force an ``asyncio.sleep(0)`` yield, which is what
    real provider calls (Z-Wave JS, Matter) do.
    """

    async def _yielding_is_connected(self: MockLCMLock) -> bool:
        await asyncio.sleep(0)
        return self._connected

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Issue 1213"
    )
    config_entry.add_to_hass(hass)
    caplog.set_level(logging.WARNING)
    with patch.object(
        MockLCMLock, "async_is_integration_connected", _yielding_is_connected
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert "No slot coordinator" not in caplog.text

    await hass.config_entries.async_unload(config_entry.entry_id)


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
async def test_number_of_uses_auto_stripped_and_repair_raised(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
) -> None:
    """Strip number_of_uses on setup and raise a one-time repair issue."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {
                    CONF_NAME: "alice",
                    CONF_PIN: "1234",
                    CONF_ENABLED: True,
                    LEGACY_NUMBER_OF_USES_KEY: 5,
                },
                3: {
                    CONF_NAME: "bob",
                    CONF_PIN: "5678",
                    CONF_ENABLED: True,
                    LEGACY_NUMBER_OF_USES_KEY: 0,
                },
            },
        },
        unique_id="House Locks",
        title="House Locks",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Field stripped from both slots
    assert LEGACY_NUMBER_OF_USES_KEY not in entry.data[CONF_SLOTS][1]
    assert LEGACY_NUMBER_OF_USES_KEY not in entry.data[CONF_SLOTS][3]

    # Informational repair raised with impacted slot list
    issue_registry = ir.async_get(hass)
    issue = issue_registry.async_get_issue(
        DOMAIN, f"number_of_uses_removed_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert "House Locks" in issue.translation_placeholders["impacted"]
    assert "1, 3" in issue.translation_placeholders["impacted"]

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_number_of_uses_repair_not_raised_when_nothing_to_strip(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
) -> None:
    """A clean config entry produces no repair."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "alice", CONF_PIN: "1234", CONF_ENABLED: True},
            },
        },
        unique_id="Clean House Locks",
        title="Clean House Locks",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue_registry = ir.async_get(hass)
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"number_of_uses_removed_{entry.entry_id}"
        )
        is None
    )

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("config", [{}])
async def test_number_of_uses_repair_per_entry(
    hass: HomeAssistant,
    setup_lovelace_ui,
    mock_lock_config_entry,
) -> None:
    """Each impacted entry gets its own repair issue (no clobbering)."""
    entry_a = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {
                    CONF_NAME: "alice",
                    CONF_PIN: "1234",
                    CONF_ENABLED: True,
                    LEGACY_NUMBER_OF_USES_KEY: 5,
                },
            },
        },
        unique_id="House Locks",
        title="House Locks",
    )
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    entry_b = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_2_ENTITY_ID],
            CONF_SLOTS: {
                2: {
                    CONF_NAME: "bob",
                    CONF_PIN: "5678",
                    CONF_ENABLED: True,
                    LEGACY_NUMBER_OF_USES_KEY: 3,
                },
            },
        },
        unique_id="Garage",
        title="Garage",
    )
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_b.entry_id)
    await hass.async_block_till_done()

    issue_registry = ir.async_get(hass)
    issue_a = issue_registry.async_get_issue(
        DOMAIN, f"number_of_uses_removed_{entry_a.entry_id}"
    )
    issue_b = issue_registry.async_get_issue(
        DOMAIN, f"number_of_uses_removed_{entry_b.entry_id}"
    )
    assert issue_a is not None
    assert issue_a.translation_placeholders is not None
    assert "House Locks" in issue_a.translation_placeholders["impacted"]
    assert issue_b is not None
    assert issue_b.translation_placeholders is not None
    assert "Garage" in issue_b.translation_placeholders["impacted"]

    await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.config_entries.async_unload(entry_b.entry_id)


async def test_async_create_fix_flow():
    """Test async_create_fix_flow returns the correct flow."""
    flow = await async_create_fix_flow(
        None, "number_of_uses_removed_test_entry_id", None
    )
    assert isinstance(flow, AcknowledgeRepairFlow)


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


async def test_unload_preserves_persistent_repair_issues(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Unloading an entry must NOT delete persistent repair issues.

    Persistent issues (``is_persistent=True``) are flagged that way
    precisely so they survive HA restarts, reloads, and integration
    disable. ``async_unload_entry`` runs in all three of those cases
    plus the actual entry-removal case; wiping issues there caused
    the "click a repair and it says repaired" short-circuit after
    every HA restart. Cleanup now lives in ``async_remove_entry``
    which runs only on entry deletion -- see the next test.
    """
    entry_id = lock_code_manager_config_entry.entry_id
    issue_reg = ir.async_get(hass)

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

    assert issue_reg.async_get_issue(DOMAIN, f"slot_disabled_{entry_id}_1") is not None
    assert issue_reg.async_get_issue(DOMAIN, f"pin_required_{entry_id}_2") is not None
    assert (
        issue_reg.async_get_issue(DOMAIN, f"lock_offline_{LOCK_1_ENTITY_ID}")
        is not None
    )

    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()

    # Every issue still in the registry; unload preserves them.
    for slot_num in (1, 2):
        assert (
            issue_reg.async_get_issue(DOMAIN, f"slot_disabled_{entry_id}_{slot_num}")
            is not None
        )
        assert (
            issue_reg.async_get_issue(DOMAIN, f"pin_required_{entry_id}_{slot_num}")
            is not None
        )
    for lock_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        assert issue_reg.async_get_issue(DOMAIN, f"lock_offline_{lock_id}") is not None


async def test_remove_entry_cleans_up_repair_issues(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Removing the entry deletes its repair issues (canonical cleanup path)."""
    entry_id = lock_code_manager_config_entry.entry_id
    issue_reg = ir.async_get(hass)

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
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"lock_setup_failed_{lock_id}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="lock_setup_failed",
            translation_placeholders={"lock_entity_id": lock_id, "error": "test"},
        )

    # Call the remove hook directly so the fixture teardown (which
    # async_unloads the entry) still sees a registered entry. HA calls
    # this hook only on actual entry deletion -- not on unload, reload,
    # disable, or HA restart -- which is precisely the property we want
    # to pin.
    await async_remove_entry(hass, lock_code_manager_config_entry)
    await hass.async_block_till_done()

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
        assert issue_reg.async_get_issue(DOMAIN, f"lock_setup_failed_{lock_id}") is None


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
    """
    An unreachable lock blocks another entry's out-of-sync slots on the same lock.

    Lock-level suspension is by design: when the lock becomes unreachable
    (its circuit breaker trips), ALL sync managers on that lock are blocked
    from syncing. This prevents a misbehaving lock from being hammered by
    multiple entries. When the coordinator recovers, all entries' sync
    managers resume.
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
    lock_a.coordinator.push_update({3: SlotCredential.known("0123")})
    await hass.async_block_till_done()

    # Drive initial ticks for entry B's sync managers to reach IN_SYNC
    await async_initial_tick(hass, entry_b_in_sync_entity)
    await async_trigger_sync_tick(hass, entry_b_in_sync_entity)

    entry_b_entity_obj = get_in_sync_entity_obj(hass, entry_b_in_sync_entity)
    assert entry_b_entity_obj._sync_manager._state == SyncState.IN_SYNC

    # Suspend the coordinator (simulating circuit breaker trip from entry A)
    for _ in range(BACKOFF_FAILURE_THRESHOLD):
        lock_a.coordinator._lock_breaker.record_failure()
    lock_a.coordinator.async_update_listeners()
    await hass.async_block_till_done()

    # An IN_SYNC slot stays IN_SYNC during suspension (nothing to do), which
    # is correct behavior: it's already synced, no need to block.
    assert entry_b_entity_obj._sync_manager._state == SyncState.IN_SYNC

    # Now make entry B's slot out-of-sync by changing the code on the lock
    # while the coordinator is suspended
    lock_a.coordinator.push_update({3: SlotCredential.known("different")})
    await hass.async_block_till_done()

    # The push_update call above resets the lock breaker (successful push
    # proves the lock is reachable). Re-trip it to test blocking, then let
    # _request_sync_check detect the mismatch naturally.
    for _ in range(BACKOFF_FAILURE_THRESHOLD):
        lock_a.coordinator._lock_breaker.record_failure()
    lock_a.coordinator.async_update_listeners()
    await hass.async_block_till_done()

    # The coordinator listener fires _request_sync_check. Since the code
    # changed ("different" != "0123"), the slot should be OUT_OF_SYNC.
    # async_update_listeners fires _request_sync_check on all managers. For
    # an IN_SYNC manager with a mismatch, it transitions to OUT_OF_SYNC.
    # Then on the next tick, the unreachable check blocks it into SUSPENDED.
    await async_trigger_sync_tick(hass, entry_b_in_sync_entity, set_dirty=False)
    await hass.async_block_till_done()
    assert entry_b_entity_obj._sync_manager._state == SyncState.SUSPENDED, (
        "OUT_OF_SYNC slot should be blocked by lock-level suspension"
    )

    # Recovery: push a successful update to reset backoff and clear suspension
    lock_a.coordinator.push_update({3: SlotCredential.known("0123")})
    await hass.async_block_till_done()

    # After recovery, entry B's sync manager should resume from SUSPENDED
    # via _request_sync_check detecting the lock is reachable again
    assert entry_b_entity_obj._sync_manager._state == SyncState.OUT_OF_SYNC, (
        f"Entry B's sync manager should have resumed to OUT_OF_SYNC, "
        f"but state is {entry_b_entity_obj._sync_manager._state}"
    )

    await hass.config_entries.async_unload(entry_b.entry_id)


async def test_setup_entry_after_start_does_not_stack_update_listeners(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Running _setup_entry_after_start a second time does not stack update listeners."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    # Initial setup ran via the fixture and should have registered exactly one
    # listener, marked by the runtime-data flag.
    assert runtime_data.update_listener_registered is True
    initial_listener_count = len(lock_code_manager_config_entry.update_listeners)

    # A second invocation (simulating a reload race with EVENT_HOMEASSISTANT_STARTED)
    # must not register another listener.
    _setup_entry_after_start(hass, lock_code_manager_config_entry)
    await hass.async_block_till_done()

    assert (
        len(lock_code_manager_config_entry.update_listeners) == initial_listener_count
    )

    # After unload, the flag clears so a future setup will register again.
    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()
    assert runtime_data.update_listener_registered is False


async def test_options_saved_while_entry_down_survive_data_migration(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Options written while the entry could not process them survive setup.

    When an entry fails setup (e.g. a configured lock entity vanished after a
    Z-Wave exclusion), no update listener is registered, so an options-flow
    save just sits in ``options``. The data→options migration in
    ``_setup_entry_after_start`` used to overwrite ``options`` wholesale with
    the stale ``data``, silently discarding the user's fix on the next
    successful setup. The migration must merge options-preferred instead —
    the same precedence ``EntryConfig.from_entry`` uses.
    """
    old_config = copy.deepcopy(BASE_CONFIG)
    # The user's fix, saved via the options flow while the entry was down:
    # LOCK_2 removed from the entry.
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=old_config,
        options=new_config,
        unique_id="Mock Title Options Survive",
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # The saved options won: LOCK_2 is not managed and the persisted config
    # (migrated back to data by the update listener) reflects the fix.
    assert set(config_entry.runtime_data.locks) == {LOCK_1_ENTITY_ID}
    assert config_entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    assert not config_entry.options

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_unload_stops_sync_managers_before_callbacks_and_platforms(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Unload sequence: stop sync managers -> fire lock-removed callbacks -> unload platforms."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    callbacks = runtime_data.callbacks

    # Capture ordering: sync managers must be stopped (and thus discarded from
    # the registry) before lock-removed callbacks fire.
    sync_manager_count_at_lock_removed: list[int] = []

    def _on_lock_removed(_entity_id: str) -> None:
        sync_manager_count_at_lock_removed.append(len(runtime_data.sync_managers))

    callbacks.register_lock_removed_handler(_on_lock_removed)

    assert len(runtime_data.sync_managers) > 0
    initial_manager_count = len(runtime_data.sync_managers)

    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()

    # Lock-removed handlers must have fired AFTER sync managers were stopped
    # and cleared from the registry.
    assert sync_manager_count_at_lock_removed
    assert all(count == 0 for count in sync_manager_count_at_lock_removed)
    # Multiple locks in the fixture means we saw multiple callback invocations.
    assert len(sync_manager_count_at_lock_removed) >= 1

    # And the original count was non-trivial -- we actually had managers to stop.
    assert initial_manager_count >= 1


async def test_unload_awaits_in_flight_sync_tick(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Unload waits for an in-flight sync tick before returning."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    assert runtime_data.sync_managers

    # Pick a manager and stall its tick mid-flight by patching its
    # _async_tick_impl to wait on an event we control.
    manager = next(iter(runtime_data.sync_managers))
    manager._state = SyncState.OUT_OF_SYNC

    mid_tick = asyncio.Event()
    release = asyncio.Event()

    async def stalled_tick_impl() -> None:
        mid_tick.set()
        await release.wait()

    with patch.object(manager, "_async_tick_impl", stalled_tick_impl):
        tick_task = hass.async_create_task(manager._async_tick())
        await asyncio.wait_for(mid_tick.wait(), timeout=5)

        # Begin unload; it should not return while the tick is in flight.
        unload_task = hass.async_create_task(
            hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
        )
        await asyncio.sleep(0)
        assert not unload_task.done()

        # Release the tick; unload should now complete.
        release.set()
        await tick_task
        await unload_task


async def test_unload_logs_sync_manager_stop_exceptions(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Unload logs warnings when individual sync manager stops raise and still stops the rest."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    managers = list(runtime_data.sync_managers)
    assert len(managers) >= 2

    boom = RuntimeError("simulated stop failure")
    failing_mgr, *other_mgrs = managers
    original_stop = failing_mgr.async_stop
    # binary_sensor.async_will_remove_from_hass also calls async_stop after the
    # unload entry has gathered them; clean up properly so timers do not leak,
    # then raise on the first call only.
    call_state = {"raised": False}

    async def failing_stop() -> None:
        await original_stop()
        if not call_state["raised"]:
            call_state["raised"] = True
            raise boom

    with patch.object(failing_mgr, "async_stop", failing_stop):
        with caplog.at_level(logging.WARNING):
            await hass.config_entries.async_unload(
                lock_code_manager_config_entry.entry_id
            )
            await hass.async_block_till_done()

    assert call_state["raised"]
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "WARNING"
    )
    # Sibling managers must still have been stopped even though one raised.
    for mgr in other_mgrs:
        assert not mgr._started


# Slot device lifecycle (issue #1399)


async def test_removing_slot_removes_its_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Dropping a slot from config retires its device, not just its entities.

    Home Assistant's registry cleanup never reaps these: the device stays
    attached to the LCM entry, so it outlived the slot indefinitely.
    """
    entry_id = lock_code_manager_config_entry.entry_id
    dev_reg = dr.async_get(hass)
    slot_2_identifiers = {(DOMAIN, f"{entry_id}|test2")}
    assert dev_reg.async_get_device(slot_2_identifiers) is not None

    new_config = copy.deepcopy(dict(lock_code_manager_config_entry.data))
    new_config[CONF_SLOTS].pop(2)
    assert hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    assert dev_reg.async_get_device(slot_2_identifiers) is None
    # The surviving slot and the entry's own device are untouched.
    assert dev_reg.async_get_device({(DOMAIN, f"{entry_id}|test1")}) is not None
    assert dev_reg.async_get_device({(DOMAIN, entry_id)}) is not None


@pytest.mark.parametrize("stale_slot", [99, 0, -1])
async def test_setup_prunes_devices_for_unconfigured_slots(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    stale_slot: int,
):
    """
    A device left behind by the pre-fix behavior is swept up on reload.

    Without this, everyone who already hit the bug would have to delete each
    stale device by hand. Negative and zero slots are covered because the
    slots YAML schema does not bound the key, and a slot the identifier
    parser cannot recover would be skipped by the sweep forever.
    """
    entry_id = lock_code_manager_config_entry.entry_id
    dev_reg = dr.async_get(hass)
    # Stand in for a slot removed by an older version: a device for a slot
    # that is not in the entry's config.
    stale = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}|{stale_slot}")},
        manufacturer="Lock Code Manager",
        name=f"Mock Title Code slot {stale_slot}",
        model="Code Slot",
    )
    assert dev_reg.async_get(stale.id) is not None

    await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done()

    assert dev_reg.async_get_device({(DOMAIN, f"{entry_id}|{stale_slot}")}) is None
    assert dev_reg.async_get_device({(DOMAIN, f"{entry_id}|test1")}) is not None


async def test_remove_config_entry_device_allows_only_unconfigured_slots(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    The Delete button appears for stale slot devices but not live ones.

    Deleting a configured slot's device would strand its entities, and the
    next reload would recreate it regardless.
    """
    entry_id = lock_code_manager_config_entry.entry_id
    dev_reg = dr.async_get(hass)

    configured = dev_reg.async_get_device({(DOMAIN, f"{entry_id}|test1")})
    assert configured is not None
    assert (
        await async_remove_config_entry_device(
            hass, lock_code_manager_config_entry, configured
        )
        is False
    )

    entry_device = dev_reg.async_get_device({(DOMAIN, entry_id)})
    assert entry_device is not None
    assert (
        await async_remove_config_entry_device(
            hass, lock_code_manager_config_entry, entry_device
        )
        is False
    )

    for stale_slot in (99, -1):
        stale = dev_reg.async_get_or_create(
            config_entry_id=entry_id,
            identifiers={(DOMAIN, f"{entry_id}|{stale_slot}")},
            manufacturer="Lock Code Manager",
            name=f"Mock Title Code slot {stale_slot}",
            model="Code Slot",
        )
        assert (
            await async_remove_config_entry_device(
                hass, lock_code_manager_config_entry, stale
            )
            is True
        ), f"slot {stale_slot} device should be deletable"


async def test_hard_refresh_usercodes_service_raises_on_lock_error(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The hard_refresh_usercodes service surfaces per-lock failures as one error.

    ``asyncio.gather(..., return_exceptions=True)`` isolates a failing lock
    from its siblings; the service handler collects any exceptions and
    re-raises a single ``HomeAssistantError`` summarizing them, rather than
    letting one bad lock silently swallow the whole request.
    """
    locks = lock_code_manager_config_entry.runtime_data.locks
    lock_2 = locks[LOCK_2_ENTITY_ID]

    with patch.object(
        lock_2,
        "async_hard_refresh_codes",
        AsyncMock(side_effect=Exception("simulated refresh failure")),
    ):
        with pytest.raises(HomeAssistantError) as exc_info:
            await hass.services.async_call(
                DOMAIN,
                SERVICE_HARD_REFRESH_USERCODES,
                {ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]},
                blocking=True,
            )

    # One line per failing lock, readable as-is.
    assert str(exc_info.value).endswith("simulated refresh failure")

    # LOCK_1's refresh still completed despite LOCK_2 raising.
    assert locks[LOCK_1_ENTITY_ID].service_calls["hard_refresh_codes"]


async def test_deobfuscate_log_service_errors_when_not_fully_set_up(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The deobfuscate_log service refuses to run without a usable instance_id.

    ``instance_id`` is populated once during ``async_setup`` and never
    cleared in normal operation. This simulates a call landing before that
    completes (or after it was wiped) to verify the service degrades to an
    explicit, actionable error instead of building a broken deobfuscation
    table.
    """
    original_instance_id = hass.data[DOMAIN]["instance_id"]
    hass.data[DOMAIN]["instance_id"] = ""
    try:
        with pytest.raises(HomeAssistantError, match="not fully set up yet"):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_DEOBFUSCATE_LOG,
                {ATTR_TEXT: "some log text"},
                blocking=True,
                return_response=True,
            )
    finally:
        hass.data[DOMAIN]["instance_id"] = original_instance_id


async def test_setup_entry_with_empty_data_uses_initial_listener_task(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """An entry persisted with empty ``data`` and full ``options`` still sets up.

    Every entry created through the config flow starts with populated
    ``data``, so the normal first-setup path always takes the "move data to
    options" branch in ``_setup_entry_after_start``. A config entry can also
    legitimately hold its config entirely under ``options`` with empty
    ``data`` (nothing currently produces this via the flow, but restored/
    hand-edited storage could) -- that takes the other branch, scheduling
    ``async_update_listener`` directly as a background task instead.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        options=BASE_CONFIG,
        unique_id="Empty Data Entry",
        title="Empty Data Entry",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert set(entry.runtime_data.locks) == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}
    # The listener consolidates options back into data, as normal.
    assert entry.data[CONF_SLOTS]
    assert not entry.options

    await hass.config_entries.async_unload(entry.entry_id)


async def test_async_unload_lock_skips_untracked_lock_entity_id(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """async_unload_lock is a no-op for a lock_entity_id no longer tracked.

    Guards the defensive ``if lock is None: continue`` in the per-lock loop
    -- calling it for an entity_id that was never (or is no longer) part of
    ``runtime_data.locks`` must not raise, and must leave the entry's real
    locks untouched.
    """
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    assert set(runtime_data.locks) == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}

    await async_unload_lock(hass, entry, lock_entity_id="lock.untracked")

    assert set(runtime_data.locks) == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}


async def test_new_lock_setup_failure_logged_and_lock_excluded(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """An unexpected exception during a new lock's setup is logged, not fatal.

    Structural/transport failures are caught inside ``async_setup_internal``
    and leave the lock degraded but present. An exception of any other type
    is a genuine bug; ``_async_setup_new_locks`` must still log it and drop
    just that lock, without blocking sibling locks in the same batch.
    """
    config = copy.deepcopy(BASE_CONFIG)
    config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Partial Locks", title="Partial Locks"
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    original_setup_internal = MockLCMLock.async_setup_internal

    async def _maybe_fail(self: MockLCMLock, config_entry) -> None:
        if self.lock.entity_id == LOCK_2_ENTITY_ID:
            raise RuntimeError("simulated unexpected setup failure")
        await original_setup_internal(self, config_entry)

    new_config = copy.deepcopy(config)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]

    caplog.set_level(logging.ERROR)
    with patch.object(MockLCMLock, "async_setup_internal", _maybe_fail):
        hass.config_entries.async_update_entry(entry, options=new_config)
        await hass.async_block_till_done()

    assert "Failed to set up lock" in caplog.text
    assert LOCK_2_ENTITY_ID not in entry.runtime_data.locks
    assert LOCK_1_ENTITY_ID in entry.runtime_data.locks

    await hass.config_entries.async_unload(entry.entry_id)


async def test_update_listener_slot_removal_handles_missing_and_failing_coordinators(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Slot removal tolerates a coordinator already gone or one that raises on stop.

    Covers two defensive branches in the ``slots_to_remove`` loop of
    ``async_update_listener``: a slot whose coordinator was already
    discarded is skipped cleanly, and a coordinator whose ``async_stop``
    raises is logged but does not block removal of the other slot.
    """
    caplog.set_level(logging.ERROR)
    runtime_data = lock_code_manager_config_entry.runtime_data
    assert 1 in runtime_data.slot_coordinators
    assert 2 in runtime_data.slot_coordinators

    # Simulate slot 1's coordinator already having been discarded.
    runtime_data.slot_coordinators.pop(1)

    # Slot 2's coordinator raises when stopped.
    boom = RuntimeError("simulated coordinator stop failure")
    slot_2_coordinator = runtime_data.slot_coordinators[2]
    with patch.object(slot_2_coordinator, "async_stop", side_effect=boom):
        new_config = copy.deepcopy(BASE_CONFIG)
        new_config[CONF_SLOTS] = {}
        hass.config_entries.async_update_entry(
            lock_code_manager_config_entry, options=new_config
        )
        await hass.async_block_till_done()

    # Both slots were removed from the registry despite the failure.
    assert 1 not in runtime_data.slot_coordinators
    assert 2 not in runtime_data.slot_coordinators
    assert not hass.states.async_entity_ids(Platform.TEXT)
    assert "slot 2 coordinator stop raised" in caplog.text


async def test_pairs_removed_skips_untracked_lock_and_logs_release_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Slot removal releases lock-side state per (lock, slot) pair, tolerating failures.

    Covers two edge cases in the ``pairs_removed`` loop of
    ``async_update_listener``: a lock still listed in config but absent
    from ``runtime_data.locks`` (e.g. it failed setup) is skipped rather
    than raising, and a ``LockDisconnected``/``LockOperationFailed`` from a
    present lock's ``async_release_managed_slot`` is logged as a warning
    without blocking the rest of the teardown.
    """
    config = copy.deepcopy(BASE_CONFIG)
    entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Release Test", title="Release Test"
    )
    entry.add_to_hass(hass)

    original_setup_internal = MockLCMLock.async_setup_internal

    async def _fail_lock_2(self: MockLCMLock, config_entry) -> None:
        if self.lock.entity_id == LOCK_2_ENTITY_ID:
            raise RuntimeError("simulated unexpected setup failure")
        await original_setup_internal(self, config_entry)

    with patch.object(MockLCMLock, "async_setup_internal", _fail_lock_2):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # LOCK_2 failed setup and was popped, but remains listed in config.
    assert LOCK_2_ENTITY_ID not in entry.runtime_data.locks
    assert LOCK_1_ENTITY_ID in entry.runtime_data.locks

    lock_1 = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    with patch.object(
        lock_1,
        "async_release_managed_slot",
        AsyncMock(side_effect=LockDisconnected("simulated disconnect")),
    ):
        new_config = copy.deepcopy(config)
        new_config[CONF_SLOTS].pop(1)
        caplog.set_level(logging.WARNING)
        hass.config_entries.async_update_entry(entry, options=new_config)
        await hass.async_block_till_done()

    assert "could not release slot 1" in caplog.text
    assert LOCK_1_ENTITY_ID in caplog.text

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_v3_to_v4_names_every_slot(hass: HomeAssistant) -> None:
    """v4 gives every slot a present, separator-free, entry-unique name.

    The name is becoming the identity Lock Code Manager keys on, so these
    three properties stop being cosmetic. A name the user already chose is
    left exactly as-is, because rewriting one would also rename that user on
    every lock that stores a user name.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                2: {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "5678"},
                3: {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "9012"},
                4: {CONF_NAME: "Ra|man", CONF_ENABLED: True, CONF_PIN: "3456"},
            },
        },
        unique_id="Name Migration Test",
        version=3,
    )
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.version == 4
    slots = config_entry.data[CONF_SLOTS]
    assert slots[1][CONF_NAME] == "User 1"  # was unnamed
    assert slots[2][CONF_NAME] == "Raman"  # user's choice, untouched
    assert slots[3][CONF_NAME] == "Raman 2"  # collided with slot 2
    assert slots[4][CONF_NAME] == "Ra man"  # separator stripped
    # Every other field survives.
    assert slots[1][CONF_PIN] == "1234"


async def test_migration_v3_to_v4_rewrites_identifiers_end_to_end(
    hass: HomeAssistant, mock_lock_config_entry, caplog
) -> None:
    """A real upgrade rewrites registry identifiers and keeps entity IDs.

    Exercises the full async_migrate_entry path against a registry seeded
    with pre-migration rows -- the shape every other test lacks, because
    they start empty and so never touch the upgrade path at all.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1234"},
            },
        },
        unique_id="Identifier Migration",
        version=3,
    )
    config_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    stale = ent_reg.async_get_or_create(
        "text", DOMAIN, f"{entry_id}|1|pin", config_entry=config_entry
    )
    original_entity_id = stale.entity_id

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.version == 4
    # The registry row survived, so the entity ID automations reference is
    # unchanged -- only the unique ID moved.
    survivor = ent_reg.async_get(original_entity_id)
    assert survivor is not None
    assert survivor.unique_id == f"{entry_id}|Raman|pin"
    assert "entity IDs are unchanged" in caplog.text


async def test_rename_reassigning_a_removed_users_name(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Removing a user and giving their name to another slot, in one update.

    The ordering case the whole rename design exists for. Renames applied
    before removals find the freed name still occupied and strand the rows;
    worse, the removal pass then deletes the device at that name, which by
    then belongs to the renamed slot. Removals must complete first.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "test1", CONF_ENABLED: True, CONF_PIN: "1234"},
                2: {CONF_NAME: "test2", CONF_ENABLED: True, CONF_PIN: "5678"},
            },
        },
        unique_id="Rename Over Removed",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    pin_entity = ent_reg.async_get_entity_id(
        "text", DOMAIN, f"{config_entry.entry_id}|test1|pin"
    )
    assert pin_entity is not None

    # Drop slot 2 and hand its name to slot 1, in a single submission.
    hass.config_entries.async_update_entry(
        config_entry,
        options={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "test2", CONF_ENABLED: True, CONF_PIN: "1234"},
            },
        },
    )
    await hass.async_block_till_done()

    # Slot 1's row moved onto the freed name, and it is the SAME row, so the
    # entity ID automations reference is unchanged.
    survivor = ent_reg.async_get(pin_entity)
    assert survivor is not None
    assert survivor.unique_id == f"{config_entry.entry_id}|test2|pin"


async def test_migration_uses_repaired_names_for_identifiers(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The identifier rewrite must use the names the repair just produced.

    Both halves of the version 3 to 4 migration run in one pass: unnamed and
    conflicting slots are given names, and identifiers then move onto those
    names. Reading the entry instead of the repaired slots would move
    identifiers onto the names the entry still holds -- for an unnamed slot,
    no name at all.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            # No name: the repair assigns "User 1", and the identifier must
            # land on that.
            CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}},
        },
        unique_id="Repaired Name Migration",
        version=3,
    )
    config_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    seeded = ent_reg.async_get_or_create(
        "text", DOMAIN, f"{entry_id}|1|pin", config_entry=config_entry
    ).entity_id

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.version == 4
    assert config_entry.data[CONF_SLOTS][1][CONF_NAME] == "User 1"
    assert ent_reg.async_get(seeded).unique_id == f"{entry_id}|User 1|pin"


async def test_rename_saved_while_entry_was_not_loaded(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A rename saved while the entry was failed is still applied at setup.

    The update listener is only registered while the entry is loaded, so an
    options-flow save made against a failed entry is never seen by it. Setup
    is then the only place holding both sides: data has the names the
    registry is still on, options has the ones the rest of setup is about to
    use.

    Without reconciling there, the orphan sweep sees the renamed user's
    device on a name no longer configured, removes it, and Home Assistant
    cascades that to every entity row on it -- the destruction this module
    exists to prevent.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {1: {CONF_NAME: "Alice", CONF_ENABLED: True, CONF_PIN: "1234"}},
        },
        # The unprocessed save: same slot, new name.
        options={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "Alicia", CONF_ENABLED: True, CONF_PIN: "1234"}
            },
        },
        unique_id="Rename While Failed",
        version=4,
    )
    config_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entry_id = config_entry.entry_id
    # The registry is still on the old name, as it would be.
    stale = ent_reg.async_get_or_create(
        "text", DOMAIN, f"{entry_id}|Alice|pin", config_entry=config_entry
    )
    device = dev_reg.async_get_or_create(
        config_entry_id=entry_id, identifiers={(DOMAIN, f"{entry_id}|Alice")}
    )

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Same rows, moved -- not deleted and re-created.
    survivor = ent_reg.async_get(stale.entity_id)
    assert survivor is not None
    assert survivor.unique_id == f"{entry_id}|Alicia|pin"
    moved = dev_reg.async_get_device(identifiers={(DOMAIN, f"{entry_id}|Alicia")})
    assert moved is not None and moved.id == device.id
