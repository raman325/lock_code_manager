"""Tests for how a slot folds and starts its sync managers."""

from __future__ import annotations

from unittest.mock import MagicMock

from hypothesis import given, strategies as st

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.domain.models import SyncState
from custom_components.lock_code_manager.domain.sync import (
    fold_in_sync,
    fold_sync_status,
)

from .common import LOCK_1_ENTITY_ID

STATUSES = [state.value for state in SyncState if state is not SyncState.LOADING]


@given(st.lists(st.sampled_from([True, False, None])))
def test_in_sync_folds_like_a_conjunction_with_unknown(values: list[bool | None]):
    """Unknown while any is unknown; otherwise on only when all are on."""
    folded = fold_in_sync(values)
    if not values or None in values:
        assert folded is None
    else:
        assert folded is all(values)


@given(st.lists(st.sampled_from([*STATUSES, None])))
def test_sync_status_folds_to_the_worst(statuses: list[str | None]):
    """Suspended beats syncing beats pending beats out of sync beats in sync."""
    order = ["suspended", "syncing", "pending_confirmation", "out_of_sync", "in_sync"]
    present = [status for status in statuses if status is not None]
    expected = next((status for status in order if status in present), None)
    assert fold_sync_status(statuses) == expected


async def test_a_lock_without_a_coordinator_starts_no_manager(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A lock whose provider setup has not finished has nothing to sync against yet."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    before = coordinator.sync_managers
    lock = MagicMock()
    lock.coordinator = None
    lock.lock.entity_id = "lock.not_ready"

    await coordinator.async_start_sync(lock)

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

    await coordinator.async_start_sync(lock)

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
        await coordinator.async_start_sync(lock)
    finally:
        entry.mock_state(hass, ConfigEntryState.LOADED)

    assert coordinator.sync_managers == []


async def test_stopping_a_coordinator_stops_managers_it_still_holds(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A manager registered after the unload's sweep is stopped, not orphaned."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    managers = coordinator.sync_managers
    assert managers and all(manager._started for manager in managers)

    coordinator.async_stop()
    await hass.async_block_till_done()

    assert coordinator.sync_managers == []
    assert not any(manager._started for manager in managers)
