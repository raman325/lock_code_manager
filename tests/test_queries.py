"""Test the helpers module."""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_PIN,
    CONF_USERS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.locks import get_locks_from_targets
from custom_components.lock_code_manager.domain.queries import (
    get_loaded_config_entry,
    get_managed_slots,
)
from custom_components.lock_code_manager.domain.slot_assignment import (
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


async def test_get_locks_from_targets_with_area_id(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves area IDs to locks."""
    area_reg = ar.async_get(hass)
    area = area_reg.async_get_or_create("test_area")
    ent_reg = er.async_get(hass)
    ent_reg.async_update_entity(LOCK_1_ENTITY_ID, area_id=area.id)

    locks = get_locks_from_targets(hass, {ATTR_AREA_ID: [area.id]})

    assert len(locks) == 1
    lock = next(iter(locks))
    assert lock.lock.entity_id == LOCK_1_ENTITY_ID


async def test_get_locks_from_targets_with_device_id(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test get_locks_from_targets resolves device IDs to locks."""
    # Get the device for lock.test_1
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(LOCK_1_ENTITY_ID)
    assert entry is not None
    assert entry.device_id is not None

    locks = get_locks_from_targets(hass, {ATTR_DEVICE_ID: [entry.device_id]})

    assert len(locks) == 1
    lock = next(iter(locks))
    assert lock.lock.entity_id == LOCK_1_ENTITY_ID


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

    assert 7 in get_managed_slots(hass, LOCK_1_ENTITY_ID)
    # The loaded entry's own numbers are still there beside it.
    assert {1, 2} <= get_managed_slots(hass, LOCK_1_ENTITY_ID)
