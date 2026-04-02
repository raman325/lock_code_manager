"""Tests for sync module."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.lock_code_manager.models import SlotCode, SlotState
from custom_components.lock_code_manager.sync import SlotSyncManager


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


class TestCalculateInSync:
    """Tests for SlotSyncManager.calculate_in_sync."""

    # -- Active (ON) + various lock codes ------------------------------------

    def test_active_matching_pin(self) -> None:
        """Active slot with matching PIN is in sync."""
        assert SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_ON, pin="1234", coordinator_code="1234")
        )

    def test_active_mismatched_pin(self) -> None:
        """Active slot with different PIN is out of sync."""
        assert not SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_ON, pin="1234", coordinator_code="5678")
        )

    def test_active_unknown_code(self) -> None:
        """Active slot with UNKNOWN code assumes in sync (can't compare)."""
        assert SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_ON, pin="1234", coordinator_code=SlotCode.UNKNOWN)
        )

    def test_active_empty_code(self) -> None:
        """Active slot with EMPTY code is out of sync (need to set)."""
        assert not SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_ON, pin="1234", coordinator_code=SlotCode.EMPTY)
        )

    def test_active_no_coordinator_data_matching(self) -> None:
        """Active slot falls back to code_state when coordinator_code is None."""
        assert SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_ON, pin="1234", coordinator_code=None, code="1234")
        )

    def test_active_no_coordinator_data_mismatched(self) -> None:
        """Active slot falls back to code_state when coordinator_code is None."""
        assert not SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_ON, pin="1234", coordinator_code=None, code="5678")
        )

    # -- Inactive (OFF) + various lock codes ---------------------------------

    def test_inactive_empty_code(self) -> None:
        """Inactive slot with EMPTY code is in sync."""
        assert SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_OFF, coordinator_code=SlotCode.EMPTY)
        )

    def test_inactive_unknown_code(self) -> None:
        """Inactive slot with UNKNOWN code is out of sync (need to clear)."""
        assert not SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_OFF, coordinator_code=SlotCode.UNKNOWN)
        )

    def test_inactive_has_pin(self) -> None:
        """Inactive slot with a PIN on lock is out of sync (need to clear)."""
        assert not SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_OFF, coordinator_code="1234")
        )

    def test_inactive_empty_string_fallback(self) -> None:
        """Inactive slot with empty string code_state is in sync (fallback)."""
        assert SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_OFF, coordinator_code=None, code="")
        )

    def test_inactive_nonempty_string_fallback(self) -> None:
        """Inactive slot with code on lock via fallback is out of sync."""
        assert not SlotSyncManager.calculate_in_sync(
            _slot(active=STATE_OFF, coordinator_code=None, code="1234")
        )
