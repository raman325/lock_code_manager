"""Properties of how a user's in-sync state folds across credentials."""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.domain.models import SyncState
from custom_components.lock_code_manager.domain.sync import (
    fold_in_sync,
    fold_sync_status,
)

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
    """Suspended beats out of sync beats syncing beats pending beats in sync."""
    order = ["suspended", "out_of_sync", "syncing", "pending_confirmation", "in_sync"]
    present = [status for status in statuses if status is not None]
    expected = next((status for status in order if status in present), None)
    assert fold_sync_status(statuses) == expected
