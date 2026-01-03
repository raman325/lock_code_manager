"""Test the helpers module."""

from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from custom_components.lock_code_manager.const import CONF_LOCKS, DOMAIN
from custom_components.lock_code_manager.helpers import get_locks_from_targets

# =============================================================================
# get_locks_from_targets Tests
# =============================================================================


async def test_get_locks_from_targets_with_entity_ids(hass: HomeAssistant):
    """Test get_locks_from_targets with entity IDs."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create a mock lock
    mock_lock = MagicMock()
    mock_lock.lock.entity_id = "lock.test_lock"

    # Set up hass.data structure
    hass.data[DOMAIN] = {CONF_LOCKS: {"lock.test_lock": mock_lock}}

    target_data = {ATTR_ENTITY_ID: ["lock.test_lock"]}
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 1
    assert mock_lock in locks


async def test_get_locks_from_targets_warns_for_non_lock_entity(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """Test get_locks_from_targets warns when entity is not a lock."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {CONF_LOCKS: {}}

    # Pass a switch entity instead of a lock
    target_data = {ATTR_ENTITY_ID: ["switch.not_a_lock"]}
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 0
    assert "invalid lock entities" in caplog.text
    assert "switch.not_a_lock" in caplog.text


async def test_get_locks_from_targets_warns_when_lock_not_in_lcm(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """Test get_locks_from_targets warns when lock is not managed by LCM."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # No locks registered in LCM
    hass.data[DOMAIN] = {CONF_LOCKS: {}}

    target_data = {ATTR_ENTITY_ID: ["lock.unmanaged_lock"]}
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 0
    assert "not managed by Lock Code Manager" in caplog.text
    assert "lock.unmanaged_lock" in caplog.text


async def test_get_locks_from_targets_with_area_id(hass: HomeAssistant):
    """Test get_locks_from_targets with area IDs."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create area
    area_reg = ar.async_get(hass).async_get_or_create("test_area")

    # Create entity in area
    ent_reg = er.async_get(hass)
    lock_entry = ent_reg.async_get_or_create(
        LOCK_DOMAIN,
        "test",
        "area_lock",
        config_entry=config_entry,
    )
    ent_reg.async_update_entity(lock_entry.entity_id, area_id=area_reg.id)

    # Create mock lock
    mock_lock = MagicMock()
    mock_lock.lock.entity_id = lock_entry.entity_id

    hass.data[DOMAIN] = {CONF_LOCKS: {lock_entry.entity_id: mock_lock}}

    target_data = {ATTR_AREA_ID: [area_reg.id]}
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 1
    assert mock_lock in locks


async def test_get_locks_from_targets_with_device_id(hass: HomeAssistant):
    """Test get_locks_from_targets with device IDs."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create device
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "device_1")},
    )

    # Create entity on device
    ent_reg = er.async_get(hass)
    lock_entry = ent_reg.async_get_or_create(
        LOCK_DOMAIN,
        "test",
        "device_lock",
        config_entry=config_entry,
        device_id=device.id,
    )

    # Create mock lock
    mock_lock = MagicMock()
    mock_lock.lock.entity_id = lock_entry.entity_id

    hass.data[DOMAIN] = {CONF_LOCKS: {lock_entry.entity_id: mock_lock}}

    target_data = {ATTR_DEVICE_ID: [device.id]}
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 1
    assert mock_lock in locks


async def test_get_locks_from_targets_empty(hass: HomeAssistant):
    """Test get_locks_from_targets with empty target data."""
    hass.data[DOMAIN] = {CONF_LOCKS: {}}

    target_data = {}
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 0


async def test_get_locks_from_targets_multiple_sources(hass: HomeAssistant):
    """Test get_locks_from_targets combining multiple target types."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create device
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "device_1")},
    )

    # Create entities
    ent_reg = er.async_get(hass)
    lock1 = ent_reg.async_get_or_create(
        LOCK_DOMAIN,
        "test",
        "lock_1",
        config_entry=config_entry,
    )
    lock2 = ent_reg.async_get_or_create(
        LOCK_DOMAIN,
        "test",
        "lock_2",
        config_entry=config_entry,
        device_id=device.id,
    )

    # Create mock locks
    mock_lock1 = MagicMock()
    mock_lock1.lock.entity_id = lock1.entity_id
    mock_lock2 = MagicMock()
    mock_lock2.lock.entity_id = lock2.entity_id

    hass.data[DOMAIN] = {
        CONF_LOCKS: {lock1.entity_id: mock_lock1, lock2.entity_id: mock_lock2}
    }

    target_data = {
        ATTR_ENTITY_ID: [lock1.entity_id],
        ATTR_DEVICE_ID: [device.id],
    }
    locks = get_locks_from_targets(hass, target_data)

    assert len(locks) == 2
    assert mock_lock1 in locks
    assert mock_lock2 in locks
