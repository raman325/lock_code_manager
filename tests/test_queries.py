"""Test the helpers module."""

from collections.abc import Callable
import logging
from typing import Any

import probatio as vol
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_FLOOR_ID,
    ATTR_LABEL_ID,
    CONF_PIN,
    ENTITY_MATCH_ALL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
    label_registry as lr,
)

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_USERS,
    DOMAIN,
    SERVICE_HARD_REFRESH_USERCODES,
)
from custom_components.lock_code_manager.ha.locks import get_locks_from_targets
from custom_components.lock_code_manager.model.queries import (
    get_loaded_config_entry,
    slots_configured_on,
)
from custom_components.lock_code_manager.model.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID


async def test_get_locks_from_targets_with_entity_ids(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves entity IDs to locks."""
    locks = get_locks_from_targets(hass, {ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID]})

    assert len(locks) == 1
    lock = next(iter(locks))
    assert lock.lock.entity_id == LOCK_1_ENTITY_ID


async def test_get_locks_from_targets_multiple_entity_ids(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves multiple entity IDs."""
    locks = get_locks_from_targets(
        hass, {ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )

    assert len(locks) == 2
    entity_ids = {lock.lock.entity_id for lock in locks}
    assert entity_ids == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}


@pytest.mark.parametrize(
    ("entity_id", "expected_warning"),
    [
        ("switch.not_a_lock", "invalid lock entities"),
        ("lock.unmanaged_lock", "not managed by Lock Code Manager"),
    ],
)
async def test_get_locks_from_targets_warns_for_bad_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
    entity_id: str,
    expected_warning: str,
):
    """Test get_locks_from_targets warns for non-lock and unmanaged entities."""
    locks = get_locks_from_targets(hass, {ATTR_ENTITY_ID: [entity_id]})

    assert len(locks) == 0
    assert expected_warning in caplog.text
    assert entity_id in caplog.text


async def test_get_locks_from_targets_empty(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets with empty target data returns no locks."""
    locks = get_locks_from_targets(hass, {})
    assert len(locks) == 0


async def test_get_locks_from_targets_deduplicates(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets deduplicates when same lock matched by multiple sources."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(LOCK_1_ENTITY_ID)
    assert entry is not None
    assert entry.device_id is not None

    locks = get_locks_from_targets(
        hass,
        {
            ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID],
            ATTR_DEVICE_ID: [entry.device_id],
        },
    )

    assert len(locks) == 1


def _lock_device_id(hass: HomeAssistant, lock_entity_id: str) -> str:
    """Return the device a lock entity belongs to."""
    entry = er.async_get(hass).async_get(lock_entity_id)
    assert entry is not None
    assert entry.device_id is not None
    return entry.device_id


def _area_on_lock_device(
    hass: HomeAssistant, mock_lock_config_entry: MockConfigEntry
) -> dict[str, Any]:
    """
    Put the area on the lock's device only, as most integrations do.

    The device also carries a sensor, as real lock devices do, so the area
    reaches an entity that is not a lock at all.
    """
    area = ar.async_get(hass).async_get_or_create("front_door")
    device_id = _lock_device_id(hass, LOCK_1_ENTITY_ID)
    dr.async_get(hass).async_update_device(device_id, area_id=area.id)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "sensor",
        "test",
        "lock_1_battery",
        config_entry=mock_lock_config_entry,
        device_id=device_id,
    )
    assert ent_reg.async_get(LOCK_1_ENTITY_ID).area_id is None
    return {ATTR_AREA_ID: area.id}


def _area_on_lock_entity(hass: HomeAssistant, _: MockConfigEntry) -> dict[str, Any]:
    """Put the area on the lock entity itself."""
    area = ar.async_get(hass).async_get_or_create("front_door")
    er.async_get(hass).async_update_entity(LOCK_1_ENTITY_ID, area_id=area.id)
    return {ATTR_AREA_ID: area.id}


def _device(hass: HomeAssistant, _: MockConfigEntry) -> dict[str, Any]:
    """Target the lock's device."""
    return {ATTR_DEVICE_ID: _lock_device_id(hass, LOCK_1_ENTITY_ID)}


def _floor(hass: HomeAssistant, _: MockConfigEntry) -> dict[str, Any]:
    """Target the floor of the area the lock's device is in."""
    floor = fr.async_get(hass).async_create("Ground")
    area = ar.async_get(hass).async_create("front_door", floor_id=floor.floor_id)
    dr.async_get(hass).async_update_device(
        _lock_device_id(hass, LOCK_1_ENTITY_ID), area_id=area.id
    )
    return {ATTR_FLOOR_ID: floor.floor_id}


def _label(hass: HomeAssistant, _: MockConfigEntry) -> dict[str, Any]:
    """Target a label on the lock entity."""
    label = lr.async_get(hass).async_create("Doors")
    er.async_get(hass).async_update_entity(LOCK_1_ENTITY_ID, labels={label.label_id})
    return {ATTR_LABEL_ID: label.label_id}


def _area_with_unmanaged_lock(
    hass: HomeAssistant, mock_lock_config_entry: MockConfigEntry
) -> dict[str, Any]:
    """Put a lock Lock Code Manager does not manage in the targeted area."""
    area = ar.async_get(hass).async_get_or_create("front_door")
    dev_reg = dr.async_get(hass)
    dev_reg.async_update_device(
        _lock_device_id(hass, LOCK_1_ENTITY_ID), area_id=area.id
    )
    unmanaged_device = dev_reg.async_get_or_create(
        config_entry_id=mock_lock_config_entry.entry_id,
        identifiers={("test", "lock.unmanaged")},
    )
    dev_reg.async_update_device(unmanaged_device.id, area_id=area.id)
    er.async_get(hass).async_get_or_create(
        "lock",
        "test",
        "unmanaged",
        config_entry=mock_lock_config_entry,
        device_id=unmanaged_device.id,
    )
    return {ATTR_AREA_ID: area.id}


@pytest.mark.parametrize(
    ("build_target", "refreshed"),
    [
        pytest.param(_area_on_lock_device, {LOCK_1_ENTITY_ID}, id="area_on_device"),
        pytest.param(_area_on_lock_entity, {LOCK_1_ENTITY_ID}, id="area_on_entity"),
        pytest.param(_device, {LOCK_1_ENTITY_ID}, id="device"),
        pytest.param(_floor, {LOCK_1_ENTITY_ID}, id="floor"),
        pytest.param(_label, {LOCK_1_ENTITY_ID}, id="label"),
        pytest.param(
            lambda hass, _: {
                ATTR_DEVICE_ID: _lock_device_id(hass, LOCK_1_ENTITY_ID),
                ATTR_ENTITY_ID: LOCK_2_ENTITY_ID,
            },
            {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID},
            id="mixed_device_and_entity",
        ),
        pytest.param(
            _area_with_unmanaged_lock, {LOCK_1_ENTITY_ID}, id="unmanaged_lock_skipped"
        ),
    ],
)
async def test_hard_refresh_usercodes_resolves_targets(
    hass: HomeAssistant,
    mock_lock_config_entry: MockConfigEntry,
    lock_code_manager_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    build_target: Callable[[HomeAssistant, MockConfigEntry], dict[str, Any]],
    refreshed: set[str],
) -> None:
    """
    hard_refresh_usercodes reaches the managed locks any entity service would.

    Entities a target reaches without naming them are skipped quietly when they
    are not managed locks; only an entity named directly earns a warning.
    """
    target = build_target(hass, mock_lock_config_entry)
    locks = lock_code_manager_config_entry.runtime_data.locks
    reads_before = {
        entity_id: len(lock.service_calls["get_usercodes"])
        for entity_id, lock in locks.items()
    }

    await hass.services.async_call(
        DOMAIN, SERVICE_HARD_REFRESH_USERCODES, target, blocking=True
    )

    # Read afresh, through each lock's worker.
    assert {
        entity_id
        for entity_id, lock in locks.items()
        if len(lock.service_calls["get_usercodes"]) > reads_before[entity_id]
    } == refreshed
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith(f"custom_components.{DOMAIN}")
    ]


async def test_hard_refresh_usercodes_rejects_entity_id_all(
    hass: HomeAssistant,
    mock_lock_config_entry: MockConfigEntry,
    lock_code_manager_config_entry: MockConfigEntry,
) -> None:
    """hard_refresh_usercodes refuses "all", which names no entity to resolve."""
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_HARD_REFRESH_USERCODES,
            {ATTR_ENTITY_ID: ENTITY_MATCH_ALL},
            blocking=True,
        )


async def test_get_loaded_config_entry_raises_when_not_loaded(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """get_loaded_config_entry raises ServiceValidationError for an unloaded entry.

    Covers the loaded-vs-not-loaded branch distinct from the "no such
    entry"/"wrong domain" cases already covered via the services tests.
    """
    entry = lock_code_manager_config_entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is not ConfigEntryState.LOADED

    with pytest.raises(ServiceValidationError, match="not loaded"):
        get_loaded_config_entry(hass, entry.entry_id)


async def test_get_loaded_config_entry_requires_an_identifier(
    hass: HomeAssistant,
) -> None:
    """
    Naming no entry is refused, rather than reported as a missing one.

    The service schemas make this unreachable from an action, but the
    function is the shared entry point the websocket uses too, and "no entry
    with ID `None`" would send a caller looking for an entry they never named.
    """
    with pytest.raises(ServiceValidationError, match="Neither"):
        get_loaded_config_entry(hass)


async def test_managed_slots_include_an_entry_that_was_never_migrated(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    A pre-subentry entry still counts the numbers it holds on a lock.

    Migration runs only on setup, so an entry that is disabled -- or here,
    simply never loaded -- keeps its old shape while every other entry on the
    lock goes on asking it which numbers are taken. Reading it as empty is how
    a new entry gets issued numbers this one already holds (#1514 review).
    """
    stale = MockConfigEntry(
        domain=DOMAIN,
        version=4,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"Ada": {CONF_PIN: "1357", "enabled": True}},
            CONF_SLOT_ASSIGNMENT: {"ada": 7},
        },
        unique_id="never migrated",
    )
    stale.add_to_hass(hass)

    assert 7 in slots_configured_on(hass, LOCK_1_ENTITY_ID)
    # The loaded entry's own numbers are still there beside it.
    assert {1, 2} <= slots_configured_on(hass, LOCK_1_ENTITY_ID)
