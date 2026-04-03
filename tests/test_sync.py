"""Tests for sync module."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.models import SlotCode, SlotState
from custom_components.lock_code_manager.sync import SlotSyncManager

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
                    "coordinator_code": SlotCode.UNKNOWN,
                },
                True,
                id="active-unknown-code-matching-last-set",
            ),
            pytest.param(
                "1234",
                {
                    "active": STATE_ON,
                    "pin": "5678",
                    "coordinator_code": SlotCode.UNKNOWN,
                },
                False,
                id="active-unknown-code-pin-changed",
            ),
            pytest.param(
                None,
                {
                    "active": STATE_ON,
                    "pin": "1234",
                    "coordinator_code": SlotCode.UNKNOWN,
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
                {"active": STATE_OFF, "coordinator_code": SlotCode.UNKNOWN},
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
