"""Tests for how a slot coordinator starts, stops, and reports its sync managers."""

from __future__ import annotations

import asyncio
from functools import partial
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.domain.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.domain.models import SyncState
from custom_components.lock_code_manager.domain.slot_coordinator import (
    SlotEntityCoordinator,
)

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

START = "custom_components.lock_code_manager.domain.slot_coordinator.SlotSyncManager.async_start"


@pytest.fixture
async def stopped_slot(mock_lock_config_entry, lock_code_manager_config_entry):
    """Slot 1's coordinator with its managers stopped, and the first lock."""
    entry = lock_code_manager_config_entry
    coordinator = entry.runtime_data.slot_coordinators[1]
    await coordinator.async_stop_sync()
    return coordinator, entry.runtime_data.locks[LOCK_1_ENTITY_ID]


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
    hass: HomeAssistant, stopped_slot
) -> None:
    """Once stopped, a coordinator has no unload left to stop what it would start."""
    coordinator, lock = stopped_slot
    coordinator.async_stop()

    await coordinator.async_start_sync(lock)

    assert coordinator.sync_managers == []


async def test_a_coordinator_the_entry_no_longer_runs_starts_no_manager(
    hass: HomeAssistant, stopped_slot, lock_code_manager_config_entry
) -> None:
    """A pass that built its coordinator on runtime data the entry replaced starts nothing."""
    _, lock = stopped_slot
    stale = SlotEntityCoordinator(hass, lock_code_manager_config_entry, 1)
    stale.async_start()
    try:
        await stale.async_start_sync(lock)
    finally:
        stale.async_stop()

    assert stale.sync_managers == []


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


async def test_a_manager_change_reaches_only_its_locks_subscribers(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A manager's change is the business of its lock's sensors, not the other locks'."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    heard: list[str] = []
    unsubs = [
        coordinator.register_sync_subscriber(
            lock_entity_id, partial(heard.append, lock_entity_id)
        )
        for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    ]
    manager = coordinator.sync_manager(LOCK_1_ENTITY_ID, pin_address(1))
    assert manager is not None
    try:
        manager._write_state()
    finally:
        for unsub in unsubs:
            unsub()

    assert heard == [LOCK_1_ENTITY_ID]


async def test_a_manager_that_fails_to_start_does_not_take_the_others(
    hass: HomeAssistant, stopped_slot
) -> None:
    """One first tick raising is logged; the pass and its siblings go on."""
    coordinator, lock = stopped_slot
    with patch(START, side_effect=RuntimeError("boom")):
        await coordinator.async_start_sync(lock)
    assert coordinator.sync_manager(LOCK_1_ENTITY_ID, pin_address(1)) is not None


async def test_a_cancelled_start_cancels_the_pass(
    hass: HomeAssistant, stopped_slot
) -> None:
    """A manager whose start was cancelled from outside reports that, not silence."""
    coordinator, lock = stopped_slot
    with (
        patch(START, side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await coordinator.async_start_sync(lock)
