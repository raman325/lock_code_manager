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

    assert dict(diff.slots_added) == {}
    assert dict(diff.slots_removed) == {}
    assert diff.slots_unchanged == frozenset()
    assert diff.locks_added == ()
    assert diff.locks_removed == ()
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()
    assert not diff.has_changes


def test_diff_added_slots_and_locks() -> None:
    """Brand-new entry: everything is added."""
    new = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}}

    diff = compute_entry_config_diff({}, new)

    assert dict(diff.slots_added) == {1: _slot(), 2: _slot()}
    assert diff.locks_added == ("lock.a",)
    assert diff.pairs_added == frozenset({("lock.a", 1), ("lock.a", 2)})
    assert diff.has_changes


def test_diff_removed_slots_and_locks() -> None:
    """All slots/locks removed."""
    old = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}

    diff = compute_entry_config_diff(old, {})

    assert dict(diff.slots_removed) == {1: _slot()}
    assert diff.locks_removed == ("lock.a",)
    assert diff.pairs_removed == frozenset({("lock.a", 1)})
    assert diff.has_changes


def test_diff_no_changes() -> None:
    """Same locks and slots -> no diff, no has_changes."""
    config = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}

    diff = compute_entry_config_diff(config, config)

    assert not diff.has_changes
    assert diff.slots_unchanged == frozenset({1})
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

    assert dict(diff.slots_added) == {}
    assert dict(diff.slots_removed) == {}
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()
    assert not diff.has_changes


def test_diff_slot_dicts_preserve_source_key_types() -> None:
    """slots_{added,unchanged} take new's key type; slots_removed takes old's.

    The listener indexes back into raw_new_slots/raw_old_slots with these
    keys to look up the slot config dict, so changing the key type would
    break those lookups.
    """
    # Both sides use str keys (typical of the listener case where both
    # `data` and `options` come from JSON storage round-trips)
    old = {CONF_SLOTS: {"1": _slot(), "3": _slot()}}
    new = {CONF_SLOTS: {"1": _slot("9999"), "2": _slot()}}

    diff = compute_entry_config_diff(old, new)

    # slots_added/unchanged take new's key type (str here)
    assert "2" in diff.slots_added
    assert "1" in diff.slots_unchanged
    # slots_removed takes old's key type (also str here, but importantly
    # it is the OLD mapping's keys regardless)
    assert "3" in diff.slots_removed


def test_diff_pair_added_for_new_lock_with_existing_slot() -> None:
    """A new lock with a slot already managed elsewhere is a NEW pair.

    This is the key options-flow case: user has lock.a managing slot 1,
    then adds lock.b — (lock.b, 1) is a brand-new pair to scan, even
    though slot 1 is "unchanged" in the slot dict view.
    """
    old = {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    new = {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}}

    diff = compute_entry_config_diff(old, new)

    assert diff.locks_added == ("lock.b",)
    assert dict(diff.slots_added) == {}
    # (lock.b, 1) is new even though slot 1 isn't
    assert diff.pairs_added == frozenset({("lock.b", 1)})


def test_diff_pair_added_for_new_slot_on_existing_lock() -> None:
    """Adding a slot creates a new pair on every existing lock."""
    old = {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}}
    new = {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot(), 2: _slot()}}

    diff = compute_entry_config_diff(old, new)

    assert dict(diff.slots_added) == {2: _slot()}
    assert diff.pairs_added == frozenset({("lock.a", 2), ("lock.b", 2)})


def test_diff_is_deeply_immutable() -> None:
    """EntryConfigDiff fields are immutable containers — safe as cached state.

    The dataclass is frozen (attribute reassignment blocked) AND the
    contained dicts/sets/lists are immutable variants
    (``MappingProxyType`` / ``frozenset`` / ``tuple``), so callers
    cannot mutate the diff after the fact.
    """
    diff = compute_entry_config_diff(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}, {}
    )

    # Attribute reassignment blocked
    with pytest.raises(FrozenInstanceError):
        diff.slots_added = {99: _slot()}  # type: ignore[misc]

    # Contained dicts are read-only
    with pytest.raises(TypeError):
        diff.slots_removed[99] = _slot()  # type: ignore[index]

    # Contained sets cannot grow
    assert not hasattr(diff.slots_unchanged, "add")
    assert not hasattr(diff.pairs_added, "add")

    # Contained lists are tuples (no mutation methods)
    assert not hasattr(diff.locks_added, "append")
    assert not hasattr(diff.locks_removed, "append")

    assert isinstance(diff, EntryConfigDiff)
