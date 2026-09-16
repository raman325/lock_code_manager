"""Tests for how a slot coordinator starts its sync managers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.domain.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.domain.models import SyncState

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


async def test_sync_state_folds_over_every_credential(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """One credential out of sync makes the user out of sync, with its status."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    pin, rfid = pin_address(1), CredentialAddress(1, CredentialType.RFID)
    pin_manager = coordinator.sync_manager(LOCK_1_ENTITY_ID, pin)
    assert pin_manager is not None
    pin_manager._state = SyncState.IN_SYNC
    second = MagicMock()
    second.in_sync = False
    second.sync_status = SyncState.SUSPENDED.value
    coordinator._sync_managers[(LOCK_1_ENTITY_ID, rfid)] = second
    try:
        assert coordinator.sync_state_for(LOCK_1_ENTITY_ID, (pin,)) == (True, "in_sync")
        assert coordinator.sync_state_for(LOCK_1_ENTITY_ID, (pin, rfid)) == (
            False,
            "suspended",
        )
        # A credential with no manager reads as unknown.
        assert coordinator.sync_state_for("lock.elsewhere", (pin,)) == (None, None)
    finally:
        coordinator._sync_managers.pop((LOCK_1_ENTITY_ID, rfid))


async def test_a_manager_that_fails_to_start_does_not_take_the_others(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """One first tick raising is logged; the pass and its siblings go on."""
    entry = lock_code_manager_config_entry
    coordinator = entry.runtime_data.slot_coordinators[1]
    lock = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    await coordinator.async_stop_sync()
    with patch(
        "custom_components.lock_code_manager.domain.slot_coordinator.SlotSyncManager.async_start",
        side_effect=RuntimeError("boom"),
    ):
        await coordinator.async_start_sync(lock, er.async_get(hass))
    assert coordinator.sync_manager(LOCK_1_ENTITY_ID, pin_address(1)) is not None
