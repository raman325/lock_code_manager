"""Test the helpers module."""

import pytest

from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.lock_code_manager.helpers import get_locks_from_targets

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

# =============================================================================
# get_locks_from_targets Tests
# =============================================================================


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
    # Assign lock.test_1 to an area
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

    locks = get_locks_from_targets(
        hass,
        {
            ATTR_ENTITY_ID: [LOCK_1_ENTITY_ID],
            ATTR_DEVICE_ID: [entry.device_id],
        },
    )

    assert len(locks) == 1
