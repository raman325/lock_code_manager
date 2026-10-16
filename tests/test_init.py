"""Test init module."""

import asyncio
import copy
from datetime import timedelta
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.components.lovelace.const import CONF_RESOURCE_TYPE_WS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.const import (
    ATTR_CODE,
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN
from homeassistant.util import dt as dt_util, slugify

from custom_components import lock_code_manager
from custom_components.lock_code_manager import (
    _async_reclaim_entities_from_foreign_devices,
    _async_setup_new_locks,
    _setup_entry_after_start,
    async_migrate_entry,
    async_release_locks,
    async_remove_config_entry_device,
    async_remove_entry,
    async_unload_lock,
)
from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    ATTR_PIN_IN_SYNC,
    ATTR_TEXT,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOT,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    SERVICE_DELETE_USER,
    SERVICE_DEOBFUSCATE_LOG,
    SERVICE_HARD_REFRESH_USERCODES,
    STRATEGY_PATH,
    SUBENTRY_TYPE_USER,
)
from custom_components.lock_code_manager.entity import build_slot_device_info
from custom_components.lock_code_manager.ha import runtime as lock_runtime
from custom_components.lock_code_manager.ha.runtime import lock_registry
from custom_components.lock_code_manager.model.config import (
    EntryConfig,
    build_slot_device_identifier,
    build_slot_unique_id,
)
from custom_components.lock_code_manager.model.credentials import SlotCredential
from custom_components.lock_code_manager.model.exceptions import (
    LockBusy,
    LockDisconnected,
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.model.queries import get_entry_config
from custom_components.lock_code_manager.model.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
    identity,
)
from custom_components.lock_code_manager.model.user_migration import migrate_to_users
from custom_components.lock_code_manager.model.util import per_lock_issue_id
from custom_components.lock_code_manager.providers import BaseLock
from custom_components.lock_code_manager.providers._base import OPERATION_TIMEOUT
from custom_components.lock_code_manager.repairs import (
    AcknowledgeRepairFlow,
    async_create_fix_flow,
)
from custom_components.lock_code_manager.sync.slot import LIMIT

from .common import (
    BASE_CONFIG,
    CONFIG_ENTRY_VERSION,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    LOCK_DEVICE_DOMAIN,
    NO_PIN_CAPABILITIES,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_IN_SYNC_ENTITY,
    SLOT_1_NAME_ENTITY,
    TICK_INTERVAL,
    MockLCMLock,
    MockLCMPushLock,
    async_advance_clock,
    async_advance_sync_ticks,
    async_blocking_stub,
    async_configure_options,
    async_disable_and_reload,
    async_discover_unclaimed_mqtt_lock,
    async_run_until_quiet,
    async_set_slot_pin,
    async_tick_until,
    code_entity_id,
    current_entry,
    entry_users,
    in_sync_entity_id,
    mock_lock,
    slot_entity_id,
    sync_status,
    user_subentries,
    when_mock_lock_built,
    write_entry_config,
)
from .conftest import (
    async_initial_tick,
    async_trigger_sync_tick,
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


def _failing_lock_build(lock_entity_id: str, error: BaseException):
    """
    Make building ``lock_entity_id``'s provider raise ``error``.

    Building the provider is the step of a lock's setup that can escape it:
    the provider's own setup is the lock's worker's to retry, and never
    drops the lock.
    """
    original = lock_runtime.async_create_lock_instance

    def _build(hass, dev_reg, ent_reg, config_entry, entity_id):
        if entity_id == lock_entity_id:
            raise error
        return original(hass, dev_reg, ent_reg, config_entry, entity_id)

    return patch.object(lock_runtime, "async_create_lock_instance", _build)


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
        device = dev_reg.async_get_device_by_identifier(
            (LOCK_DEVICE_DOMAIN, entity_id), mock_lock_entry_id
        )
        assert device
        assert device.config_entry_id == mock_lock_entry_id
        # Nothing of ours sits on the lock's own device. A device belongs to
        # one config entry as of HA 2026.8, so an entity parked on another
        # integration's device is one this integration cannot show.
        assert not [
            entry
            for entry in er.async_entries_for_device(ent_reg, device.id)
            if entry.config_entry_id == lcm_entry_id
        ]

    # The per-lock entities live on the slot device beside the shared ones.
    for slot in range(1, 3):
        slot_device = dev_reg.async_get_device_by_identifier(
            (DOMAIN, build_slot_device_identifier(lcm_entry_id, slot)),
            lcm_entry_id,
        )
        assert slot_device
        assert slot_device.config_entry_id == lcm_entry_id
        # Exact, so anything unexpected landing here is caught too.
        assert {
            entry.unique_id
            for entry in er.async_entries_for_device(
                ent_reg, slot_device.id, include_disabled_entities=True
            )
        } == {
            f"{lcm_entry_id}|{slot}|{key}|{entity_id}"
            for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
            for key in (ATTR_CODE, ATTR_IN_SYNC, ATTR_PIN_IN_SYNC)
        } | {
            f"{lcm_entry_id}|{slot}|{key}"
            for key in (
                CONF_ENABLED,
                CONF_NAME,
                CONF_PIN,
                ATTR_ACTIVE,
                EVENT_CREDENTIAL_USED,
            )
        }

    unique_ids = set()
    for slot in range(1, 3):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC, ATTR_PIN_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_CREDENTIAL_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

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

    # A hard refresh is a fresh read of the targeted lock only: the provider's
    # own read, as the mock lock does not read through a hard refresh.
    locks = lock_code_manager_config_entry.runtime_data.locks
    reads_before = {
        lock_entity_id: len(locks[lock_entity_id].service_calls["get_usercodes"])
        for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    }

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    assert (
        len(locks[LOCK_1_ENTITY_ID].service_calls["get_usercodes"])
        > reads_before[LOCK_1_ENTITY_ID]
    )
    assert (
        len(locks[LOCK_2_ENTITY_ID].service_calls["get_usercodes"])
        == reads_before[LOCK_2_ENTITY_ID]
    )

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

    assert write_entry_config(hass, lock_code_manager_config_entry, new_config)
    await hass.async_block_till_done()

    unique_ids = set()
    for slot in range(1, 4):
        for entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
            for key in (ATTR_CODE, ATTR_IN_SYNC, ATTR_PIN_IN_SYNC):
                unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{entity_id}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_CREDENTIAL_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

    assert unique_ids == {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(ent_reg, lcm_entry_id)
    }
    assert len(hass.states.async_entity_ids(Platform.BINARY_SENSOR)) == 9
    assert len(hass.states.async_entity_ids(Platform.EVENT)) == 3
    assert len(hass.states.async_entity_ids(Platform.SENSOR)) == 6
    assert len(hass.states.async_entity_ids(Platform.SWITCH)) == 3
    assert len(hass.states.async_entity_ids(Platform.TEXT)) == 6

    new_config = copy.deepcopy(new_config)
    new_config[CONF_SLOTS].pop(3)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]

    assert write_entry_config(hass, lock_code_manager_config_entry, new_config)
    await hass.async_block_till_done()

    # LOCK_2 was removed from the LCM config entry; its device keeps its own
    # config entry and no longer has any LCM entities linked to it.
    device = dev_reg.async_get_device_by_identifier(
        (LOCK_DEVICE_DOMAIN, LOCK_2_ENTITY_ID), mock_lock_entry_id
    )
    assert device
    assert device.config_entry_id == mock_lock_entry_id
    assert not [
        entry
        for entry in er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        )
        if entry.config_entry_id == lcm_entry_id
    ]

    unique_ids = set()
    for slot in range(1, 3):
        for key in (ATTR_CODE, ATTR_IN_SYNC, ATTR_PIN_IN_SYNC):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}|{LOCK_1_ENTITY_ID}")

        for key in (
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            ATTR_ACTIVE,
            EVENT_CREDENTIAL_USED,
        ):
            unique_ids.add(f"{lcm_entry_id}|{slot}|{key}")

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


async def test_an_entry_set_up_during_startup_reaches_its_lock_once_started(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #247: an entry set up while Home Assistant starts does not hold startup.

    Setting the entry up waited on its lock, so a lock whose integration was
    still coming up held Home Assistant's startup on "Initial setup for
    entities". The entry loads at once and leaves the lock alone; the lock
    is read, then written, once startup is over.
    """
    entry = current_entry("Set Up During Startup", {1: "4321"})
    entry.add_to_hass(hass)
    # Starting before any of Lock Code Manager is set up, as at boot.
    hass.set_state(CoreState.starting)
    with when_mock_lock_built(lambda lock: lock) as built:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        # A slow startup: a scan interval passes and the lock is still left alone.
        await async_advance_clock(hass, freezer, 60)
        assert built == []

        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
    [lock] = built
    assert lock.call_order[:2] == ["setup", "get_usercodes"]

    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        # A tick to compare, one to write and one to confirm, with headroom.
        max_ticks=6,
        what="slot 1 in sync once started",
    )
    assert lock.codes[1] == "4321"
    assert await hass.config_entries.async_unload(entry.entry_id)


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
    assert config_entry.version == 5

    # Read through the typed view: the entry is keyed by user now, and this
    # migration runs before that reshape in the same version bump.
    migrated = get_entry_config(config_entry)

    # Slot 1 should be unchanged (no calendar)
    assert CONF_CALENDAR not in migrated.slot(1)
    assert CONF_ENTITY_ID not in migrated.slot(1)

    # Slot 2 should have CONF_ENTITY_ID instead of CONF_CALENDAR
    assert CONF_CALENDAR not in migrated.slot(2)
    assert migrated.slot(2)[CONF_CONDITION] == "calendar.test_1"

    # Slot 3 already had both fields set -- calendar is dropped and the
    # pre-existing entity_id value is preserved untouched.
    assert CONF_CALENDAR not in migrated.slot(3)
    assert migrated.slot(3)[CONF_CONDITION] == "calendar.test_2"

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


async def test_two_entries_sharing_a_lock_at_startup_both_come_up(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #865: the second entry sharing a lock at startup gets its entities.

    Both entries set up while Home Assistant starts, and the lock's own setup
    takes a while. The second entry to reach the lock found it built but
    not yet set up, created none of its per-lock entities, and they stayed
    unavailable until a reload.
    """
    entries = [
        current_entry("Startup A", {1: "1234"}),
        current_entry("Startup B", {3: "0123"}),
    ]
    # Starting before any of Lock Code Manager is set up, as at boot.
    hass.set_state(CoreState.starting)
    with when_mock_lock_built(
        lambda lock: lock.slow("setup", seconds=5, times=1)
    ) as slow_setups:
        for entry in entries:
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await async_run_until_quiet()
        [slow_setup] = slow_setups
        await slow_setup.async_wait_held()
        await async_advance_clock(hass, freezer, 6)

    per_lock = [
        (in_sync_entity_id(hass, entry, slot), code_entity_id(hass, entry, slot))
        for entry, slot in zip(entries, (1, 3), strict=True)
    ]
    await async_tick_until(
        hass,
        freezer,
        lambda: all(
            (state := hass.states.get(in_sync)) is not None and state.state == STATE_ON
            for in_sync, _code in per_lock
        ),
        # The second entry's slot waits on a debounced read of the lock first.
        max_ticks=8,
        what="both entries' slots in sync",
    )
    assert [hass.states.get(code).state for _in_sync, code in per_lock] == [
        "1234",
        "0123",
    ]
    for entry in entries:
        assert await hass.config_entries.async_unload(entry.entry_id)


def _per_lock_entities(
    hass: HomeAssistant, entry: MockConfigEntry, lock_entity_id: str
) -> list[str | None]:
    """Return a lock's in-sync entity on each of the entry's slots, None if not made."""
    ent_reg = er.async_get(hass)
    return [
        ent_reg.async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            build_slot_unique_id(entry.entry_id, slot, ATTR_IN_SYNC, lock_entity_id),
        )
        for slot in (1, 2)
    ]


@pytest.mark.parametrize("setup_outcome", ["answers", "times_out"])
async def test_a_lock_slow_to_set_up_at_startup_holds_back_only_its_own_entities(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
    setup_outcome: str,
):
    """
    A lock whose setup does not answer at startup keeps no other entity waiting.

    Every entity of the entry waited on every lock's setup, which has ten
    minutes to answer: an entry with one such lock came up loaded with none
    of its entities, the switch and text ones included, until that setup
    answered or gave up. Now the entry's own entities, and those of each lock
    that answers, come at once; the slow lock's come when its setup is done.
    """
    entry = current_entry(
        "Slow Lock", {1: "1234", 2: "5678"}, locks=(LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    )
    entry.add_to_hass(hass)
    hass.set_state(CoreState.starting)
    with when_mock_lock_built(
        lambda lock: (
            lock.hang("setup") if lock.lock.entity_id == LOCK_1_ENTITY_ID else None
        )
    ) as faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await async_run_until_quiet()
        [hung, _] = faults
        await hung.async_wait_held()

        # The entry's own entities, and the answering lock's, sync on time.
        await async_tick_until(
            hass,
            freezer,
            lambda: (
                all(
                    hass.states.get(in_sync).state == STATE_ON
                    for in_sync in _per_lock_entities(hass, entry, LOCK_2_ENTITY_ID)
                    if in_sync is not None
                )
                and None not in _per_lock_entities(hass, entry, LOCK_2_ENTITY_ID)
            ),
            max_ticks=8,
            what="the answering lock's slots in sync",
        )
        assert hass.states.get(
            slot_entity_id(hass, SWITCH_DOMAIN, entry, 1, CONF_ENABLED)
        )
        assert _per_lock_entities(hass, entry, LOCK_1_ENTITY_ID) == [None, None]

        # Visible in the log well before the setup's deadline.
        await async_advance_clock(hass, freezer, 60, settle=False)
        assert "still waiting for lock.test_1 to answer its setup" in caplog.text
        assert "still waiting for lock.test_2" not in caplog.text

        if setup_outcome == "times_out":
            await async_advance_clock(hass, freezer, OPERATION_TIMEOUT, settle=False)
            # Given up on: its entities come, and say they have nothing yet.
            in_sync = _per_lock_entities(hass, entry, LOCK_1_ENTITY_ID)
            assert None not in in_sync
            assert {hass.states.get(e).state for e in in_sync} == {STATE_UNAVAILABLE}
        hung.clear()
        await async_run_until_quiet()

    in_sync = _per_lock_entities(hass, entry, LOCK_1_ENTITY_ID)
    assert None not in in_sync
    await async_tick_until(
        hass,
        freezer,
        lambda: all(hass.states.get(e).state == STATE_ON for e in in_sync),
        max_ticks=30,
        what="the slow lock's slots in sync once it answers",
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_condition_turning_while_a_lock_sets_up_reaches_that_lock(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A slot's condition turning on while its lock is still setting up reaches the lock.

    The lock is handed every slot's want when the entry starts holding it,
    and a want that changes is told to the locks the entry holds. One that
    changed in between was told to neither: the slot's PIN never reached a
    lock that set up slowly, until the condition turned again.
    """
    condition = "input_boolean.slot_1_condition"
    hass.states.async_set(condition, STATE_OFF)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_1_ENTITY_ID]},
        subentries_data=user_subentries(
            {
                1: {
                    CONF_NAME: "user1",
                    CONF_PIN: "1234",
                    CONF_ENABLED: True,
                    CONF_CONDITION: condition,
                }
            }
        ),
        unique_id="Slow Lock Condition",
        title="Slow Lock Condition",
        version=CONFIG_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)
    hass.set_state(CoreState.starting)
    with when_mock_lock_built(lambda lock: lock.hang("setup")) as faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await async_run_until_quiet()
        [hung] = faults
        await hung.async_wait_held()

        hass.states.async_set(condition, STATE_ON)
        await async_run_until_quiet()
        hung.clear()
        await async_run_until_quiet()

    await async_tick_until(
        hass,
        freezer,
        lambda: mock_lock(entry, LOCK_1_ENTITY_ID).codes.get(1) == "1234",
        max_ticks=5,
        what="the slot's PIN on the lock",
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


def _entry_with_shared_lock(unique_id: str, slot_num: int) -> MockConfigEntry:
    """Build an unloaded LCM entry managing LOCK_1 on its own slot."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                slot_num: {
                    CONF_NAME: f"shared{slot_num}",
                    CONF_PIN: "0123",
                    CONF_ENABLED: True,
                },
            },
        },
        unique_id=unique_id,
        title=unique_id,
    )


async def test_shared_lock_unloads_when_only_sibling_is_unloaded(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """The last loaded entry tears the shared lock down, ignoring unloaded siblings."""
    entry_a = _entry_with_shared_lock("Shared A", 1)
    entry_b = _entry_with_shared_lock("Shared B", 3)
    for entry in (entry_a, entry_b):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    shared_lock = entry_a.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert shared_lock is entry_b.runtime_data.locks[LOCK_1_ENTITY_ID]

    # B goes away while A still owns the lock: the instance must survive.
    await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()
    assert shared_lock._config_entry_state_unsub is not None

    # Now nothing loaded uses the lock. B merely *existing* must not keep the
    # provider's listeners alive with no owner.
    await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()
    assert shared_lock._config_entry_state_unsub is None


async def test_reload_with_unloaded_sibling_does_not_leave_two_instances(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Reloading past an unloaded sibling replaces the shared lock, not duplicates it."""
    entry_a = _entry_with_shared_lock("Reload A", 1)
    entry_b = _entry_with_shared_lock("Reload B", 3)
    for entry in (entry_a, entry_b):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    original_lock = entry_a.runtime_data.locks[LOCK_1_ENTITY_ID]

    await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_reload(entry_a.entry_id)
    await hass.async_block_till_done()

    # The reload builds a fresh instance because no loaded entry holds one, so
    # the pre-reload instance must have been torn down -- two live instances
    # means two push subscriptions and duplicate events per physical code use.
    assert entry_a.runtime_data.locks[LOCK_1_ENTITY_ID] is not original_lock
    assert original_lock._config_entry_state_unsub is None

    await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()


async def test_reload_with_loaded_sibling_keeps_sharing_the_instance(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A loaded sibling still keeps the shared lock alive across a reload."""
    entry_a = _entry_with_shared_lock("Keep A", 1)
    entry_b = _entry_with_shared_lock("Keep B", 3)
    for entry in (entry_a, entry_b):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    shared_lock = entry_b.runtime_data.locks[LOCK_1_ENTITY_ID]

    await hass.config_entries.async_reload(entry_a.entry_id)
    await hass.async_block_till_done()

    assert entry_a.runtime_data.locks[LOCK_1_ENTITY_ID] is shared_lock
    assert shared_lock._config_entry_state_unsub is not None

    for entry in (entry_a, entry_b):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_loaded_sibling_without_an_instance_does_not_block_teardown(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A sibling that dropped its lock during setup does not own it anymore."""
    entry_b = _entry_with_shared_lock("Dropped B", 3)
    entry_b.add_to_hass(hass)
    # No provider claims the lock for this setup, so _async_setup_new_locks
    # drops it and reports it -- while the entry itself still loads.
    with patch.dict(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {},
        clear=True,
    ):
        assert await hass.config_entries.async_setup(entry_b.entry_id)
        await hass.async_block_till_done()

    assert entry_b.state is ConfigEntryState.LOADED
    assert LOCK_1_ENTITY_ID not in entry_b.runtime_data.locks

    entry_a = _entry_with_shared_lock("Dropped A", 1)
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()

    shared_lock = entry_a.runtime_data.locks[LOCK_1_ENTITY_ID]

    # B is loaded and still lists the lock in its config, but holds no
    # instance -- so it cannot be the reason to keep this one alive.
    await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()
    assert shared_lock._config_entry_state_unsub is None

    await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()


async def test_lock_leaving_one_entry_is_not_removed_permanently(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Another entry still managing the lock means it is not leaving LCM."""
    entry_a = _entry_with_shared_lock("Keeper A", 1)
    entry_b = _entry_with_shared_lock("Keeper B", 3)
    for entry in (entry_a, entry_b):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    shared_lock = entry_a.runtime_data.locks[LOCK_1_ENTITY_ID]

    await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()

    # Dropping the lock from A's options tears the instance down, but B still
    # manages it, so the state that outlives a reload -- the per-lock
    # setup-failed repair, the provider's stored codes -- has to survive.
    await async_unload_lock(hass, entry_a, [LOCK_1_ENTITY_ID], remove_permanently=True)
    await hass.async_block_till_done()

    assert shared_lock.service_calls["unload"] == [(False,)]

    await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()


async def test_dropping_a_lock_another_entry_manages_keeps_its_repair(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for PR #1482: dropping a lock from one entry keeps what another relies on.

    The lock was taken out of one entry through its options while the other
    entry managing it was unloaded. It was torn down as though it were
    leaving Lock Code Manager altogether, which deletes the state meant to
    outlive an unload: the provider's stored codes, and the repair saying
    the lock failed its setup -- which still holds for the other entry.
    """
    issue_reg = ir.async_get(hass)
    setup_issue = per_lock_issue_id("lock_setup_failed", LOCK_1_ENTITY_ID)
    entry_a = current_entry(
        "Dropping A", {1: "1234"}, (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    )
    entry_b = current_entry("Keeping B", {3: "0123"})
    # Lock 1 reports no room for PIN codes, so it fails its setup check.
    with when_mock_lock_built(
        lambda lock: (
            setattr(lock, "capabilities", NO_PIN_CAPABILITIES)
            if lock.lock.entity_id == LOCK_1_ENTITY_ID
            else None
        )
    ):
        for entry in (entry_a, entry_b):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    lock = mock_lock(entry_a)
    assert issue_reg.async_get_issue(DOMAIN, setup_issue) is not None
    assert await hass.config_entries.async_unload(entry_b.entry_id)
    await hass.async_block_till_done()

    started = await hass.config_entries.options.async_init(entry_a.entry_id)
    result = await async_configure_options(
        hass, started["flow_id"], {CONF_LOCKS: [LOCK_2_ENTITY_ID]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry_a.data[CONF_LOCKS] == [LOCK_2_ENTITY_ID]
    assert lock.service_calls["unload"] == [(False,)]
    assert issue_reg.async_get_issue(DOMAIN, setup_issue) is not None
    assert await hass.config_entries.async_unload(entry_a.entry_id)


async def test_dropping_one_lock_leaves_the_other_locks_codes_alone(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #1538: removing one lock from an entry leaves its other lock alone.

    Reported as the credentials on the lock that stayed being wiped when the
    other lock was removed through the options. Never reproduced, so this
    pins what has to hold: nothing is cleared or rewritten on the lock that
    stays, and its slots stay in sync.
    """
    pins = {1: "1357", 2: "2468"}
    entry = current_entry("Two Locks", pins, (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID))
    entry.add_to_hass(hass)
    # Both locks already hold the entry's codes, which are not the mock's own.
    with when_mock_lock_built(lambda lock: setattr(lock, "codes", dict(pins))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    kept = mock_lock(entry, LOCK_1_ENTITY_ID)
    in_syncs = [in_sync_entity_id(hass, entry, slot) for slot in (1, 2)]
    await async_advance_sync_ticks(hass, freezer)
    assert [hass.states.get(in_sync).state for in_sync in in_syncs] == [STATE_ON] * 2

    started = await hass.config_entries.options.async_init(entry.entry_id)
    result = await async_configure_options(
        hass, started["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await async_advance_sync_ticks(hass, freezer, 5)

    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    # Still the instance the entry writes through, so nothing went elsewhere.
    assert mock_lock(entry, LOCK_1_ENTITY_ID) is kept
    assert kept.codes == pins
    assert {"set_usercode", "clear_usercode"}.isdisjoint(kept.call_order)
    assert [hass.states.get(in_sync).state for in_sync in in_syncs] == [STATE_ON] * 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def _async_unload_cleanly(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> MockLCMLock:
    """Unload ``entry``, checking it ends unloaded with nothing logged as an error."""
    lock = mock_lock(entry)
    caplog.clear()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ] == []
    return lock


async def test_unloading_beside_a_sibling_disabled_since_startup_unloads_cleanly(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """
    Regression for PR #251: an entry unloads cleanly beside a disabled sibling on its lock.

    Unloading asked every entry configuring the lock about the lock it was
    holding, and a sibling disabled since before it ever loaded had nothing
    to answer with: the unload raised.
    """
    entry_a = current_entry("Unloading A", {1: "1234"})
    entry_b = current_entry("Disabled B", {3: "0123"})
    entry_b.add_to_hass(hass)
    await hass.config_entries.async_set_disabled_by(
        entry_b.entry_id, ConfigEntryDisabler.USER
    )
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)
    await hass.async_block_till_done()
    assert entry_b.state is ConfigEntryState.NOT_LOADED

    lock = await _async_unload_cleanly(hass, entry_a, caplog)

    assert lock.service_calls["unload"] == [(False,)]


async def test_a_sibling_disabled_while_sharing_the_lock_leaves_it_to_the_other(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """
    A sibling disabled while sharing the lock lets go of it; the other entry tears it down.

    Disabling the sibling unloads it without touching the lock the other
    entry still holds, and that entry's own unload is then the one that
    tears the lock down -- once, and cleanly.
    """
    entry_a = current_entry("Unloading A", {1: "1234"})
    entry_b = current_entry("Disabled B", {3: "0123"})
    for entry in (entry_a, entry_b):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.config_entries.async_set_disabled_by(
        entry_b.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert entry_b.state is ConfigEntryState.NOT_LOADED
    assert mock_lock(entry_a).service_calls["unload"] == []

    lock = await _async_unload_cleanly(hass, entry_a, caplog)

    assert lock.service_calls["unload"] == [(False,)]


async def test_a_reload_beside_an_unloaded_sibling_leaves_one_push_subscription(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #1476: reloading beside an unloaded sibling keeps one subscription.

    The lock was torn down only when no other entry listed it, so the entry
    reloading beside an unloaded sibling kept its old instance subscribed --
    and, finding no loaded entry to share with, built a second one. Two
    subscriptions to one lock reported every use of a code twice.
    """
    entry_a = current_entry("Push A", {1: "1234"})
    entry_b = current_entry("Push B", {3: "0123"})
    with (
        patch.dict(
            "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
            {LOCK_DEVICE_DOMAIN: MockLCMPushLock},
        ),
        when_mock_lock_built(lambda lock: lock) as built,
    ):
        for entry in (entry_a, entry_b):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry_b.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(entry_a.entry_id)
        await hass.async_block_till_done()

    # One per load of entry A; entry B shared the first.
    assert len(built) == 2
    assert [lock.subscribe_calls - lock.unsubscribe_calls for lock in built] == [0, 1]

    assert await hass.config_entries.async_unload(entry_a.entry_id)
    await hass.async_block_till_done()
    assert [lock.subscribe_calls - lock.unsubscribe_calls for lock in built] == [0, 0]


def _cannot_subscribe(lock: MockLCMPushLock) -> MockLCMPushLock:
    """Make a push lock's subscription unimplemented, and return the lock."""
    lock.subscribe_error = ProviderNotImplementedError(lock, "setup_push_subscription")
    return lock


def _require(condition: bool, what: str) -> None:
    """
    Fail with ``Failed`` unless ``condition``: a broken setup, not the defect.

    For tests that expect an ``AssertionError``, so one whose setup went
    wrong cannot pass as the failure it was expecting.
    """
    if not condition:
        pytest.fail(what)


async def test_a_lock_whose_setup_failed_never_sets_up_again_after_unload(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A lock dropped after its state listener started never runs its setup again.

    A push lock whose subscription is not implemented fails its setup after
    the listener on its integration's config entry is registered. The entry
    used to drop the lock without removing that listener, so every reload of
    the lock's integration ran the dropped instance's provider setup again,
    after the entry had unloaded too. For a provider whose setup registers a
    listener of its own, that is a ghost listener and every event twice.
    The lock's worker now keeps a lock whose setup failed (it is the lock's
    to retry), and unloading the entry unloads its provider, listener and all.

    Only the last assertion is the defect; everything before it is the setup
    reaching the state the defect needs, and fails as ``Failed`` if it does
    not, so a broken setup is never mistaken for the expected failure.
    """
    entry = current_entry("Cannot Subscribe", {1: "1234"})
    entry.add_to_hass(hass)
    with (
        patch.dict(
            "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
            {LOCK_DEVICE_DOMAIN: MockLCMPushLock},
        ),
        when_mock_lock_built(_cannot_subscribe) as built,
    ):
        _require(await hass.config_entries.async_setup(entry.entry_id), "entry set up")
        await hass.async_block_till_done()
    _require(len(built) == 1, f"one lock built, not {len(built)}")
    [dropped] = built
    _require(entry.state is ConfigEntryState.LOADED, "entry loaded")
    _require(
        er.async_get(hass).async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            build_slot_unique_id(entry.entry_id, 1, ATTR_IN_SYNC, LOCK_1_ENTITY_ID),
        )
        is not None,
        "the lock kept by the entry",
    )
    _require(
        dropped.service_calls["setup"] == [(entry.entry_id,)], "one provider setup"
    )
    _require(await hass.config_entries.async_unload(entry.entry_id), "entry unloaded")
    await hass.async_block_till_done()

    _require(
        await hass.config_entries.async_reload(mock_lock_config_entry.entry_id),
        "lock integration reloaded",
    )
    await hass.async_block_till_done()

    assert dropped.service_calls["setup"] == [(entry.entry_id,)]


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
    write_entry_config(hass, lock_code_manager_config_entry, new_config)
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
    write_entry_config(hass, lock_code_manager_config_entry, new_config)
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
    assert LEGACY_NUMBER_OF_USES_KEY not in get_entry_config(entry).slot(1)
    assert LEGACY_NUMBER_OF_USES_KEY not in get_entry_config(entry).slot(3)

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


async def test_a_reload_keeps_the_repair_for_a_slot_suspended_by_real_failures(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Regression for PR #1243: a reload keeps a suspended slot's repair.

    The suspension came from writes that really failed and the lock still
    fails them, but unloading deleted the repair, so a reload or a restart
    hid the problem until the slot failed its way to suspended again.
    """
    entry = lock_code_manager_config_entry
    issue_reg = ir.async_get(hass)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    await async_set_slot_pin(hass, entry, 1, "4321")
    # The lock fails every write of slot 1 until the slot is held.
    mock_lock(entry).fail("set", 1)
    await async_tick_until(
        hass,
        freezer,
        lambda: sync_status(hass, in_sync) == "suspended",
        max_ticks=2 * LIMIT + 3,
        what="slot 1 suspended",
    )
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    # The lock built anew fails the same writes.
    with when_mock_lock_built(
        lambda lock: (
            lock.fail("set", 1) if lock.lock.entity_id == LOCK_1_ENTITY_ID else None
        )
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await async_tick_until(
        hass,
        freezer,
        lambda: sync_status(hass, in_sync) == "suspended",
        max_ticks=2 * LIMIT + 3,
        what="slot 1 suspended again",
    )
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None
    assert mock_lock(entry).codes[1] == "1234"


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
    """A config entry reload starts the lock over with a fresh worker."""
    entry = lock_code_manager_config_entry

    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)
    old_worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert old_worker is not None

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # No entry holds the lock any more: its worker is gone with it.
    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is None

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    new_worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert new_worker is not None
    assert new_worker is not old_worker
    # The fresh worker read the lock and brought the slot in line.
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)
    assert new_worker.slots[1].tries == 0
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON


async def test_removing_lock_from_config_stops_its_worker(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Removing a lock from CONF_LOCKS removes it from runtime_data and cleans up entities."""
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    ent_reg = er.async_get(hass)

    # Verify both locks have workers and are running
    assert LOCK_1_ENTITY_ID in runtime_data.locks
    assert LOCK_2_ENTITY_ID in runtime_data.locks
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        worker = lock_registry(hass).worker(lock_entity_id)
        assert worker is not None and worker.started
    lock_2 = runtime_data.locks[LOCK_2_ENTITY_ID]

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

    # Verify LOCK_2 is gone from runtime_data, and nothing keeps syncing it
    assert LOCK_2_ENTITY_ID not in runtime_data.locks
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None
    # Its provider was unloaded for good (the lock left the entry), once.
    assert lock_2.service_calls["unload"] == [(True,)]

    # Verify LOCK_2's in-sync entities are removed (state no longer present)
    for entity in lock_2_in_sync_entities:
        state = hass.states.get(entity.entity_id)
        assert state is None, (
            f"Expected {entity.entity_id} to be removed, but state is {state}"
        )

    # Verify LOCK_1 is still running with its worker intact
    assert LOCK_1_ENTITY_ID in runtime_data.locks
    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is not None
    assert runtime_data.locks[LOCK_1_ENTITY_ID].service_calls["unload"] == []


async def test_two_entries_same_lock_share_suspension_and_recovery(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    An unreachable lock blocks another entry's out-of-sync slots on the same lock.

    Lock-level backoff is by design: entries sharing a lock share its one
    worker, so once the lock stops answering, nothing is sent to it for any
    entry until it answers again. Then every entry's slots are synced again.
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

    # Both entries share the same provider, and the one worker, for LOCK_1
    lock = entry_a.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert entry_b.runtime_data.locks[LOCK_1_ENTITY_ID] is lock
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None and 3 in worker.slots and 1 in worker.slots

    entry_b_in_sync_entity = in_sync_entity_id(hass, entry_b, 3)
    await async_initial_tick(hass, entry_b_in_sync_entity)
    assert hass.states.get(entry_b_in_sync_entity).state == STATE_ON
    assert lock.codes[3] == "0123"

    # The lock stops answering, right after it reported a keypad change.
    fault = lock.fail(None, error=LockDisconnected)
    lock.codes[3] = "different"
    lock._push_credential_update(3, SlotCredential.known("different"))
    await hass.async_block_till_done()
    for _ in range(3):
        await async_trigger_sync_tick(hass, entry_b_in_sync_entity)

    # Backing off: entry B's slot is out of sync and nothing reaches the lock.
    assert not worker.reachable
    assert hass.states.get(entry_b_in_sync_entity).state == STATE_OFF
    assert sync_status(hass, entry_b_in_sync_entity) == "out_of_sync"
    assert lock.codes[3] == "different"

    # Recovery: the lock answers again once the backoff is over.
    fault.clear()
    await async_advance_clock(hass, freezer, 61)
    assert worker.reachable
    assert hass.states.get(entry_b_in_sync_entity).state == STATE_ON
    assert lock.codes[3] == "0123"

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
    assert runtime_data.post_start_setup_done is True
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
    assert runtime_data.post_start_setup_done is False


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


async def _async_wedge_adding_lock_2(hass: HomeAssistant, entry) -> dict:
    """Drop lock 2, then write it back; return the config without it."""
    one_lock = copy.deepcopy(BASE_CONFIG)
    one_lock[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    hass.config_entries.async_update_entry(entry, options=one_lock)
    await hass.async_block_till_done()
    assert LOCK_2_ENTITY_ID not in entry.runtime_data.locks
    hass.config_entries.async_update_entry(entry, options=copy.deepcopy(BASE_CONFIG))
    return one_lock


async def _async_let_the_loop_run() -> None:
    """Give every ready task several turns."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_update_passes_run_one_at_a_time(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A pass that arrives while another is setting a lock up waits for it."""
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    wedged, entered, release = async_blocking_stub(BaseLock.async_setup_internal)
    with patch.object(BaseLock, "async_setup_internal", wedged):
        one_lock = await _async_wedge_adding_lock_2(hass, entry)
        await asyncio.wait_for(entered.wait(), timeout=5)
        # The release of the lock the first pass is still adding.
        hass.config_entries.async_update_entry(entry, options=one_lock)
        try:
            await _async_let_the_loop_run()
            # The first pass is still adding lock 2; the second waits for it.
            assert runtime_data.pass_lock.locked()
            assert LOCK_2_ENTITY_ID not in runtime_data.locks
        finally:
            release.set()
        await hass.async_block_till_done()

    assert LOCK_2_ENTITY_ID not in runtime_data.locks
    # Nothing keeps syncing lock 2: no entry holds it, so it has no worker.
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None
    assert (
        er.async_get(hass).async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            build_slot_unique_id(entry.entry_id, 1, ATTR_IN_SYNC, LOCK_2_ENTITY_ID),
        )
        is None
    )


async def test_unload_waits_for_the_pass_in_flight(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """An unload never overlaps a pass, and a pass queued behind it does nothing."""
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    wedged, entered, release = async_blocking_stub(BaseLock.async_setup_internal)
    with patch.object(BaseLock, "async_setup_internal", wedged):
        await _async_wedge_adding_lock_2(hass, entry)
        await asyncio.wait_for(entered.wait(), timeout=5)
        # Queued behind the wedged pass: a new user.
        with_slot_3 = copy.deepcopy(BASE_CONFIG)
        with_slot_3[CONF_SLOTS][3] = {
            CONF_NAME: "test3",
            ATTR_CODE: "4321",
            CONF_ENABLED: True,
        }
        assert write_entry_config(hass, entry, with_slot_3)
        unload = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        try:
            await _async_let_the_loop_run()
            assert not unload.done()
            # The pass adding lock 2 is still in flight.
            assert runtime_data.pass_lock.locked()
        finally:
            release.set()
        assert await unload
        await hass.async_block_till_done()

    assert runtime_data.locks == {}
    assert runtime_data.slot_coordinators == {}
    assert (
        er.async_get(hass).async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            build_slot_unique_id(entry.entry_id, 3, ATTR_IN_SYNC, LOCK_1_ENTITY_ID),
        )
        is None
    )


async def test_removing_a_lock_purges_its_disabled_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A per-lock entity disabled in the registry never loads, so nobody else removes it."""
    entry = lock_code_manager_config_entry
    ent_reg = er.async_get(hass)
    disabled_id = in_sync_entity_id(hass, entry, 1, LOCK_2_ENTITY_ID)
    await async_disable_and_reload(hass, entry, disabled_id)
    assert ent_reg.async_get(disabled_id) is not None

    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    hass.config_entries.async_update_entry(entry, options=new_config)
    await hass.async_block_till_done()

    assert ent_reg.async_get(disabled_id) is None
    assert LOCK_2_ENTITY_ID not in entry.runtime_data.locks


async def test_unload_stops_slot_coordinators_before_callbacks_and_platforms(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Unload sequence: stop slot coordinators -> fire lock-removed callbacks -> unload platforms."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    callbacks = runtime_data.callbacks
    coordinators = list(runtime_data.slot_coordinators.values())
    assert coordinators

    # Capture ordering: the slot coordinators -- what tells each lock what a
    # slot should hold -- must be stopped and out of the runtime before the
    # lock-removed callbacks fire.
    seen_at_lock_removed: list[tuple[int, bool]] = []

    def _on_lock_removed(_entity_id: str) -> None:
        seen_at_lock_removed.append(
            (
                len(runtime_data.slot_coordinators),
                any(coordinator._started for coordinator in coordinators),
            )
        )

    callbacks.register_lock_removed_handler(_on_lock_removed)

    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()

    # One callback per lock, each after every slot coordinator was stopped.
    assert len(seen_at_lock_removed) == 2
    assert all(seen == (0, False) for seen in seen_at_lock_removed)


async def test_unload_cuts_off_an_exchange_the_lock_never_answers(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Unload does not wait out a lock exchange that never answers: it is cut off."""
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    hung = lock.hang("set", 1)
    await async_set_slot_pin(hass, entry, 1, "9876")
    await asyncio.wait_for(hung.async_wait_held(), timeout=5)

    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    # The exchange gets the worker's stop grace (30s), then it is cut off.
    taken = await async_tick_until(
        hass,
        freezer,
        unload.done,
        max_ticks=int(30 / TICK_INTERVAL.total_seconds()) + 3,
        what="the unload",
        settle=False,
    )
    assert taken * TICK_INTERVAL.total_seconds() >= 28
    assert unload.result()
    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is None
    assert lock.codes[1] == "1234"
    hung.clear()


async def test_unload_logs_slot_coordinator_stop_exceptions(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Unload logs a slot coordinator stop that raises, and still stops the rest."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    failing, *others = runtime_data.slot_coordinators.values()
    assert others

    boom = RuntimeError("simulated stop failure")
    original_stop = failing.async_stop

    def failing_stop() -> None:
        original_stop()
        raise boom

    with patch.object(failing, "async_stop", failing_stop):
        assert await hass.config_entries.async_unload(
            lock_code_manager_config_entry.entry_id
        )
        await hass.async_block_till_done()

    assert runtime_data.slot_coordinators == {}
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )
    # Sibling coordinators must still have been stopped even though one raised.
    for coordinator in others:
        assert not coordinator._started


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
    slot_2_identifier = (DOMAIN, f"{entry_id}|2")
    assert (
        dev_reg.async_get_device_by_identifier(slot_2_identifier, entry_id) is not None
    )

    # Removing a user is removing their subentry; the slot they occupied is
    # freed with them.
    removed = get_entry_config(lock_code_manager_config_entry).name_for(2)
    subentry = next(
        subentry
        for subentry in lock_code_manager_config_entry.subentries.values()
        if subentry.title == removed
    )
    assert hass.config_entries.async_remove_subentry(
        lock_code_manager_config_entry, subentry.subentry_id
    )
    await hass.async_block_till_done()

    assert dev_reg.async_get_device_by_identifier(slot_2_identifier, entry_id) is None
    # The surviving slot and the entry's own device are untouched.
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, f"{entry_id}|1"), entry_id)
        is not None
    )
    # And there is no entry-level device left behind either.
    assert dev_reg.async_get_device_by_identifier((DOMAIN, entry_id), entry_id) is None


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

    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{entry_id}|{stale_slot}"), entry_id
        )
        is None
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, f"{entry_id}|1"), entry_id)
        is not None
    )


async def test_setup_prunes_entities_for_unconfigured_locks(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A lock dropped while the entry was unloaded leaves no entities behind.

    A loaded per-lock entity removes its own row when its lock goes, but one
    disabled in the registry never loads, and the update listener that would
    otherwise notice the drop is not registered while the entry is unloaded.
    """
    entry = lock_code_manager_config_entry
    ent_reg = er.async_get(hass)
    disabled = in_sync_entity_id(hass, entry, 1, LOCK_2_ENTITY_ID)
    kept = in_sync_entity_id(hass, entry, 1, LOCK_1_ENTITY_ID)
    ent_reg.async_update_entity(disabled, disabled_by=er.RegistryEntryDisabler.USER)
    ent_reg.async_update_entity(kept, disabled_by=er.RegistryEntryDisabler.USER)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    one_lock = copy.deepcopy(BASE_CONFIG)
    one_lock[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    assert write_entry_config(hass, entry, one_lock)
    await hass.async_block_till_done()
    assert ent_reg.async_get(disabled) is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert ent_reg.async_get(disabled) is None
    assert ent_reg.async_get(kept) is not None
    assert code_entity_id(hass, entry, 1, LOCK_1_ENTITY_ID)


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

    configured = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{entry_id}|1"), entry_id
    )
    assert configured is not None
    assert (
        await async_remove_config_entry_device(
            hass, lock_code_manager_config_entry, configured
        )
        is False
    )

    # A device of ours whose identifier names no slot is not something this
    # integration knows how to let go of.
    unrecognized = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, entry_id)},
        name="Not a slot",
    )
    assert (
        await async_remove_config_entry_device(
            hass, lock_code_manager_config_entry, unrecognized
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
    reads_before = len(locks[LOCK_1_ENTITY_ID].service_calls["get_usercodes"])

    with patch.object(
        lock_2,
        "async_get_usercodes",
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
    assert len(locks[LOCK_1_ENTITY_ID].service_calls["get_usercodes"]) > reads_before


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
    assert get_entry_config(entry).slots
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

    await async_unload_lock(hass, entry, ["lock.untracked"])

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

    new_config = copy.deepcopy(config)
    new_config[CONF_LOCKS] = [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]

    caplog.set_level(logging.ERROR)
    with _failing_lock_build(
        LOCK_2_ENTITY_ID, RuntimeError("simulated unexpected setup failure")
    ):
        hass.config_entries.async_update_entry(entry, options=new_config)
        await hass.async_block_till_done()

    assert "Failed to set up lock" in caplog.text
    assert LOCK_2_ENTITY_ID not in entry.runtime_data.locks
    assert LOCK_1_ENTITY_ID in entry.runtime_data.locks

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_lock_whose_setup_fails_part_way_is_torn_down_with_the_entry(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A lock whose setup failed part way stops being polled once the entry unloads.

    The second lock claims push updates but its provider never implemented
    the subscription, which fails its setup after its connection check and
    poll were started. The lock's worker keeps it (setup is the lock's to
    retry), so the entry unloading is what stops it: unless that unloads the
    provider, its connection check keeps polling the lock for good.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=copy.deepcopy(BASE_CONFIG), unique_id="No Subscription"
    )
    entry.add_to_hass(hass)
    connection_checks: list[MockLCMLock] = []
    original_is_device_available = MockLCMLock.async_is_device_available

    async def _note_connection_checks(self: MockLCMLock) -> bool:
        connection_checks.append(self)
        return await original_is_device_available(self)

    with (
        patch.object(
            MockLCMLock,
            "supports_push",
            property(lambda self: self.lock.entity_id == LOCK_2_ENTITY_ID),
        ),
        patch.object(MockLCMLock, "async_is_device_available", _note_connection_checks),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        lock_2 = mock_lock(entry, LOCK_2_ENTITY_ID)
        assert lock_2.service_calls["unload"] == []

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert lock_2.service_calls["unload"] == [(False,)]
        assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None

        connection_checks.clear()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert lock_2 not in connection_checks


async def test_an_entry_setting_up_while_another_unloads_joins_the_lock_it_holds(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    An entry setting up while another unloads shares the lock, which outlives the first.

    A lock has one worker and one provider at a time. While the first entry
    unloads it still holds the lock, so the second entry joins that same
    instance rather than building a second one beside it; the first letting
    go then leaves the lock up, read and written for the second. The first
    entry's unload is held up the way an update pass in flight holds it.
    """
    config = copy.deepcopy(BASE_CONFIG)
    config[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    unloading_entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Leaving")
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                3: {CONF_NAME: "second", CONF_PIN: "2468", CONF_ENABLED: True}
            },
        },
        unique_id="Arriving",
    )
    unloading_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(unloading_entry.entry_id)
    await hass.async_block_till_done()
    old_lock = unloading_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)

    pass_lock = unloading_entry.runtime_data.pass_lock
    await pass_lock.acquire()
    unload = hass.async_create_task(
        hass.config_entries.async_unload(unloading_entry.entry_id)
    )
    await asyncio.sleep(0)
    assert unloading_entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS

    second_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second_entry.entry_id)
    # Not settled by blocking: that would wait out the unload held up above.
    for _ in range(1000):
        if LOCK_1_ENTITY_ID in second_entry.runtime_data.locks:
            break
        await asyncio.sleep(0)
    assert second_entry.runtime_data.locks[LOCK_1_ENTITY_ID] is old_lock

    pass_lock.release()
    assert await unload
    await hass.async_block_till_done()

    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is worker
    assert old_lock.service_calls["unload"] == []
    # Still the second entry's: its slot is on the lock, and a read sees it.
    assert old_lock.codes[3] == "2468"
    old_lock.codes[9] = "1357"
    await worker.read((9,))
    assert lock_runtime.lock_codes(hass, LOCK_1_ENTITY_ID)[9] == "1357"

    assert await hass.config_entries.async_unload(second_entry.entry_id)
    await hass.async_block_till_done()
    assert old_lock.service_calls["unload"] == [(False,)]


def _sharing_entries() -> tuple[MockConfigEntry, MockConfigEntry]:
    """Build an entry on both locks and one sharing the second lock."""
    return (
        MockConfigEntry(
            domain=DOMAIN, data=copy.deepcopy(BASE_CONFIG), unique_id="First Sharer"
        ),
        MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [LOCK_2_ENTITY_ID],
                CONF_SLOTS: {
                    3: {CONF_NAME: "second", CONF_PIN: "2468", CONF_ENABLED: True}
                },
            },
            unique_id="Second Sharer",
        ),
    )


async def test_two_entries_unloading_one_shared_lock_at_once_tear_it_down_once(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Two entries letting go of one lock at once tear it down exactly once.

    The first entry tears its unshared lock down beside the shared one, and
    that teardown is where the second entry takes the shared lock out -- the
    way any await in a real teardown would give it the turn. Only the last
    holder out stops the lock's worker and unloads its provider.
    """
    first_entry, second_entry = _sharing_entries()
    original_unload = MockLCMLock.async_unload
    unshared: list[MockLCMLock] = []

    async def _let_the_other_entry_let_go_first(
        self: MockLCMLock, remove_permanently: bool
    ) -> None:
        if self in unshared:
            for _ in range(100):
                # Home Assistant drops the runtime data once an unload ends.
                runtime_data = getattr(second_entry, "runtime_data", None)
                if runtime_data is None or LOCK_2_ENTITY_ID not in runtime_data.locks:
                    break
                await asyncio.sleep(0)
            else:
                pytest.fail("the other entry never let go of the shared lock")
        await original_unload(self, remove_permanently)

    with patch.object(MockLCMLock, "async_unload", _let_the_other_entry_let_go_first):
        for entry in (first_entry, second_entry):
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        assert list(first_entry.runtime_data.locks) == [
            LOCK_1_ENTITY_ID,
            LOCK_2_ENTITY_ID,
        ]
        unshared.append(first_entry.runtime_data.locks[LOCK_1_ENTITY_ID])
        lock = first_entry.runtime_data.locks[LOCK_2_ENTITY_ID]
        assert second_entry.runtime_data.locks[LOCK_2_ENTITY_ID] is lock

        assert all(
            await asyncio.gather(
                hass.config_entries.async_unload(first_entry.entry_id),
                hass.config_entries.async_unload(second_entry.entry_id),
            )
        )
        await hass.async_block_till_done()

    assert lock.service_calls["unload"] == [(False,)]
    assert unshared[0].service_calls["unload"] == [(False,)]
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None


async def test_a_lock_an_unloading_entry_still_holds_outlives_another_entry(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    An entry still unloading keeps the lock it holds until it lets go.

    Holding counts whatever state the holder is in, so the other entry
    unloading first leaves the shared lock to the one still on its way out.
    The second entry's unload is held up the way an update pass in flight
    holds it.
    """
    first_entry, second_entry = _sharing_entries()
    for entry in (first_entry, second_entry):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = first_entry.runtime_data.locks[LOCK_2_ENTITY_ID]

    pass_lock = second_entry.runtime_data.pass_lock
    await pass_lock.acquire()
    second_unload = asyncio.create_task(
        hass.config_entries.async_unload(second_entry.entry_id)
    )
    await asyncio.sleep(0)
    assert second_entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
    assert second_entry.runtime_data.locks[LOCK_2_ENTITY_ID] is lock

    assert await hass.config_entries.async_unload(first_entry.entry_id)
    await hass.async_block_till_done()
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is not None
    assert lock.service_calls["unload"] == []

    pass_lock.release()
    assert await second_unload
    await hass.async_block_till_done()
    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None
    assert lock.service_calls["unload"] == [(False,)]


def _offline_issue(hass: HomeAssistant) -> ir.IssueEntry | None:
    """Return the first lock's lock-offline repair, if there is one."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, per_lock_issue_id("lock_offline", LOCK_1_ENTITY_ID)
    )


def _raise_offline_issue_for_a_new_lock(hass: HomeAssistant) -> None:
    """Raise the first lock's lock-offline repair, as a lock set up anew would."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        per_lock_issue_id("lock_offline", LOCK_1_ENTITY_ID),
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="lock_offline",
        translation_placeholders={"lock_entity_id": LOCK_1_ENTITY_ID},
    )


async def test_a_push_after_the_lock_is_torn_down_records_nothing(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A push that arrives once its lock is torn down changes nothing.

    It must neither bring back the gone lock's codes nor clear the
    lock-offline repair, which is by then the lock set up in its place's.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=copy.deepcopy(BASE_CONFIG), unique_id="Torn Down"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    pushed_to = lock.coordinator
    assert pushed_to is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    _raise_offline_issue_for_a_new_lock(hass)

    pushed_to.push_update({1: SlotCredential.known("0000")})
    lock._push_credential_update(1, SlotCredential.known("0000"))
    await hass.async_block_till_done()

    assert _offline_issue(hass) is not None
    assert lock_runtime.lock_codes(hass, LOCK_1_ENTITY_ID) == {}


async def test_a_shared_polled_lock_keeps_polling_after_the_entry_that_built_it_leaves(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A shared lock's worker belongs to no entry, so its polls outlive any one.

    The first entry builds the lock, the second shares it, and once the first
    unloads, the scheduled poll keeps reading the lock for the second. The
    code it is seen to read sits on a slot no entry manages, so nothing but
    the poll reads it.
    """
    first_entry, second_entry = _sharing_entries()
    for entry in (first_entry, second_entry):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = first_entry.runtime_data.locks[LOCK_2_ENTITY_ID]
    assert second_entry.runtime_data.locks[LOCK_2_ENTITY_ID] is lock
    assert not lock.supports_push

    assert await hass.config_entries.async_unload(first_entry.entry_id)
    await hass.async_block_till_done()
    # Lets any refresh already requested run first, so the read below can
    # only be the scheduled poll's.
    async_fire_time_changed(
        hass,
        dt_util.utcnow() + timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1),
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    lock.codes[9] = "1357"
    assert 9 not in lock_runtime.lock_codes(hass, LOCK_2_ENTITY_ID)
    async_fire_time_changed(
        hass, dt_util.utcnow() + lock.usercode_scan_interval + timedelta(seconds=1)
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert lock_runtime.lock_codes(hass, LOCK_2_ENTITY_ID)[9] == "1357"

    assert await hass.config_entries.async_unload(second_entry.entry_id)


@pytest.mark.parametrize("leave", ["disable", "reload"])
async def test_a_shared_polled_lock_sees_a_change_after_the_entry_that_built_it_leaves(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
    leave: str,
):
    """
    Regression for #1629: a shared lock is still polled after its builder leaves.

    The lock's coordinator belonged to the entry that built it, so disabling
    or reloading that entry shut it down, and the lock was never read again
    for the entry still sharing it: a code changed at the keypad stayed
    unseen. The second entry's slot is the only one of its own on the lock.
    """
    first_entry, second_entry = _sharing_entries()
    for entry in (first_entry, second_entry):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = mock_lock(second_entry, LOCK_2_ENTITY_ID)
    in_sync = in_sync_entity_id(hass, second_entry, 3, LOCK_2_ENTITY_ID)
    # The joining entry's slot waits on a debounced read of the lock first.
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=8,
        what="slot 3 in sync",
    )

    if leave == "disable":
        assert await hass.config_entries.async_set_disabled_by(
            first_entry.entry_id, ConfigEntryDisabler.USER
        )
    else:
        assert await hass.config_entries.async_reload(first_entry.entry_id)
    await hass.async_block_till_done()
    reads_before = len(lock.service_calls["get_usercodes"])
    lock.codes[3] = "0000"

    # The poll sees the change and the worker puts the code back at once.
    await async_tick_until(
        hass,
        freezer,
        lambda: lock.codes[3] == "2468",
        # A scan interval of ticks, and one more to act on the poll ending it.
        max_ticks=int(lock.usercode_scan_interval / TICK_INTERVAL) + 1,
        what="the keypad change seen on slot 3 and put right",
    )
    assert len(lock.service_calls["get_usercodes"]) > reads_before
    assert hass.states.get(in_sync).state == STATE_ON

    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)


async def _reads_over_scan_intervals(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lock: MockLCMLock,
    intervals: int,
) -> int:
    """
    Count the reads a lock answers over ``intervals`` scan intervals.

    Any refresh already requested runs first, so only what runs on a timer is
    counted: the scheduled poll, and the drift check when the lock has one.
    """
    # A poll reads in a background task: waited for too.
    freezer.tick(timedelta(seconds=REQUEST_REFRESH_DEFAULT_COOLDOWN + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    reads_before = len(lock.service_calls["get_usercodes"])
    for _ in range(intervals):
        freezer.tick(lock.usercode_scan_interval + timedelta(seconds=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
    return len(lock.service_calls["get_usercodes"]) - reads_before


def _entry_on_the_second_lock(
    slot_num: int | None, *, disable_polling: bool, unique_id: str | None = None
) -> MockConfigEntry:
    """
    Build an entry on the second lock, with one user whose code it already holds.

    Nothing is left to write, so no sync reads the lock back: every read a
    settled lock answers is on a timer. ``None`` builds an entry with no users
    at all, which gives the lock no entity of its own to request a refresh.
    """
    slots = (
        {}
        if slot_num is None
        else {
            slot_num: {
                CONF_NAME: f"user{slot_num}",
                # The code MockLCMLock starts out holding there.
                CONF_PIN: {1: "1234", 2: "5678"}[slot_num],
                CONF_ENABLED: True,
            }
        }
    )
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_2_ENTITY_ID], CONF_SLOTS: slots},
        unique_id=unique_id or f"Users {slot_num}",
        pref_disable_polling=disable_polling,
    )


def _locks_set_up(built: list[MockLCMLock]) -> list[MockLCMLock]:
    """Return the locks among ``built`` that ran provider setup."""
    return [built_lock for built_lock in built if built_lock.service_calls["setup"]]


async def _set_up(
    hass: HomeAssistant, *entries: MockConfigEntry, builds: int = 1
) -> MockLCMLock:
    """
    Set up ``entries`` in order and return the second lock, which they share.

    Shared means one fake lock set up once: ``builds`` is 1 when these
    entries bring the lock up and 0 when they join one an earlier call set
    up. Every entry reaches that same fake, and each user's code on it reads
    as the code the lock holds, so its read answered.
    """
    # Setup moves each entry's users out of its data.
    users = {entry.entry_id: dict(entry.data[CONF_SLOTS]) for entry in entries}
    with when_mock_lock_built(lambda built_lock: built_lock) as built:
        for entry in entries:
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
    lock = mock_lock(entries[0], LOCK_2_ENTITY_ID)
    assert _locks_set_up(built) == ([lock] if builds else [])
    assert all(mock_lock(entry, LOCK_2_ENTITY_ID) is lock for entry in entries)
    assert not lock.supports_push
    for entry in entries:
        for slot_num, slot in users[entry.entry_id].items():
            code = code_entity_id(hass, entry, slot_num, LOCK_2_ENTITY_ID)
            assert hass.states.get(code).state == slot[CONF_PIN]
    return lock


async def test_disable_polling_stops_the_scheduled_poll(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
):
    """An entry with "Disable polling" set has its lock read once, then never polled."""
    lock = await _set_up(hass, _entry_on_the_second_lock(1, disable_polling=True))

    assert await _reads_over_scan_intervals(hass, freezer, lock, 3) == 0

    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_disable_polling_leaves_the_drift_check_running(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
):
    """
    "Disable polling" stops the scheduled poll, not the drift check.

    The lock is still read fresh on its hard refresh interval, and that is
    every read it answers: every read is a fresh one, so a drift check is a
    read of the lock (the mock lock's own, as it reads without a hard
    refresh).
    """
    with patch.object(
        MockLCMLock,
        "hard_refresh_interval",
        new_callable=PropertyMock,
        return_value=timedelta(minutes=2),
    ):
        lock = await _set_up(hass, _entry_on_the_second_lock(1, disable_polling=True))

    # Four one-minute scan intervals: two two-minute drift checks.
    reads = await _reads_over_scan_intervals(hass, freezer, lock, 4)

    assert reads == 2

    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("first_disables", "second_slot", "second_disables", "polled"),
    [
        (True, 2, False, True),
        (True, None, False, True),
        (False, 2, True, True),
        (True, 2, True, False),
    ],
    ids=[
        "builder-disables",
        "builder-disables-joiner-has-no-users",
        "joiner-disables",
        "both-disable",
    ],
)
async def test_a_shared_lock_is_polled_unless_every_holder_disables_polling(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
    first_disables: bool,
    second_slot: int | None,
    second_disables: bool,
    polled: bool,
):
    """
    A lock two entries share is polled while either of them allows polling.

    The first entry builds the lock and the second shares it, so the cases
    name which side disables polling: one entry still allowing it keeps the
    lock polled once per scan interval, and only both disabling it stops that.
    A joiner with no users adds no entity that would refresh the lock, so its
    joining alone has to start the poll.
    """
    lock = await _set_up(
        hass,
        _entry_on_the_second_lock(1, disable_polling=first_disables),
        _entry_on_the_second_lock(second_slot, disable_polling=second_disables),
    )

    reads = await _reads_over_scan_intervals(hass, freezer, lock, 3)

    assert reads == (3 if polled else 0)

    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_re_enabling_polling_on_one_holder_resumes_the_poll(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
):
    """
    Turning "Disable polling" back off on one holder polls the shared lock again.

    Home Assistant saves the option and reloads that entry. The other entry
    keeps the lock's coordinator alive through the reload, and the reloaded
    entry has no users, so nothing but its rejoining can restart the poll.
    """
    keeper = _entry_on_the_second_lock(1, disable_polling=True)
    toggled = _entry_on_the_second_lock(None, disable_polling=True)
    lock = await _set_up(hass, keeper, toggled)
    assert await _reads_over_scan_intervals(hass, freezer, lock, 3) == 0

    hass.config_entries.async_update_entry(toggled, pref_disable_polling=False)
    with when_mock_lock_built(lambda built_lock: built_lock) as rebuilt:
        assert await hass.config_entries.async_reload(toggled.entry_id)
        await hass.async_block_till_done()
    assert _locks_set_up(rebuilt) == []
    assert mock_lock(toggled, LOCK_2_ENTITY_ID) is lock

    assert await _reads_over_scan_intervals(hass, freezer, lock, 3) == 3

    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_entry_joining_leaves_a_scheduled_poll_where_it_is(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
):
    """
    A poll already scheduled when another entry joins still runs when it was due.

    Rescheduling it on every join would push it a full interval later each
    time. The joiner has no users, so nothing else reads the lock.
    """
    lock = await _set_up(hass, _entry_on_the_second_lock(1, disable_polling=False))
    assert await _reads_over_scan_intervals(hass, freezer, lock, 1) == 1
    freezer.tick(lock.usercode_scan_interval / 2)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    reads_before = len(lock.service_calls["get_usercodes"])

    await _set_up(
        hass, _entry_on_the_second_lock(None, disable_polling=False), builds=0
    )
    freezer.tick(lock.usercode_scan_interval / 2 + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert len(lock.service_calls["get_usercodes"]) == reads_before + 1

    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_shared_lock_nothing_listens_to_is_not_polled(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer: FrozenDateTimeFactory,
):
    """
    A lock whose holders have no users is not polled, even once an entry joins.

    Home Assistant polls only for listeners, and no entity listens here, so
    the joiner allowing polling starts nothing.
    """
    lock = await _set_up(
        hass,
        _entry_on_the_second_lock(None, disable_polling=False, unique_id="Builder"),
        _entry_on_the_second_lock(None, disable_polling=False, unique_id="Joiner"),
    )

    assert await _reads_over_scan_intervals(hass, freezer, lock, 3) == 0

    for entry in hass.config_entries.async_entries(DOMAIN):
        assert await hass.config_entries.async_unload(entry.entry_id)


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
    # Whoever discards a coordinator has stopped it first; the removal path
    # cannot reach it once it is gone.
    runtime_data.slot_coordinators.pop(1).async_stop()

    # Slot 2's coordinator raises when stopped.
    boom = RuntimeError("simulated coordinator stop failure")
    slot_2_coordinator = runtime_data.slot_coordinators[2]
    with patch.object(slot_2_coordinator, "async_stop", side_effect=boom):
        new_config = copy.deepcopy(BASE_CONFIG)
        new_config[CONF_SLOTS] = {}
        write_entry_config(hass, lock_code_manager_config_entry, new_config)
        await hass.async_block_till_done()

    # Both slots were removed from the registry despite the failure.
    assert 1 not in runtime_data.slot_coordinators
    assert 2 not in runtime_data.slot_coordinators
    assert not hass.states.async_entity_ids(Platform.TEXT)
    assert "slot 2 coordinator stop raised" in caplog.text


async def test_settled_waits_for_the_last_overlapping_pass(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """``settled`` means no pass is still running, not that one finished.

    One service call is several entry writes -- a subentry per user, then
    the entry -- and Home Assistant schedules a listener task per write.
    They overlap, and the ones with nothing to do finish FIRST: only the
    earliest pass finds a diff, because it caches the new configuration
    before its first await. Settling on the first pass out therefore hands
    the waiter an entry whose entities are still being built.

    Driven directly rather than through a service, because which pass wins
    the race downstream is not something a test can pin: the assertion
    would pass either way and prove nothing.
    """
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    runtime_data.settled.clear()

    slow_pass_may_finish = asyncio.Event()

    async def _slow(hass_, config_entry_, old_config=None):
        await slow_pass_may_finish.wait()

    async def _fast(hass_, config_entry_, old_config=None):
        return None

    with patch.object(lock_code_manager, "_async_apply_entry_update", _slow):
        slow = hass.async_create_task(
            lock_code_manager.async_update_listener(hass, entry)
        )
        await asyncio.sleep(0)

    with patch.object(lock_code_manager, "_async_apply_entry_update", _fast):
        await lock_code_manager.async_update_listener(hass, entry)

    # The no-op pass is done; the one doing the work is not.
    assert not runtime_data.settled.is_set()

    slow_pass_may_finish.set()
    await slow
    assert runtime_data.settled.is_set()


@pytest.mark.parametrize(
    "release_error",
    [LockDisconnected("simulated disconnect"), LockBusy("simulated busy")],
    ids=["disconnected", "busy"],
)
async def test_pairs_removed_skips_untracked_lock_and_survives_release_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
    release_error: Exception,
):
    """Slot removal releases lock-side state per (lock, slot) pair, tolerating failures.

    Covers two edge cases in the ``pairs_removed`` loop of
    ``async_update_listener``: a lock still listed in config but absent
    from ``runtime_data.locks`` (e.g. it failed setup) is skipped rather
    than raising, and any provider error from a present lock's
    ``async_release_managed_slot`` -- unreachable, or merely busy behind
    another operation -- does not block the rest of the teardown: the
    release is the lock's worker's to retry.
    """
    config = copy.deepcopy(BASE_CONFIG)
    entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Release Test", title="Release Test"
    )
    entry.add_to_hass(hass)

    with _failing_lock_build(
        LOCK_2_ENTITY_ID, RuntimeError("simulated unexpected setup failure")
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # LOCK_2 failed setup and was popped, but remains listed in config.
    assert LOCK_2_ENTITY_ID not in entry.runtime_data.locks
    assert LOCK_1_ENTITY_ID in entry.runtime_data.locks

    lock_1 = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    slot_1_in_sync = in_sync_entity_id(hass, entry, 1, LOCK_1_ENTITY_ID)
    with patch.object(
        lock_1,
        "async_release_managed_slot",
        AsyncMock(side_effect=release_error),
    ) as release:
        new_config = copy.deepcopy(config)
        new_config[CONF_SLOTS].pop(1)
        write_entry_config(hass, entry, new_config)
        await hass.async_block_till_done()

        # The release was tried and failed; the rest of the teardown went on.
        assert release.await_args_list[0].args[0] == 1
        assert hass.states.get(slot_1_in_sync) is None
        assert 1 not in entry.runtime_data.slot_coordinators
        assert 2 in entry.runtime_data.slot_coordinators
        # The clear is still owed: the worker keeps the slot to retry it.
        worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
        assert worker is not None and worker.slots[1].release

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_options_submission_that_changes_nothing_still_settles(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    `options` is a staging area, and it is emptied even when nothing changed.

    The update listener returns early when the submission produces no diff,
    and that was the one path out of it that skipped settling. What was left
    behind read fine -- `options` holds what `data` holds -- right up until
    the next write cleared it, which would take anything that lived only
    there with it.
    """
    entry = lock_code_manager_config_entry
    locks = list(get_entry_config(entry).locks)
    assert entry.options == {}

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LOCKS: locks}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.options == {}
    assert entry.data[CONF_LOCKS] == locks


async def test_an_options_save_repairs_an_entry_that_failed_to_load(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    Removing the lock an entry cannot load without brings the entry back.

    A failed entry has no update listener, so nothing acts on an ordinary
    options save: the user is told it worked while the entry stays failed
    until Home Assistant restarts.
    """
    entry = lock_code_manager_config_entry
    await hass.config_entries.async_unload(entry.entry_id)
    er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Spied rather than observed: a successful reload's own orphan sweep
    # removes the same registry rows, so the end state cannot tell whether
    # the save released the lock.
    with patch(
        "custom_components.lock_code_manager.config_flow.async_release_locks",
        wraps=async_release_locks,
    ) as release:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"
    release.assert_awaited_once_with(hass, entry, [LOCK_2_ENTITY_ID])
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    assert entry.options == {}
    assert list(entry.runtime_data.locks) == [LOCK_1_ENTITY_ID]


async def test_an_options_save_leaves_a_loaded_entry_to_its_listener(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A loaded entry applies the save through its update listener, not a reload."""
    entry = lock_code_manager_config_entry
    ent_reg = er.async_get(hass)
    lock_2_rows = [
        entity_id(hass, entry, slot_num, LOCK_2_ENTITY_ID)
        for slot_num in BASE_CONFIG[CONF_SLOTS]
        for entity_id in (in_sync_entity_id, code_entity_id)
    ]
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    assert entry.options == {}
    assert list(entry.runtime_data.locks) == [LOCK_1_ENTITY_ID]
    assert all(ent_reg.async_get(row) is None for row in lock_2_rows)
    reload.assert_not_called()


@pytest.mark.parametrize("repair", ["options", "reauth"])
async def test_a_save_repairs_an_entry_whose_failed_setup_left_runtime_data(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    repair: str,
) -> None:
    """
    A setup reads the entry as it is now, not as a previous failed setup saw it.

    Home Assistant keeps `runtime_data` on an entry whose setup failed after
    assigning it, and unloading an entry that is not loaded keeps it too. A
    setup that read its configuration from there found the lock the save had
    just removed, and failed again.
    """
    entry = lock_code_manager_config_entry
    await hass.config_entries.async_unload(entry.entry_id)
    with patch(
        "custom_components.lock_code_manager._async_prune_orphaned_lock_entities",
        side_effect=RuntimeError,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert hasattr(entry, "runtime_data")
    er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    if repair == "options":
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
    else:
        [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    assert list(entry.runtime_data.locks) == [LOCK_1_ENTITY_ID]


async def _async_fail_setup_after_runtime_data(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    error: type[Exception] = RuntimeError,
) -> None:
    """Leave `entry` failed by a setup that got as far as assigning `runtime_data`."""
    await hass.config_entries.async_unload(entry.entry_id)
    with patch(
        "custom_components.lock_code_manager._async_prune_orphaned_lock_entities",
        side_effect=error,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is (
        ConfigEntryState.SETUP_RETRY
        if error is ConfigEntryNotReady
        else ConfigEntryState.SETUP_ERROR
    )
    assert hasattr(entry, "runtime_data")


@pytest.mark.parametrize(
    ("error", "unload"),
    [(RuntimeError, False), (ConfigEntryNotReady, False), (RuntimeError, True)],
    ids=["failed", "retrying", "unloaded"],
)
async def test_a_failed_setup_s_cached_config_is_not_the_entry_s_config(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    error: type[Exception],
    unload: bool,
) -> None:
    """
    An entry whose setup failed is read from what it stores.

    Home Assistant keeps `runtime_data` on an entry whose setup failed after
    assigning it, whether it gave up or is waiting to retry, and unloading an
    entry that is not loaded keeps it too. Its cached config is the failed
    run's, and predates anything saved since.
    """
    entry = lock_code_manager_config_entry
    await _async_fail_setup_after_runtime_data(hass, entry, error)
    if unload:
        await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert hasattr(entry, "runtime_data")

    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    assert get_entry_config(entry).locks == (LOCK_1_ENTITY_ID,)


async def test_the_options_form_offers_a_failed_entry_s_saved_locks(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """The form a user repairs a failed entry with starts from what it stores."""
    entry = lock_code_manager_config_entry
    await _async_fail_setup_after_runtime_data(hass, entry)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)

    [locks_key] = result["data_schema"].schema
    assert locks_key.default() == [LOCK_1_ENTITY_ID]


async def test_a_failed_entry_checks_a_lock_it_adds_against_the_users_it_saved_since(
    hass: HomeAssistant, mock_lock_config_entry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Regression for PR #1632: an entry that failed to load is checked against what it stores.

    Home Assistant keeps what a failed setup built, and the entry's settings
    were read from it: a user added to the entry after the failure was
    invisible to the options form, which let the entry take on a lock where
    another entry already held that user's number.
    """
    entry = current_entry("Failed To Load", {1: "1234"})
    entry.add_to_hass(hass)
    # A platform that will not import fails the setup partway through, after
    # it has built what Home Assistant then keeps. Only that one module is
    # hidden, and only for the setup.
    with monkeypatch.context() as hidden:
        hidden.setitem(sys.modules, f"{lock_code_manager.__name__}.event", None)
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    started = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
    )
    started = await hass.config_entries.subentries.async_configure(
        started["flow_id"], {"next_step_id": "one"}
    )
    added = await hass.config_entries.subentries.async_configure(
        started["flow_id"], {CONF_NAME: "Visitor", CONF_ENABLED: True, CONF_PIN: "2468"}
    )
    assert added["type"] is FlowResultType.CREATE_ENTRY
    # The lock holds a code on 2, so the visitor is issued 3.
    assert added["data"][CONF_SLOT] == 3
    other = current_entry("Holds Three", {3: "1357"}, (LOCK_2_ENTITY_ID,))
    other.add_to_hass(hass)

    started = await hass.config_entries.options.async_init(entry.entry_id)
    [locks_key] = started["data_schema"].schema
    assert locks_key.default() == [LOCK_1_ENTITY_ID]
    result = await async_configure_options(
        hass, started["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "slots_already_configured"}
    assert result["description_placeholders"]["common_slots"] == "3"


async def test_a_loaded_entry_answers_from_its_cached_config(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    A loaded entry, and one unloading, answer from the view last applied.

    The cache is set apart from the stored data here, so only a read that
    went through it can find it.
    """
    entry = lock_code_manager_config_entry
    cached = EntryConfig.from_mapping({CONF_LOCKS: [LOCK_1_ENTITY_ID]})
    entry.runtime_data.config = cached

    assert get_entry_config(entry) is cached

    unload_lock = lock_code_manager.async_unload_lock
    seen: list[tuple[ConfigEntryState, bool]] = []

    async def _recording_unload_lock(
        hass: HomeAssistant, config_entry: MockConfigEntry, *args, **kwargs
    ) -> None:
        seen.append((config_entry.state, get_entry_config(config_entry) is cached))
        await unload_lock(hass, config_entry, *args, **kwargs)

    with patch.object(lock_code_manager, "async_unload_lock", _recording_unload_lock):
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert seen == [(ConfigEntryState.UNLOAD_IN_PROGRESS, True)]


async def test_an_entry_that_failed_to_unload_is_read_from_what_it_stores(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    An entry stuck in FAILED_UNLOAD answers from its stored data.

    Its update listener survives the failed unload, but the unload marked the
    entry as unloading first, so every pass returns before refreshing the
    cache: what was applied last stops following what is saved.
    """
    entry = lock_code_manager_config_entry
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    stale = entry.runtime_data.config

    try:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        await hass.async_block_till_done()

        assert entry.runtime_data.config is stale
        assert stale.locks == (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
        assert get_entry_config(entry).locks == (LOCK_1_ENTITY_ID,)
    finally:
        # Home Assistant refuses to unload an entry in FAILED_UNLOAD, so it is
        # put back to LOADED for an unload that tears down what the failed one
        # left.
        entry.mock_state(hass, ConfigEntryState.LOADED)
        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_an_options_save_on_a_failed_entry_keeps_what_the_form_never_asked_about(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The failed-entry save rewrites `data`, so it has to carry the rest of it.

    The form has one field. Writing only that field drops every other key the
    entry holds, and nothing on the way to the reload would notice.
    """
    er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_CONFIG, "written_by_something_else": {"kept": True}},
        unique_id="carries_extra",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "locks_updated"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["written_by_something_else"] == {"kept": True}

    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_options_save_repairs_an_entry_waiting_to_retry_setup(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """An entry waiting to retry setup is saved and reloaded like a failed one."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="retrying")
    entry.add_to_hass(hass)
    with patch(
        "custom_components.lock_code_manager.async_setup_entry",
        side_effect=ConfigEntryNotReady,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("version", "legacy"),
    [
        (
            3,
            {
                CONF_SLOTS: {
                    1: {CONF_NAME: "Alice", CONF_ENABLED: True, CONF_PIN: "1234"}
                }
            },
        ),
        (
            4,
            {
                CONF_USERS: {"Alice": {CONF_ENABLED: True, CONF_PIN: "1234"}},
                CONF_SLOT_ASSIGNMENT: {"alice": 1},
            },
        ),
    ],
    ids=["slots", "users"],
)
async def test_an_options_save_keeps_the_users_of_an_entry_not_yet_migrated(
    hass: HomeAssistant, mock_lock_config_entry, version: int, legacy: dict
) -> None:
    """
    An entry that has not migrated still holds its users in its own data.

    A disabled entry is never set up, so it is never migrated either. Saving
    its locks by rewriting its data from the entry's own half would leave the
    migration nobody to move once the entry is enabled.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID], **legacy},
        version=version,
        disabled_by=ConfigEntryDisabler.USER,
        unique_id="not_migrated",
    )
    entry.add_to_hass(hass)
    assert entry.state is ConfigEntryState.NOT_LOADED

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.version == version
    assert all(entry.data[key] == value for key, value in legacy.items())
    assert set(get_entry_config(entry).users) == {"Alice"}
    assert list(get_entry_config(entry).locks) == [LOCK_1_ENTITY_ID]


async def test_an_options_save_does_not_reload_an_entry_that_failed_to_migrate(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Home Assistant refuses to reload an entry whose migration failed.

    Reloading it from the save would fail in the background, after the flow
    had already told the user the locks were updated.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]},
        version=99,
        unique_id="from_the_future",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.MIGRATION_ERROR

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    reload.assert_not_called()


async def test_migration_v5_moves_an_existing_slot_device_to_its_user(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A device the entry already owns is moved into its user's subentry.

    Every upgrading instance is in exactly this position: the devices were
    created before subentries existed, so they belong to the entry and to no
    subentry. Adding the user's entities would drag the device across
    implicitly, which Home Assistant deprecated -- a device belongs to one
    subentry and is meant to be moved deliberately.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"Housekeeper": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"housekeeper": 1},
        },
        unique_id="Existing Device Migration",
        version=4,
    )
    config_entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    # Registered through the same DeviceInfo the entities themselves carry, so
    # the identifier is the one production writes. Spelling it out here let an
    # earlier version of this test agree with a migration that looked the
    # device up by a malformed identifier: both were wrong in the same way, so
    # the test passed and the migration did nothing on a real instance.
    before = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        **build_slot_device_info(config_entry, 1),
    )
    assert before.config_subentry_id is None

    # The migration alone, not a full setup. Setting the entry up would add
    # this user's entities, and that drags the device across by itself -- so
    # the end state is the same whether the migration moved it or not, and a
    # test that only looked at the end state could not tell the difference.
    assert await async_migrate_entry(hass, config_entry)

    subentry_id = next(iter(config_entry.subentries))
    after = dev_reg.async_get(before.id)
    assert after.config_subentry_id == subentry_id


async def test_migration_v5_drops_a_user_with_no_number(
    hass: HomeAssistant, caplog
) -> None:
    """
    A user version 4 never numbered has no subentry to move into.

    Inventing a number would put their code on a position somebody else may
    hold, so they are dropped -- but by name, in the log, rather than
    vanishing silently.
    """
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {
                "Placed": {CONF_ENABLED: True, CONF_PIN: "1234"},
                "Unplaced": {CONF_ENABLED: True, CONF_PIN: "5678"},
            },
            CONF_SLOT_ASSIGNMENT: {"placed": 1},
        },
        unique_id="Unplaced Migration",
        version=4,
    )
    config_entry.add_to_hass(hass)
    caplog.set_level(logging.WARNING)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert set(entry_users(config_entry)) == {"Placed"}
    assert "Unplaced" in caplog.text


async def test_migration_v3_to_v4_reshapes_to_users(hass: HomeAssistant) -> None:
    """v4 names every user, then keys the configuration by that name.

    Both halves of one version bump. A name the user already chose is left
    exactly as-is, because rewriting one also renames that user on every lock
    that stores a user name.

    The slot number survives as internal bookkeeping and NOBODY IS
    RENUMBERED: it is the credential index on most providers, so a changed
    number moves that person's code to a different index on every lock and
    orphans their entities.
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

    assert config_entry.version == 5
    config = get_entry_config(config_entry)

    # Names: repaired where they had to be, untouched where they did not.
    by_slot = {num: slot[CONF_NAME] for num, slot in config.slots.items()}
    assert by_slot[1] == "User 1"  # was unnamed
    assert by_slot[2] == "Raman"  # user's choice, untouched
    assert by_slot[3] == "Raman 2"  # collided with slot 2
    assert by_slot[4] == "Ra|man"  # "|" is ordinary text now

    # Shape: one subentry per user, each carrying their own number.
    stored = {**config_entry.data, **config_entry.options}
    assert CONF_SLOTS not in stored
    assert CONF_USERS not in stored
    users = entry_users(config_entry)
    assert set(users) == {"User 1", "Raman", "Raman 2", "Ra|man"}
    # Nobody was renumbered, and the name is the subentry's title, not a field.
    assert {
        identity(subentry.title): subentry.data[CONF_SLOT]
        for subentry in config_entry.subentries.values()
    } == {"user 1": 1, "raman": 2, "raman 2": 3, "ra|man": 4}
    assert CONF_NAME not in users["Raman"]
    # Every other field survives.
    assert users["User 1"][CONF_PIN] == "1234"


async def test_the_user_shape_survives_a_full_setup_cycle(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """The migrated shape is still on disk after setup has written the entry back.

    Setup does not leave the entry alone: the update listener rebuilds the
    configuration and writes it back. A write path that put the users back on
    the entry -- or dropped their numbers -- would silently undo the migration
    on every single load.

    Nothing else catches that. Behaviour is IDENTICAL either way, because the
    integration still works in slot numbers internally, so every behavioural
    test passes while the migration quietly undoes itself.
    """
    entry = lock_code_manager_config_entry
    stored = {**entry.data, **entry.options}

    assert CONF_SLOTS not in stored
    assert CONF_USERS not in stored
    assert CONF_SLOT_ASSIGNMENT not in stored
    assert set(entry_users(entry)) == {"test1", "test2"}
    assert {
        identity(subentry.title): subentry.data[CONF_SLOT]
        for subentry in entry.subentries.values()
    } == {"test1": 1, "test2": 2}
    # And the reconstructed view still sees them, so nothing downstream broke.
    assert set(get_entry_config(entry).slots) == {1, 2}


async def test_setup_reclaims_entities_left_on_a_split_device(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Entities parked on a copy of the lock's device are brought home.

    HA 2026.8 restricted a device to one config entry and split every device
    that had two, leaving a Lock Code Manager-owned copy of the lock holding
    the per-lock entities. Setup moves them onto their slot device and drops
    the emptied copy, which nothing else will: ``dr.async_cleanup`` keeps any
    device that references a live config entry, occupied or not.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Split Device"
    )
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id

    # The copy the split left behind: our config entry, the lock's identifiers.
    split = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={("test", "lock_1_serial")},
        name="Test 1",
    )
    stranded = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry_id}|1|{ATTR_IN_SYNC}|{LOCK_1_ENTITY_ID}",
        config_entry=config_entry,
        device_id=split.id,
    )

    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    slot_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, build_slot_device_identifier(entry_id, 1)),
        entry_id,
    )
    assert slot_device
    reclaimed = ent_reg.async_get_entity_id(stranded.domain, DOMAIN, stranded.unique_id)
    assert ent_reg.async_get(reclaimed).device_id == slot_device.id
    assert dev_reg.async_get(split.id) is None

    await hass.config_entries.async_unload(entry_id)


async def test_setup_leaves_devices_of_other_integrations_alone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The sweep only touches devices this entry owns.

    The lock's real device is owned by the lock's integration and holds its
    entities; removing it, or moving anything off it, would be this
    integration reaching into another's registry.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    lock_device = dev_reg.async_get_device_by_identifier(
        (LOCK_DEVICE_DOMAIN, LOCK_1_ENTITY_ID), mock_lock_config_entry.entry_id
    )
    assert lock_device
    before = {
        entry.entity_id
        for entry in er.async_entries_for_device(ent_reg, lock_device.id)
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Leaves Others Alone"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert dev_reg.async_get(lock_device.id) is not None
    assert {
        entry.entity_id
        for entry in er.async_entries_for_device(ent_reg, lock_device.id)
    } == before

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_reclaim_skips_entities_it_has_no_slot_device_for(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The sweep moves slot entities and nothing else.

    An entity with no device, one already on a Lock Code Manager device, and
    one whose unique ID names no slot are all left exactly where they are --
    the last of those names no slot device to be moved to.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Skips Non Slot Entities"
    )
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id

    foreign = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={("test", "some_other_serial")},
        name="Test 1",
    )
    ours = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, build_slot_device_identifier(entry_id, 1))},
        name="Already home",
    )
    no_device = ent_reg.async_get_or_create(
        "binary_sensor", DOMAIN, f"{entry_id}|no_device", config_entry=config_entry
    )
    settled = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry_id}|1|{ATTR_IN_SYNC}|{LOCK_2_ENTITY_ID}",
        config_entry=config_entry,
        device_id=ours.id,
    )
    unparseable = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry_id}|not_a_slot|{ATTR_IN_SYNC}",
        config_entry=config_entry,
        device_id=foreign.id,
    )

    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    assert ent_reg.async_get(no_device.entity_id).device_id is None
    # Already on one of our devices, so the sweep has nothing to do for it.
    assert (
        ent_reg.async_get(
            ent_reg.async_get_entity_id(settled.domain, DOMAIN, settled.unique_id)
        ).device_id
        == ours.id
    )
    # Still on the foreign device, which therefore survives the removal pass.
    assert ent_reg.async_get(unparseable.entity_id).device_id == foreign.id
    assert dev_reg.async_get(foreign.id) is not None

    await hass.config_entries.async_unload(entry_id)


async def test_reclaim_leaves_our_own_empty_devices_alone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Devices of ours that hold nothing are not copies to be cleaned up.

    The pass removes devices left behind by the 2026.8 device split, which
    it recognises by their holding no entities. A device of ours can be
    empty too -- transiently, while its entities are being moved onto it --
    and only the check that it is one of ours tells the two apart.
    """
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Keeps Own Devices"
    )
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id

    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    ours = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}|404")},
        name="A slot device with nothing on it yet",
    )
    assert not er.async_entries_for_device(
        er.async_get(hass), ours.id, include_disabled_entities=True
    )

    # Run the pass against a registry that has it. Not via a reload: Home
    # Assistant does its own housekeeping there and takes empty devices
    # away itself, which would prove nothing about this pass.
    _async_reclaim_entities_from_foreign_devices(hass, config_entry)

    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, f"{entry_id}|404"), entry_id)
        is not None
    )

    await hass.config_entries.async_unload(entry_id)


async def test_reclaim_moves_a_disabled_entity_rather_than_deleting_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A disabled entity is never re-added by a platform, so it must be moved.

    Removing the copy first and letting the platforms sort it out would take
    this entity with it, along with whatever the user had customised on it.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Disabled Entity"
    )
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id

    split = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={("test", "lock_1_serial")},
        name="Test 1",
    )
    disabled = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry_id}|1|{ATTR_IN_SYNC}|{LOCK_1_ENTITY_ID}",
        config_entry=config_entry,
        device_id=split.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    survivor = ent_reg.async_get(
        ent_reg.async_get_entity_id(disabled.domain, DOMAIN, disabled.unique_id)
    )
    assert survivor is not None
    assert survivor.disabled_by is er.RegistryEntryDisabler.USER
    slot_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, build_slot_device_identifier(entry_id, 1)),
        entry_id,
    )
    assert slot_device and survivor.device_id == slot_device.id
    assert dev_reg.async_get(split.id) is None

    await hass.config_entries.async_unload(entry_id)


async def test_reclaim_does_not_resurrect_a_device_for_an_unconfigured_slot(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Moving an entity must not recreate a slot device the sweep then drops.

    The entity's slot is no longer configured, so its device is orphaned. If
    the move ran after the orphan sweep, that device would be recreated and
    left behind as a phantom until the next reload.
    """
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Unconfigured Slot"
    )
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id

    split = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={("test", "lock_1_serial")},
        name="Test 1",
    )
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry_id}|9|{ATTR_IN_SYNC}|{LOCK_1_ENTITY_ID}",
        config_entry=config_entry,
        device_id=split.id,
    )

    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    # Slot 9 is not configured, so neither device may survive.
    assert (
        dev_reg.async_get_device_by_identifier(
            (DOMAIN, build_slot_device_identifier(entry_id, 9)), entry_id
        )
        is None
    )
    assert dev_reg.async_get(split.id) is None

    await hass.config_entries.async_unload(entry_id)


async def test_reclaim_moves_an_entity_off_the_lock_integrations_device(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The repair this exists for: an entity sitting on the lock's own device.

    That is the state entities were left in before the device model changed --
    attached to a device owned by the lock's integration, which Lock Code
    Manager cannot display. It is the common case, and the one the mock lock
    could not represent while its device carried this integration's domain.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    lock_device = dev_reg.async_get_device_by_identifier(
        (LOCK_DEVICE_DOMAIN, LOCK_1_ENTITY_ID), mock_lock_config_entry.entry_id
    )
    assert lock_device
    assert lock_device.config_entry_id == mock_lock_config_entry.entry_id

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="On The Lock Device"
    )
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id

    # Disabled so no platform re-homes it, isolating the sweep's own work.
    stranded = ent_reg.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{entry_id}|1|{ATTR_IN_SYNC}|{LOCK_1_ENTITY_ID}",
        config_entry=config_entry,
        device_id=lock_device.id,
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    await hass.config_entries.async_setup(entry_id)
    await hass.async_block_till_done()

    slot_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, build_slot_device_identifier(entry_id, 1)),
        entry_id,
    )
    assert slot_device
    reclaimed = ent_reg.async_get_entity_id(stranded.domain, DOMAIN, stranded.unique_id)
    assert ent_reg.async_get(reclaimed).device_id == slot_device.id
    # The lock's own device is another integration's and must survive.
    assert dev_reg.async_get(lock_device.id) is not None

    await hass.config_entries.async_unload(entry_id)


async def test_migration_of_a_real_world_entry_shape(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A shape taken from a production entry, not invented for the test.

    Real configurations carry things a hand-written case tends not to:
    slots whose name is the empty string, slots with no name key at all,
    and disabled slots still holding a PIN.

    A slot holding a PIN survives however it was named, keeping its number,
    because the number is what its entities are keyed on and the credential
    is on the lock. A slot with neither a name nor a PIN was never a user --
    just an unused position from picking how many slots to create -- and is
    dropped rather than turned into somebody.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
            "slots": {
                1: {"enabled": True, "name": "Raman and Sherene", "pin": "1111"},
                2: {"enabled": True, "name": "Housekeeper", "pin": "2222"},
                3: {"enabled": False, "name": ""},
                4: {"enabled": False},
                5: {"enabled": False},
                6: {"enabled": False, "name": ""},
            },
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    # Devices as a released version left them, so the teardown of a dropped
    # slot has one to remove.
    for slot in range(1, 7):
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, build_slot_device_identifier(entry.entry_id, slot))},
            name=f"All Locks Code slot {slot}",
        )

    # The registry as a released version leaves it: entity IDs slugged from
    # the entry title and the slot number, keyed on the slot number.
    seeded = {
        slot: ent_reg.async_get_or_create(
            "text",
            DOMAIN,
            f"{entry.entry_id}|{slot}|{CONF_PIN}",
            config_entry=entry,
            suggested_object_id=f"all_locks_code_slot_{slot}_pin",
        )
        for slot in range(1, 7)
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    config = get_entry_config(entry)
    assert entry.version == 5
    # Nobody lost, and nobody renumbered: the number is the entities' key.
    # Only the two people. Everything else held nothing and named nobody.
    assert sorted(config.assignment.slots.values()) == [1, 2]
    assert config.name_for(1) == "Raman and Sherene"
    assert config.name_for(2) == "Housekeeper"
    assert not any(config.has_slot(slot) for slot in (3, 4, 5, 6))

    # The entity IDs move onto the users, but the registry ENTRIES are the
    # same rows: everything hanging off them -- settings, area, and the
    # recorder history the registry repoints -- follows the entity.
    for slot, entry_before in seeded.items():
        moved = ent_reg.async_get_entity_id(
            "text", DOMAIN, f"{entry.entry_id}|{slot}|{CONF_PIN}"
        )
        if not config.has_slot(slot):
            # A dropped slot's entities and device go with it, rather than
            # lingering under a name for a user who does not exist.
            assert moved is None
            assert (
                dev_reg.async_get_device_by_identifier(
                    (DOMAIN, build_slot_device_identifier(entry.entry_id, slot)),
                    entry.entry_id,
                )
                is None
            )
            continue
        assert moved == f"text.{slugify(f'{entry.title} {config.name_for(slot)}')}_pin"
        assert ent_reg.async_get(moved).id == entry_before.id

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_slot_device_is_named_for_the_user_who_holds_it(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The slot number is bookkeeping, so it must not be what the user reads."""
    entry_id = lock_code_manager_config_entry.entry_id
    dev_reg = dr.async_get(hass)
    config = get_entry_config(lock_code_manager_config_entry)

    for slot_num in config.slot_numbers:
        device = dev_reg.async_get_device_by_identifier(
            (DOMAIN, f"{entry_id}|{slot_num}"), entry_id
        )
        assert device is not None
        # Prefixed with the entry title, which is what makes the entity IDs
        # derived from it unique across entries.
        assert (
            device.name
            == f"{lock_code_manager_config_entry.title} {config.name_for(slot_num)}"
        )


async def test_renaming_a_user_keeps_the_subentry_they_live_in(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A rename retitles the subentry somebody already has.

    Matching users to subentries by name would make this a departure and an
    arrival, and removing a subentry takes the entities bound to it -- so the
    renamed user would lose their settings and their history, and get a fresh
    set of entities with `_2` on the end.
    """
    entry = lock_code_manager_config_entry
    ent_reg = er.async_get(hass)
    before = next(
        subentry
        for subentry in entry.subentries.values()
        if subentry.data[CONF_SLOT] == 1
    )
    entity_ids = {
        registry_entry.entity_id
        for registry_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if registry_entry.config_subentry_id == before.subentry_id
    }
    assert entity_ids

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "Sherene"},
        target={ATTR_ENTITY_ID: SLOT_1_NAME_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    after = entry.subentries[before.subentry_id]
    assert after.title == "Sherene"
    assert after.data[CONF_SLOT] == 1
    assert get_entry_config(entry).name_for(1) == "Sherene"
    # Every entity that was theirs is still theirs, under the same id.
    assert entity_ids <= set(ent_reg.entities)
    # Their device follows the rename too. The name entity writes data with
    # empty options, so it returns before the update listener does any entity
    # work: a rename handled alongside entity creation would miss it.
    assert (
        dr.async_get(hass)
        .async_get_device_by_identifier((DOMAIN, f"{entry.entry_id}|1"), entry.entry_id)
        .name
        == "Mock Title Sherene"
    )


async def test_migration_reslugs_entity_ids_onto_the_user_name(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An upgraded install must end up shaped like a fresh one.

    The registry entry itself is kept, not rebuilt: the unique ID still
    carries the slot number, so settings, area and recorder history follow
    the entity rather than being stranded under the old ID.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    # The registry as the released version leaves it.
    before = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|{CONF_PIN}",
        config_entry=entry,
        suggested_object_id="all_locks_code_slot_1_pin",
    )
    ent_reg.async_update_entity(before.entity_id, area_id="kitchen")
    assert before.entity_id == "text.all_locks_code_slot_1_pin"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    after = ent_reg.async_get_entity_id(
        "text", DOMAIN, f"{entry.entry_id}|1|{CONF_PIN}"
    )
    assert after == "text.all_locks_raman_pin"
    # Same registry entry, so everything hanging off it came along.
    assert ent_reg.async_get(after).id == before.id
    assert ent_reg.async_get(after).area_id == "kitchen"

    # Nothing rewrites an entity id stored inside an automation, so the user
    # is handed the mapping rather than left to discover it. A notification
    # rather than a repair: there is nothing here to fix, only to read.
    await hass.async_block_till_done()
    notice = hass.data["persistent_notification"][f"{DOMAIN}_entity_ids_renamed"]
    assert before.entity_id in notice["message"]
    assert after in notice["message"]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_names_an_entity_the_old_version_left_nameless(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The credential-used event arrives at migration with no name to reuse.

    The released version set ``_attr_name = None`` on it, so Home Assistant
    stored no ``original_name`` and its entity id was the device slug and
    nothing else -- no clue what the entity was. There is no name to append,
    so the key inside the unique ID stands in for one.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    before = ent_reg.async_get_or_create(
        "event",
        DOMAIN,
        f"{entry.entry_id}|1|{EVENT_CREDENTIAL_USED}",
        config_entry=entry,
        suggested_object_id="all_locks_code_slot_1",
    )
    assert before.entity_id == "event.all_locks_code_slot_1"
    assert before.original_name is None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    after = ent_reg.async_get_entity_id(
        "event", DOMAIN, f"{entry.entry_id}|1|{EVENT_CREDENTIAL_USED}"
    )
    assert after == "event.all_locks_raman_credential_used"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_takes_away_the_config_entrys_own_device(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The hub goes, and the users stop pointing at it.

    It never held an entity: it existed only so the per-user devices could
    name it in ``via_device``. Home Assistant reports a ``via_device``
    naming a device that is not there as a use it intends to break, so the
    two had to go together.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)

    # The registry as the released version leaves it.
    hub = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="Lock Code Manager",
        name="All Locks",
    )
    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        is not None
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        dev_reg.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        is None
    )

    user_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}|1"), entry.entry_id
    )
    assert user_device is not None
    assert user_device.via_device_id is None
    assert user_device.id != hub.id

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_leaves_an_unrecognized_entity_id_alone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Only the prefix this integration generated can be swapped.

    The rest of an entity ID comes from the entity's own translated name, so
    an ID that does not start with the expected prefix cannot be taken apart
    into a prefix and a suffix, and is left as it is.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    before = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|{CONF_PIN}",
        config_entry=entry,
        suggested_object_id="something_else_entirely",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        ent_reg.async_get_entity_id("text", DOMAIN, f"{entry.entry_id}|1|{CONF_PIN}")
        == before.entity_id
    )

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_renames_even_after_the_entry_was_retitled(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The entry's title is not a way back to an existing entity ID.

    An entity is slugged from the title in force when it was created, so
    rebuilding the OLD ID from today's title matches nothing once the entry
    has been renamed -- and silently renames nothing at all. The registry's
    own ``original_name`` is what a released version left behind, and it does
    not depend on the title.

    The NEW ID does use today's title, because that is what the device is
    named and what a fresh install would generate.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Renamed Since",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|{CONF_PIN}",
        config_entry=entry,
        original_name="PIN",
        suggested_object_id="the_original_title_code_slot_1_pin",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        ent_reg.async_get_entity_id("text", DOMAIN, f"{entry.entry_id}|1|{CONF_PIN}")
        == "text.renamed_since_raman_pin"
    )

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_leaves_an_entity_that_already_has_the_right_id(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Nothing to move means no registry write and nothing to report."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    before = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|{CONF_PIN}",
        config_entry=entry,
        original_name="PIN",
        suggested_object_id="all_locks_raman_pin",
    )
    assert before.entity_id == "text.all_locks_raman_pin"

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert (
        ent_reg.async_get_entity_id("text", DOMAIN, f"{entry.entry_id}|1|{CONF_PIN}")
        == "text.all_locks_raman_pin"
    )
    # Nothing moved, so the user is not told anything moved.
    assert f"{DOMAIN}_entity_ids_renamed" not in hass.data.get(
        "persistent_notification", {}
    )

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_rekeys_the_event_entity(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The event is named for what it reports, and it no longer reports a PIN.

    The key is the last part of the unique ID, so leaving the stored one
    alone would orphan the existing entity and build a fresh one beside it.
    Updating it keeps the same registry row, and with it the entity ID and
    everything else hanging off that row.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    before = ent_reg.async_get_or_create(
        EVENT_DOMAIN,
        DOMAIN,
        f"{entry.entry_id}|1|pin_used",
        config_entry=entry,
        suggested_object_id="all_locks_code_slot_1",
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    moved = ent_reg.async_get_entity_id(
        EVENT_DOMAIN, DOMAIN, f"{entry.entry_id}|1|{EVENT_CREDENTIAL_USED}"
    )
    assert moved is not None
    # Same row, so nothing hanging off it was lost.
    assert ent_reg.async_get(moved).id == before.id
    # And the old key is gone rather than left beside it.
    assert (
        ent_reg.async_get_entity_id(
            EVENT_DOMAIN, DOMAIN, f"{entry.entry_id}|1|pin_used"
        )
        is None
    )

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migration_survives_an_event_entity_that_already_moved(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A duplicate is worth a log line; refusing to start is not.

    Re-keying onto a unique ID the registry already holds raises, and a raise
    inside the migration leaves the whole entry unloadable -- every lock and
    every PIN, over one stray registry row.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        EVENT_DOMAIN, DOMAIN, f"{entry.entry_id}|1|pin_used", config_entry=entry
    )
    ent_reg.async_get_or_create(
        EVENT_DOMAIN,
        DOMAIN,
        f"{entry.entry_id}|1|{EVENT_CREDENTIAL_USED}",
        config_entry=entry,
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(entry.entry_id)


def test_migration_keeps_the_condition_already_set() -> None:
    """
    A user carrying both fields keeps the current one.

    Only reachable from a configuration written part-way through this
    release's changes, but taking the legacy value would quietly undo
    whatever set the new one.
    """
    migrated, _, _dropped = migrate_to_users(
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {
                "Raman": {
                    "entity_id": "binary_sensor.stale",
                    CONF_CONDITION: "binary_sensor.current",
                }
            },
        }
    )

    assert migrated[CONF_USERS]["Raman"] == {CONF_CONDITION: "binary_sensor.current"}


async def test_removing_a_slot_clears_its_code_off_the_lock(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A slot leaving the configuration takes its credential with it.

    Two things go wrong when it does not. The obvious one: a PIN nobody
    manages any more still opens the door. The quieter one: allocation
    refuses to issue a number it can still read a code at, so the slot is
    burned -- every add-then-remove cycle costs a slot the lock never gets
    back.
    """
    entry = lock_code_manager_config_entry
    locks = list(entry.runtime_data.locks.values())
    # Slot 1: slot 2's calendar keeps it inactive, so it holds no code anyway.
    for lock in locks:
        assert (await lock.async_get_usercodes())[1].pin

    # Written the way the editor writes it: the user's subentry goes, and
    # their slot number goes with them.
    departing = get_entry_config(entry).name_for(1)
    subentry = next(
        subentry
        for subentry in entry.subentries.values()
        if subentry.title == departing
    )
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()

    assert not get_entry_config(entry).has_slot(1)
    # Every lock in the entry, not just the first: the slot was written to
    # all of them.
    for lock in locks:
        # A cleared slot may drop out of the lock's view entirely or come
        # back empty; both mean nothing is programmed there.
        credential = (await lock.async_get_usercodes()).get(1)
        assert not (credential and credential.pin)


async def test_a_per_lock_entity_id_names_the_lock_it_belongs_to(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    One user, several locks: each per-lock entity has to say which lock.

    They all sit on the same slot device, so naming them after the user alone
    makes identical slugs and Home Assistant separates them with _2 and _3 --
    suffixes assigned in whatever order the rename happened to run, which
    identify nothing and are worse than the lock-shaped IDs they replaced.
    Rebuilding the name from the lock is what keeps them tellable apart.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
            CONF_SLOTS: {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        ent_reg.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}|1|{ATTR_CODE}|{lock_entity_id}",
            config_entry=entry,
            suggested_object_id=f"{lock_entity_id.split('.')[1]}_code_slot_1",
            # As a released version stored it: no lock in the entity's own
            # name, which is why it cannot be read back and reused.
            original_name="Code slot 1",
        )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    moved = [
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}|1|{ATTR_CODE}|{lock_entity_id}"
        )
        for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    ]
    # The user, then the lock, then the credential type -- no slot number and
    # no collision suffix.
    assert moved == [
        "sensor.all_locks_raman_test_1_pin",
        "sensor.all_locks_raman_test_2_pin",
    ]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_per_lock_entity_survives_its_lock_leaving_the_registry(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A lock with no registry row still has to name its entities.

    The lock's entity can be gone by the time this runs -- excluded from
    Z-Wave, integration removed -- while Lock Code Manager's own entities for
    it remain. Skipping them would leave slot-shaped IDs behind forever, so
    the name falls back to the lock's object id, which is what its entity id
    was slugged from to begin with.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            CONF_LOCKS: ["lock.departed"],
            CONF_SLOTS: {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}|1|{ATTR_CODE}|lock.departed",
        config_entry=entry,
        suggested_object_id="departed_code_slot_1",
        original_name="Code slot 1",
    )
    assert ent_reg.async_get("lock.departed") is None

    # Setup itself cannot succeed without the lock, but the migration runs
    # ahead of it, so the rename still has to do something sensible.
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 5

    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}|1|{ATTR_CODE}|lock.departed"
        )
        == "sensor.all_locks_raman_departed_pin"
    )


@pytest.mark.parametrize("enabled", [True, False])
async def test_a_slot_with_a_pin_but_no_name_survives_being_migrated(
    hass: HomeAssistant, mock_lock_config_entry, enabled: bool
) -> None:
    """
    Constructed, not captured: a PIN with no name, which only 4.x could make.

    Its schema left both the name and the PIN optional and only required a
    PIN when the slot was enabled, so a nameless slot could hold one -- and
    could be switched on, putting a live code on the lock belonging to
    nobody. The user-centric shape cannot express this at all, because the
    name is the key a user is stored under, so it arrives only by migration.

    Either way the slot survives and is named after its number, but for
    different reasons: enabled, there is a credential on the lock and
    somebody has to own it; disabled, the PIN is only stored configuration,
    and dropping it would delete something the owner typed.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {"enabled": True, "name": "Raman", "pin": "1111"},
                2: {"enabled": enabled, "name": "", "pin": "2222"},
            },
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    config = get_entry_config(entry)
    assert sorted(config.assignment.slots.values()) == [1, 2]
    assert config.name_for(2) == "User 2"
    assert config.slot(2)[CONF_PIN] == "2222"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_lock_no_provider_claims_raises_its_own_repair(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    A lock dropped during setup says so in the repairs panel, not just the log.

    Selection-time validation refuses an unclaimed mqtt lock today, so an
    entry holding one was configured before that check existed. Nothing ever
    re-validates it: its entities simply never appear and its codes are never
    written, and the only trace was a line in the log nobody reads.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id],
        CONF_SLOTS: BASE_CONFIG[CONF_SLOTS],
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="pre_upgrade")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"lock_dropped_{unclaimed.entity_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["lock_entity_id"] == unclaimed.entity_id
    assert unclaimed.entity_id in issue.translation_placeholders["error"]
    # The rest of the entry is untouched: one bad lock is not a failed setup.
    assert list(entry.runtime_data.locks) == [LOCK_1_ENTITY_ID]

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_provider_bug_during_setup_raises_no_repair(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Only an unclaimed lock gets the repair that says the bridge is unsupported.

    Every other exception escaping setup is a bug in a provider LCM does
    speak, and that repair is persistent, survives restarts, and tells the
    user to remove a lock that works. The log line it already had is the
    right amount of noise for a bug.
    """
    config = {CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_SLOTS: BASE_CONFIG[CONF_SLOTS]}
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="provider_bug")
    entry.add_to_hass(hass)

    with _failing_lock_build(LOCK_1_ENTITY_ID, ValueError("provider bug")):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"lock_dropped_{LOCK_1_ENTITY_ID}")
        is None
    )
    assert list(entry.runtime_data.locks) == []

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_cancellation_during_setup_is_never_swallowed(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Shutting Home Assistant down must not look like a lock that could not load.

    ``asyncio.gather(return_exceptions=True)`` hands a cancelled child back as
    a result rather than propagating it, so cancellation reached the same
    branch as a real failure: the lock was dropped and a persistent repair
    blamed an unsupported bridge -- outliving the restart that ended the
    condition, on a lock that was never unsupported at all.
    """
    config = {CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_SLOTS: BASE_CONFIG[CONF_SLOTS]}
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="cancelled")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Driven through the setup pass directly: Home Assistant's own config
    # entry machinery absorbs whatever a setup raises, so going through it
    # would assert on that absorption rather than on this loop.
    with (
        _failing_lock_build(LOCK_2_ENTITY_ID, asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        await _async_setup_new_locks(
            hass,
            entry,
            [LOCK_2_ENTITY_ID],
            get_entry_config(entry),
            MagicMock(),
            er.async_get(hass),
        )

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"lock_dropped_{LOCK_2_ENTITY_ID}")
        is None
    )

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_removing_a_dropped_lock_clears_its_repair(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    Taking the lock out of the entry is what the repair asks for, so it works.

    Nothing else can clear that repair: the lock never got an instance, so
    there is no teardown to do it. The rest of the update has to survive the
    removal too, which is why a slot is added in the same breath -- the
    removal used to reach into runtime data for the instance that is not
    there, and everything after it in the update never ran.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id],
        CONF_SLOTS: BASE_CONFIG[CONF_SLOTS],
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="pre_upgrade")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    write_entry_config(
        hass,
        entry,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                **BASE_CONFIG[CONF_SLOTS],
                3: {CONF_NAME: "test3", CONF_PIN: "9012", CONF_ENABLED: True},
            },
        },
    )
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"lock_dropped_{unclaimed.entity_id}"
        )
        is None
    )
    assert 3 in entry.runtime_data.slot_coordinators

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_releasing_a_lock_from_an_entry_that_never_loaded_purges_every_row(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """With nothing loaded, no entity can remove itself, so the release removes all of them."""
    entry = lock_code_manager_config_entry
    ent_reg = er.async_get(hass)
    lock_2_rows = [
        entity_id(hass, entry, slot_num, LOCK_2_ENTITY_ID)
        for slot_num in BASE_CONFIG[CONF_SLOTS]
        for entity_id in (in_sync_entity_id, code_entity_id)
    ]
    assert all(ent_reg.async_get(row) for row in lock_2_rows)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not hasattr(entry, "runtime_data")

    await async_release_locks(hass, entry, [LOCK_2_ENTITY_ID])

    assert all(ent_reg.async_get(entity_id) is None for entity_id in lock_2_rows)


async def test_removing_a_slot_stops_its_coordinator_and_lets_the_locks_go(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A removed slot's coordinator is stopped, and no lock's worker keeps the slot."""
    entry = lock_code_manager_config_entry
    coordinator = entry.runtime_data.slot_coordinators[2]
    assert coordinator._started
    workers = [
        lock_registry(hass).worker(lock_entity_id)
        for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    ]
    assert all(worker is not None and 2 in worker.slots for worker in workers)

    new_config = copy.deepcopy(BASE_CONFIG)
    del new_config[CONF_SLOTS][2]
    write_entry_config(hass, entry, new_config)
    await hass.async_block_till_done()

    assert 2 not in entry.runtime_data.slot_coordinators
    assert 1 in entry.runtime_data.slot_coordinators
    assert not coordinator._started
    # The slot's release clear is done, and the workers have let it go.
    assert not any(2 in worker.slots for worker in workers)


async def test_a_blip_during_the_deferred_setup_retry_is_retried_again(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry: MockConfigEntry,
) -> None:
    """
    Regression for PR #1471: one blip on the deferred retry does not strand writes.

    A lock not connected at setup is set up later, from the connection
    check. That retry's flag was cleared before it ran, so a disconnect on
    the way through spent the only retry, and no code reached the lock
    until the integration was reloaded by hand.
    """
    entry = current_entry("Deferred Blip", {1: "4321"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(lambda lock: lock.set_connected(False)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = mock_lock(entry)
    blip = lock.fail("setup", error=LockDisconnected, times=1)
    lock.set_connected(True)

    # Two connection checks: the first retry meets the blip, the second runs.
    check = lock.connection_check_interval.total_seconds()
    await async_advance_clock(hass, freezer, 2 * check + 5)
    if blip.hits != 1:
        pytest.fail("setup: the deferred retry never met the blip")
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=20,
        what="slot 1 in sync",
    )

    assert lock.codes[1] == "4321"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_hard_refresh_naming_an_unmanaged_lock_warns_about_that_lock(
    hass: HomeAssistant,
    mock_lock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Regression for PR #688: the warning names the target that is unmanaged.

    Given a managed lock and an unmanaged one, the warning named the wrong
    entity, sending the reader after a lock that was fine.
    """
    entry = current_entry("Refresh Targets", {1: "1234"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    caplog.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]},
        blocking=True,
    )

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("custom_components.lock_code_manager")
    ]
    assert any(LOCK_2_ENTITY_ID in message for message in warnings)
    assert not any(LOCK_1_ENTITY_ID in message for message in warnings)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_slot_entities_are_named_in_title_case(
    hass: HomeAssistant,
    mock_lock_config_entry: MockConfigEntry,
    lock_code_manager_config_entry: MockConfigEntry,
) -> None:
    """Regression for PR #1173: slot entity names read "Active", not "active"."""
    for entity_id, name in (
        (SLOT_1_ACTIVE_ENTITY, "Active"),
        (SLOT_1_ENABLED_ENTITY, "Enabled"),
        (SLOT_1_NAME_ENTITY, "Name"),
    ):
        assert (
            hass.states.get(entity_id)
            .attributes[ATTR_FRIENDLY_NAME]
            .endswith(f" {name}")
        )


async def test_deleting_a_user_by_service_removes_its_slot_device(
    hass: HomeAssistant,
    mock_lock_config_entry: MockConfigEntry,
    lock_code_manager_config_entry: MockConfigEntry,
) -> None:
    """
    Regression for #1399 (PR #1402), through ``delete_user``.

    A removed user's slot device stayed in the device registry, and Home
    Assistant would not let it be deleted by hand.
    """
    entry = lock_code_manager_config_entry
    dev_reg = dr.async_get(hass)
    slot_2 = (DOMAIN, f"{entry.entry_id}|2")
    if dev_reg.async_get_device_by_identifier(slot_2, entry.entry_id) is None:
        pytest.fail("setup: slot 2 has no device")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE_USER,
        {
            "config_entry_id": entry.entry_id,
            "name": [get_entry_config(entry).name_for(2)],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert dev_reg.async_get_device_by_identifier(slot_2, entry.entry_id) is None
    assert dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}|1"), entry.entry_id
    )


async def test_the_code_sensor_shows_a_new_pin_when_in_sync_does(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry: MockConfigEntry,
) -> None:
    """
    Regression for #64 (PR #68): the code sensor does not trail in-sync.

    On a polled lock the code sensor waited for the next poll, up to a
    minute after in-sync had already said the new PIN was on the lock.
    """
    entry = current_entry("Code Sensor Lag", {1: "1234"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    in_sync = in_sync_entity_id(hass, entry, 1)
    code = code_entity_id(hass, entry, 1)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=10,
        what="slot 1 in sync",
    )

    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_tick_until(
        hass,
        freezer,
        lambda: (
            hass.states.get(in_sync).state == STATE_ON
            and mock_lock(entry).codes.get(1) == "4321"
        ),
        max_ticks=10,
        what="the new PIN in sync",
    )

    assert hass.states.get(code).state == "4321"
    assert await hass.config_entries.async_unload(entry.entry_id)
