"""Tests for sync module."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_get as async_get_issue_registry,
)
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import DOMAIN, MAX_SYNC_ATTEMPTS
from custom_components.lock_code_manager.exceptions import (
    CodeRejectedError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.models import SlotCode, SyncState
from custom_components.lock_code_manager.sync import SlotState, SlotSyncManager

from .common import SLOT_1_IN_SYNC_ENTITY
from .conftest import async_trigger_sync_tick, get_in_sync_entity_obj


def _slot(
    active: str = STATE_ON,
    pin: str = "1234",
    name: str | None = "Test",
    code: str = "",
    coordinator_code: str | SlotCode | None = None,
) -> SlotState:
    """Build a SlotState for testing."""
    return SlotState(
        active_state=active,
        pin_state=pin,
        name_state=name,
        code_state=code,
        coordinator_code=coordinator_code,
    )


def _manager(last_set_pin: str | None = None) -> SlotSyncManager:
    """Build a mock SlotSyncManager with _last_set_pin set."""
    mgr = MagicMock(spec=SlotSyncManager)
    mgr._last_set_pin = last_set_pin
    mgr.calculate_in_sync = SlotSyncManager.calculate_in_sync.__get__(mgr)
    return mgr


class TestCalculateInSync:
    """Tests for SlotSyncManager.calculate_in_sync."""

    @pytest.mark.parametrize(
        ("last_set_pin", "slot_kwargs", "expected"),
        [
            # -- Active (ON) + various lock codes --
            pytest.param(
                None,
                {"active": STATE_ON, "pin": "1234", "coordinator_code": "1234"},
                True,
                id="active-matching-pin",
            ),
            pytest.param(
                None,
                {"active": STATE_ON, "pin": "1234", "coordinator_code": "5678"},
                False,
                id="active-mismatched-pin",
            ),
            pytest.param(
                "1234",
                {
                    "active": STATE_ON,
                    "pin": "1234",
                    "coordinator_code": SlotCode.UNREADABLE_CODE,
                },
                True,
                id="active-unknown-code-matching-last-set",
            ),
            pytest.param(
                "1234",
                {
                    "active": STATE_ON,
                    "pin": "5678",
                    "coordinator_code": SlotCode.UNREADABLE_CODE,
                },
                False,
                id="active-unknown-code-pin-changed",
            ),
            pytest.param(
                None,
                {
                    "active": STATE_ON,
                    "pin": "1234",
                    "coordinator_code": SlotCode.UNREADABLE_CODE,
                },
                False,
                id="active-unknown-code-never-set",
            ),
            pytest.param(
                None,
                {
                    "active": STATE_ON,
                    "pin": "1234",
                    "coordinator_code": SlotCode.EMPTY,
                },
                False,
                id="active-empty-code",
            ),
            pytest.param(
                None,
                {
                    "active": STATE_ON,
                    "pin": "1234",
                    "coordinator_code": None,
                    "code": "1234",
                },
                True,
                id="active-no-coordinator-data-matching",
            ),
            pytest.param(
                None,
                {
                    "active": STATE_ON,
                    "pin": "1234",
                    "coordinator_code": None,
                    "code": "5678",
                },
                False,
                id="active-no-coordinator-data-mismatched",
            ),
            # -- Inactive (OFF) + various lock codes --
            pytest.param(
                None,
                {"active": STATE_OFF, "coordinator_code": SlotCode.EMPTY},
                True,
                id="inactive-empty-code",
            ),
            pytest.param(
                None,
                {"active": STATE_OFF, "coordinator_code": SlotCode.UNREADABLE_CODE},
                False,
                id="inactive-unknown-code",
            ),
            pytest.param(
                None,
                {"active": STATE_OFF, "coordinator_code": "1234"},
                False,
                id="inactive-has-pin",
            ),
            pytest.param(
                None,
                {"active": STATE_OFF, "coordinator_code": None, "code": ""},
                True,
                id="inactive-empty-string-fallback",
            ),
            pytest.param(
                None,
                {"active": STATE_OFF, "coordinator_code": None, "code": "1234"},
                False,
                id="inactive-nonempty-string-fallback",
            ),
        ],
    )
    def test_calculate_in_sync(
        self,
        last_set_pin: str | None,
        slot_kwargs: dict,
        expected: bool,
    ) -> None:
        """Test calculate_in_sync with various active/inactive and code combos."""
        assert (
            _manager(last_set_pin=last_set_pin).calculate_in_sync(_slot(**slot_kwargs))
            is expected
        )


class TestTryUpgradeStateTracking:
    """Tests for upgrading catch-all to targeted state tracking."""

    async def test_upgrade_catchall_to_targeted_on_tick(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Catch-all tracking upgrades to targeted when entities become available."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        # The manager starts in catch-all mode because the code sensor entity
        # is not yet registered when _setup_state_tracking runs during setup.
        # After full setup completes, the entities are available so a tick
        # should upgrade to targeted tracking.
        assert manager._tracking_all_states
        old_unsub = manager._state_tracking_unsub

        with caplog.at_level(logging.DEBUG):
            await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

        assert not manager._tracking_all_states
        assert manager._state_tracking_unsub is not old_unsub
        assert manager._state_tracking_unsub is not None

    async def test_no_upgrade_when_entities_missing(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Catch-all tracking stays when entities are still missing."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        # Force back into catch-all with a mock unsub and clear entity map
        catch_all_unsub = MagicMock()
        manager._state_tracking_unsub = catch_all_unsub
        manager._tracking_all_states = True
        manager._entity_id_map.clear()

        with patch.object(
            manager._ent_reg,
            "async_get_entity_id",
            return_value=None,
        ):
            await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

        # Should still be in catch-all mode
        assert manager._tracking_all_states
        catch_all_unsub.assert_not_called()

    async def test_no_upgrade_when_already_targeted(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """No-op when already using targeted tracking."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        # First tick upgrades from catch-all to targeted
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)
        assert not manager._tracking_all_states
        original_unsub = manager._state_tracking_unsub

        # Second tick should be a no-op for state tracking
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)
        assert not manager._tracking_all_states
        assert manager._state_tracking_unsub is original_unsub


class TestDisableSlotExceptionHandling:
    """Tests for _disable_slot exception handling (Issue 7)."""

    async def test_disable_slot_exception_resets_sync_tracker(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Sync tracker resets even when async_disable_slot raises."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        # Set up some sync tracker state to verify it gets reset
        manager._sync_attempt_count = 5
        manager._sync_attempt_first = MagicMock()

        with patch(
            "custom_components.lock_code_manager.sync.async_disable_slot",
            new_callable=AsyncMock,
            side_effect=RuntimeError("service call failed"),
        ):
            await manager._disable_slot("test reason")

        # Sync tracker should still be reset even after exception
        assert manager._sync_attempt_count == 0
        assert manager._sync_attempt_first is None
        assert "Failed to disable slot" in caplog.text

        # Fallback repair issue should be created even though service call failed
        issue_registry = async_get_issue_registry(hass)
        entry_id = lock_code_manager_config_entry.entry_id
        issue = issue_registry.async_get_issue(DOMAIN, f"slot_disabled_{entry_id}_1")
        assert issue is not None
        assert issue.severity == IssueSeverity.WARNING

    async def test_disable_slot_success_resets_sync_tracker(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Sync tracker resets when async_disable_slot succeeds."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._sync_attempt_count = 5
        manager._sync_attempt_first = MagicMock()

        with patch(
            "custom_components.lock_code_manager.sync.async_disable_slot",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await manager._disable_slot("test reason")

        assert manager._sync_attempt_count == 0
        assert manager._sync_attempt_first is None


class TestSlotDisabledIssueCleanup:
    """Tests for slot_disabled_ issue auto-deletion when back in sync (Issue 1)."""

    async def test_slot_disabled_issue_deleted_when_back_in_sync(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """slot_disabled_ repair issue is deleted when slot comes back in sync."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager
        entry_id = lock_code_manager_config_entry.entry_id
        slot_num = manager._slot_num

        # Manually create a slot_disabled issue to simulate circuit breaker
        issue_id = f"slot_disabled_{entry_id}_{slot_num}"
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="slot_disabled",
            translation_placeholders={
                "slot_num": str(slot_num),
                "reason": "test",
            },
        )

        issue_registry = async_get_issue_registry(hass)
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

        # Initialize the manager state so it thinks it was out of sync
        manager._state = SyncState.OUT_OF_SYNC

        # Trigger a tick that will find the slot in sync (coordinator has matching code)
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

        # The issue should be deleted since slot is back in sync
        assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


class TestLockOperationFailedRetry:
    """Tests that LockOperationFailed triggers retry, not slot disable."""

    async def test_lock_operation_failed_triggers_retry(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """LockOperationFailed during sync sets dirty for retry instead of disabling slot."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        # Force out-of-sync state so _perform_sync is called:
        # set coordinator data to EMPTY so the slot appears to need a set
        manager._state = SyncState.OUT_OF_SYNC
        manager._coordinator.data[1] = SlotCode.EMPTY

        with patch.object(
            manager,
            "_perform_sync",
            new_callable=AsyncMock,
            side_effect=LockOperationFailed("service validation failed"),
        ):
            await manager._async_tick()
            await hass.async_block_till_done()

        # Should retry (OUT_OF_SYNC) rather than disable the slot
        assert manager._state is SyncState.OUT_OF_SYNC


class TestSyncStateMachine:
    """Tests for SyncState transitions in SlotSyncManager."""

    async def test_initial_state_is_loading(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Manager starts in LOADING then transitions after a tick resolves entities."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager
        # The initial tick during async_start may not resolve entities yet
        # (they may not be registered), but after full setup + a tick,
        # the manager should transition out of LOADING.
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY, set_dirty=False)
        assert manager._state in (SyncState.SYNCED, SyncState.OUT_OF_SYNC)

    async def test_loading_to_out_of_sync(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """LOADING transitions to OUT_OF_SYNC when slot is not in sync on startup."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager
        manager._state = SyncState.LOADING
        # Make coordinator data mismatch (slot active but lock has empty code)
        manager._coordinator.data[1] = SlotCode.EMPTY
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY, set_dirty=False)
        assert manager._state is SyncState.OUT_OF_SYNC

    async def test_loading_to_synced(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """LOADING transitions to SYNCED when already in sync."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager
        # The mock lock has code "1234" in slot 1, and the config also has "1234".
        # Trigger a tick to complete initial loading.
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY, set_dirty=False)
        assert manager._state is SyncState.SYNCED

    async def test_synced_to_out_of_sync_on_state_change(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """SYNCED transitions to OUT_OF_SYNC when coordinator data changes."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)
        assert manager._state is SyncState.SYNCED

        manager._coordinator.data[1] = SlotCode.EMPTY
        manager._request_sync_check()
        assert manager._state is SyncState.OUT_OF_SYNC

    async def test_out_of_sync_to_synced_after_successful_sync(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """OUT_OF_SYNC transitions through SYNCING to SYNCED on success."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        # First get out of LOADING state
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY, set_dirty=False)
        assert manager._state is SyncState.SYNCED

        manager._coordinator.data[1] = SlotCode.EMPTY
        manager._request_sync_check()
        assert manager._state is SyncState.OUT_OF_SYNC

        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY, set_dirty=False)
        assert manager._state is SyncState.SYNCED

    async def test_syncing_to_out_of_sync_on_lock_disconnected(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """SYNCING transitions to OUT_OF_SYNC on LockDisconnected."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.OUT_OF_SYNC
        manager._coordinator.data[1] = SlotCode.EMPTY

        with patch.object(
            manager,
            "_perform_sync",
            new_callable=AsyncMock,
            side_effect=LockDisconnected("test"),
        ):
            await manager._async_tick()
            await hass.async_block_till_done()

        assert manager._state is SyncState.OUT_OF_SYNC

    async def test_syncing_to_suspended_on_unexpected_error(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """SYNCING transitions to SUSPENDED on generic exception."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.OUT_OF_SYNC
        manager._coordinator.data[1] = SlotCode.EMPTY

        with patch.object(
            manager,
            "_perform_sync",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected"),
        ):
            await manager._async_tick()
            await hass.async_block_till_done()

        assert manager._state is SyncState.SUSPENDED
        assert manager._coordinator.suspended is True

    async def test_syncing_to_suspended_on_circuit_breaker(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """Circuit breaker trips transitions to SUSPENDED."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.OUT_OF_SYNC
        manager._coordinator.data[1] = SlotCode.EMPTY
        manager._sync_attempt_count = MAX_SYNC_ATTEMPTS
        manager._sync_attempt_first = dt_util.utcnow()

        await manager._async_tick()
        await hass.async_block_till_done()

        assert manager._state is SyncState.SUSPENDED
        assert manager._coordinator.suspended is True

    async def test_suspended_skips_tick(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """SUSPENDED state skips tick processing."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.SUSPENDED

        with patch.object(manager, "_async_tick_impl") as mock_tick_impl:
            await manager._async_tick()
            mock_tick_impl.assert_not_called()

    async def test_suspended_to_out_of_sync_on_coordinator_recovery(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """SUSPENDED transitions to OUT_OF_SYNC when coordinator clears suspended flag."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.SUSPENDED
        manager._coordinator.suspend()

        manager._coordinator._suspended = False
        manager._request_sync_check()

        assert manager._state is SyncState.OUT_OF_SYNC

    async def test_suspended_stays_suspended_when_coordinator_still_suspended(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """SUSPENDED stays SUSPENDED when coordinator flag is still set."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.SUSPENDED
        manager._coordinator.suspend()

        manager._request_sync_check()
        assert manager._state is SyncState.SUSPENDED

    async def test_code_rejected_still_disables_slot(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """CodeRejectedError still calls _disable_slot (profile-wide)."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.OUT_OF_SYNC
        manager._coordinator.data[1] = SlotCode.EMPTY

        with (
            patch.object(
                manager,
                "_perform_sync",
                new_callable=AsyncMock,
                side_effect=CodeRejectedError(1, "lock.test_1"),
            ),
            patch.object(
                manager,
                "_disable_slot",
                new_callable=AsyncMock,
            ) as mock_disable,
        ):
            await manager._async_tick()
            await hass.async_block_till_done()

        mock_disable.assert_called_once()
        assert manager._coordinator.suspended is False


class TestSyncStatusAttribute:
    """Tests for sync_status extra state attribute."""

    async def test_sync_status_synced(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """sync_status is 'synced' when in sync."""
        await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

        state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
        assert state is not None
        assert state.attributes.get("sync_status") == "synced"

    async def test_sync_status_out_of_sync(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """sync_status is 'out_of_sync' when out of sync."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._state = SyncState.OUT_OF_SYNC
        manager._write_state()
        await hass.async_block_till_done()

        state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
        assert state is not None
        assert state.attributes.get("sync_status") == "out_of_sync"

    async def test_sync_status_suspended(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry,
        lock_code_manager_config_entry,
    ) -> None:
        """sync_status is 'suspended' when suspended."""
        entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
        manager = entity_obj._sync_manager

        manager._coordinator.suspend()
        manager._state = SyncState.SUSPENDED
        manager._write_state()
        await hass.async_block_till_done()

        state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
        assert state is not None
        assert state.attributes.get("sync_status") == "suspended"
