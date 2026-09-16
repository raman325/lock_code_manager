"""Tests for how a slot coordinator starts its sync managers."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .common import LOCK_1_ENTITY_ID


async def test_a_lock_without_a_coordinator_starts_no_manager(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A lock whose provider setup has not finished has nothing to sync against yet."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    before = coordinator.sync_managers
    lock = MagicMock()
    lock.coordinator = None
    lock.lock.entity_id = "lock.not_ready"

    await coordinator.async_start_sync(lock, er.async_get(hass))

    assert coordinator.sync_managers == before


async def test_a_stopped_coordinator_starts_no_manager(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Once stopped, a coordinator has no unload left to stop what it would start."""
    entry = lock_code_manager_config_entry
    coordinator = entry.runtime_data.slot_coordinators[1]
    lock = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    await coordinator.async_stop_sync()
    coordinator.async_stop()

    await coordinator.async_start_sync(lock, er.async_get(hass))

    assert coordinator.sync_managers == []


async def test_an_unloading_entry_starts_no_manager(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """An update listener that lost its race with an unload starts nothing."""
    entry = lock_code_manager_config_entry
    coordinator = entry.runtime_data.slot_coordinators[1]
    lock = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    await coordinator.async_stop_sync()

    entry.mock_state(hass, ConfigEntryState.UNLOAD_IN_PROGRESS)
    try:
        await coordinator.async_start_sync(lock, er.async_get(hass))
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)

    assert coordinator.sync_managers == []
