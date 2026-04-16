"""Tests for data helpers (compute_entry_config_diff, etc)."""

from dataclasses import FrozenInstanceError

import pytest

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS
from custom_components.lock_code_manager.data import (
    EntryConfigDiff,
    compute_entry_config_diff,
)


def _slot(pin: str = "1234") -> dict:
    """Trivial slot config dict for tests."""
    return {"pin": pin, "enabled": True}


def test_diff_empty_inputs() -> None:
    """No old, no new -> empty diff, no changes."""
    diff = compute_entry_config_diff({}, {})

    assert diff.slots_added == {}
    assert diff.slots_removed == {}
    assert diff.slots_unchanged == set()
    assert diff.locks_added == []
    assert diff.locks_removed == []
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()
    assert not diff.has_changes


def test_diff_added_slots_and_locks() -> None:
    """Brand-new entry: everything is added."""
    new = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}}

    diff = compute_entry_config_diff({}, new)

    assert diff.slots_added == {1: _slot(), 2: _slot()}
    assert diff.locks_added == ["lock.a"]
    assert diff.pairs_added == frozenset({("lock.a", 1), ("lock.a", 2)})
    assert diff.has_changes


def test_diff_removed_slots_and_locks() -> None:
    """All slots/locks removed."""
    old = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}

    diff = compute_entry_config_diff(old, {})

    assert diff.slots_removed == {1: _slot()}
    assert diff.locks_removed == ["lock.a"]
    assert diff.pairs_removed == frozenset({("lock.a", 1)})
    assert diff.has_changes


def test_diff_no_changes() -> None:
    """Same locks and slots -> no diff, no has_changes."""
    config = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}

    diff = compute_entry_config_diff(config, config)

    assert not diff.has_changes
    assert diff.slots_unchanged == {1}
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()


def test_diff_str_keys_match_int_keys() -> None:
    """Stored data has str slot keys; voluptuous output has int.

    The helper must treat ``"1"`` and ``1`` as the same slot for both
    set comparisons and pair tuples — otherwise the options flow would
    flag every existing slot as "newly added" the first time the user
    edits options.
    """
    old = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {"1": _slot(), "2": _slot()}}
    new = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}}

    diff = compute_entry_config_diff(old, new)

    assert diff.slots_added == {}
    assert diff.slots_removed == {}
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()
    assert not diff.has_changes


def test_diff_slot_dicts_preserve_new_key_type() -> None:
    """slots_added/unchanged use the *new* mapping's key type.

    The listener indexes back into raw_new_slots/raw_old_slots with these
    keys to look up the slot config dict, so changing the key type would
    break those lookups.
    """
    # Both sides use str keys (typical of the listener case where both
    # `data` and `options` come from JSON storage round-trips)
    old = {CONF_SLOTS: {"1": _slot()}}
    new = {CONF_SLOTS: {"1": _slot("9999"), "2": _slot()}}

    diff = compute_entry_config_diff(old, new)

    # New mapping has str keys -> outputs preserve str
    assert "2" in diff.slots_added
    assert "1" in diff.slots_unchanged
    # Pair tuples normalize to int regardless
    assert diff.pairs_added == frozenset()  # no locks given


def test_diff_pair_added_for_new_lock_with_existing_slot() -> None:
    """A new lock with a slot already managed elsewhere is a NEW pair.

    This is the key options-flow case: user has lock.a managing slot 1,
    then adds lock.b — (lock.b, 1) is a brand-new pair to scan, even
    though slot 1 is "unchanged" in the slot dict view.
    """
    old = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    new = {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}}

    diff = compute_entry_config_diff(old, new)

    assert diff.locks_added == ["lock.b"]
    assert diff.slots_added == {}
    # (lock.b, 1) is new even though slot 1 isn't
    assert diff.pairs_added == frozenset({("lock.b", 1)})


def test_diff_pair_added_for_new_slot_on_existing_lock() -> None:
    """Adding a slot creates a new pair on every existing lock."""
    old = {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}}
    new = {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot(), 2: _slot()}}

    diff = compute_entry_config_diff(old, new)

    assert diff.slots_added == {2: _slot()}
    assert diff.pairs_added == frozenset({("lock.a", 2), ("lock.b", 2)})


def test_diff_is_immutable() -> None:
    """EntryConfigDiff is a frozen dataclass — supports use as cached state."""
    diff = compute_entry_config_diff({}, {})

    with pytest.raises(FrozenInstanceError):
        diff.slots_added = {1: _slot()}  # type: ignore[misc]

    assert isinstance(diff, EntryConfigDiff)
