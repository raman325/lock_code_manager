"""Tests for data helpers (EntryConfig, EntryConfigDiff, etc)."""

from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.config import (
    EntryConfig,
    EntryConfigDiff,
    build_slot_device_identifier,
    build_slot_unique_id,
    parse_slot_device_identifier,
    parse_slot_unique_id,
)
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)


def _slot(pin: str = "1234", name: str | None = None) -> dict:
    """Trivial slot config dict for tests.

    A slot without a name gets a generated one, since the name is the key the
    configuration is stored under.
    """
    return {"pin": pin, "enabled": True, **({"name": name} if name else {})}


def _named(slot_num: int, pin: str = "1234") -> dict:
    """The slot as EntryConfig returns it: with the name it was given."""
    return {"name": f"User {slot_num}", "pin": pin, "enabled": True}


def _cfg(mapping: dict | None = None) -> EntryConfig:
    """Build an EntryConfig from a raw mapping (test convenience)."""
    return EntryConfig.from_mapping(mapping) if mapping else EntryConfig.empty()


# --- EntryConfigDiff tests ---


def test_diff_empty_inputs() -> None:
    """No old, no new -> empty diff, no changes."""
    diff = EntryConfigDiff()

    assert dict(diff.slots_added) == {}
    assert dict(diff.slots_removed) == {}
    assert diff.locks_added == ()
    assert diff.locks_removed == ()
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()
    assert not diff.has_changes
    # Source configs are accessible after construction (default to empty)
    assert diff.old == EntryConfig.empty()
    assert diff.new == EntryConfig.empty()


def test_diff_added_slots_and_locks() -> None:
    """Brand-new entry: everything is added (omit `old` -> defaults to empty)."""
    new = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}})

    diff = EntryConfigDiff(new=new)

    assert dict(diff.slots_added) == {1: _named(1), 2: _named(2)}
    assert diff.locks_added == ("lock.a",)
    assert diff.pairs_added == frozenset({("lock.a", 1), ("lock.a", 2)})
    assert diff.has_changes


def test_diff_removed_slots_and_locks() -> None:
    """All slots/locks removed (omit `new` -> defaults to empty)."""
    old = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}})

    diff = EntryConfigDiff(old=old)

    assert dict(diff.slots_removed) == {1: _named(1)}
    assert diff.locks_removed == ("lock.a",)
    assert diff.pairs_removed == frozenset({("lock.a", 1)})
    assert diff.has_changes


def test_diff_no_changes() -> None:
    """Same config on both sides -> no diff, no has_changes."""
    config = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}})

    diff = EntryConfigDiff(old=config, new=config)

    assert not diff.has_changes
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()


def test_diff_str_keys_match_int_keys() -> None:
    """
    Stored data has str slot keys; voluptuous output has int.

    EntryConfig.from_mapping normalizes keys to int up front, so by the
    time the diff is computed both sides are int-keyed and ``"1"`` /
    ``1`` are treated as the same slot. Without this, the options flow
    would flag every existing slot as "newly added" the first time the
    user edits options.
    """
    old = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {"1": _slot(), "2": _slot()}})
    new = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}})

    diff = EntryConfigDiff(old=old, new=new)

    assert dict(diff.slots_added) == {}
    assert dict(diff.slots_removed) == {}
    assert diff.pairs_added == frozenset()
    assert diff.pairs_removed == frozenset()
    assert not diff.has_changes


def test_diff_slot_dicts_always_int_keyed() -> None:
    """All slot-dict outputs are int-keyed regardless of input key type."""
    old = _cfg({CONF_SLOTS: {"1": _slot(), "3": _slot()}})
    new = _cfg({CONF_SLOTS: {1: _slot("9999"), 2: _slot()}})

    diff = EntryConfigDiff(old=old, new=new)

    assert 2 in diff.slots_added
    assert "2" not in diff.slots_added
    assert 3 in diff.slots_removed
    assert "3" not in diff.slots_removed


def test_diff_pair_added_for_new_lock_with_existing_slot() -> None:
    """
    A new lock with a slot already managed elsewhere is a NEW pair.

    This is the key options-flow case: user has lock.a managing slot 1,
    then adds lock.b — (lock.b, 1) is a brand-new pair to scan, even
    though slot 1 is "unchanged" in the slot dict view.
    """
    old = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}})
    new = _cfg({CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}})

    diff = EntryConfigDiff(old=old, new=new)

    assert diff.locks_added == ("lock.b",)
    assert dict(diff.slots_added) == {}
    # (lock.b, 1) is new even though slot 1 isn't
    assert diff.pairs_added == frozenset({("lock.b", 1)})


def test_diff_pair_added_for_new_slot_on_existing_lock() -> None:
    """Adding a slot creates a new pair on every existing lock."""
    old = _cfg({CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}})
    new = _cfg({CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot(), 2: _slot()}})

    diff = EntryConfigDiff(old=old, new=new)

    assert dict(diff.slots_added) == {2: _named(2)}
    assert diff.pairs_added == frozenset({("lock.a", 2), ("lock.b", 2)})


def test_subtraction_operator_is_diff_sugar() -> None:
    """``a - b`` on EntryConfig returns the same as EntryConfigDiff(old=a, new=b)."""
    a = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}})
    b = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}})

    via_operator = a - b
    via_constructor = EntryConfigDiff(old=a, new=b)

    # Same diff content (the dataclasses are equal field-for-field;
    # the source configs are also equal so __eq__ matches)
    assert dict(via_operator.slots_added) == dict(via_constructor.slots_added)
    assert via_operator.pairs_added == via_constructor.pairs_added
    assert via_operator.has_changes is via_constructor.has_changes


def test_subtraction_with_non_entry_config_raises_type_error() -> None:
    """
    ``cfg - non_config`` returns NotImplemented -> Python raises TypeError.

    Without the isinstance guard, the operator would succeed and the
    error would surface deep inside EntryConfigDiff.__post_init__ as
    a confusing AttributeError ("'str' has no attribute 'slots'").
    Returning NotImplemented lets Python's operator protocol surface
    the standard "unsupported operand type(s)" message instead.
    """
    cfg = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}})

    with pytest.raises(TypeError, match="unsupported operand"):
        cfg - "not a config"  # type: ignore[operator]
    with pytest.raises(TypeError, match="unsupported operand"):
        cfg - {"locks": []}  # type: ignore[operator]
    with pytest.raises(TypeError, match="unsupported operand"):
        cfg - None  # type: ignore[operator]


def test_diff_is_deeply_immutable() -> None:
    """
    EntryConfigDiff fields are immutable containers — safe as cached state.

    The dataclass is frozen (attribute reassignment blocked) AND the
    contained dicts/sets/lists are immutable variants
    (``MappingProxyType`` / ``frozenset`` / ``tuple``), so callers
    cannot mutate the diff after the fact.
    """
    # Build a diff with both an added slot AND a removed slot so we can
    # exercise inner-mutation guards on both
    diff = EntryConfigDiff(
        old=_cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}),
        new=_cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {2: _slot()}}),
    )

    # Attribute reassignment blocked
    with pytest.raises(FrozenInstanceError):
        diff.slots_added = {99: _slot()}  # type: ignore[misc]

    # Outer dicts are read-only
    with pytest.raises(TypeError):
        diff.slots_removed[99] = _slot()  # type: ignore[index]
    with pytest.raises(TypeError):
        diff.slots_added[99] = _slot()  # type: ignore[index]

    # INNER per-slot dicts are also read-only (deep immutability).
    # Without this, callers could do diff.slots_added[2]["pin"] = "X"
    # and mutate cached state — defeating the whole point of frozen.
    with pytest.raises(TypeError):
        diff.slots_added[2]["pin"] = "9999"  # type: ignore[index]
    with pytest.raises(TypeError):
        diff.slots_removed[1]["pin"] = "9999"  # type: ignore[index]

    # Contained sets cannot grow
    assert not hasattr(diff.pairs_added, "add")

    # Contained lists are tuples (no mutation methods)
    assert not hasattr(diff.locks_added, "append")
    assert not hasattr(diff.locks_removed, "append")

    assert isinstance(diff, EntryConfigDiff)


def test_diff_snapshots_inner_slot_dicts() -> None:
    """
    Mutating the source slot config after diff is built doesn't leak in.

    The defensive ``dict(v)`` copy inside __post_init__ snapshots slot
    configs at construction time — later mutations to the original
    mapping don't change the diff view.
    """
    inner_slot = {"pin": "1234", "enabled": True}
    new = _cfg({CONF_SLOTS: {1: inner_slot}})

    diff = EntryConfigDiff(new=new)

    # Mutate the original inner slot dict after the diff is built
    inner_slot["pin"] = "9999"

    # Diff snapshot is unaffected
    assert diff.slots_added[1]["pin"] == "1234"


# --- EntryConfig tests ---


def test_entry_config_empty() -> None:
    """EntryConfig.empty() returns a config with no locks or slots."""
    config = EntryConfig.empty()
    assert config.locks == ()
    assert dict(config.slots) == {}
    assert not config.has_lock("lock.anything")
    assert not config.has_slot(1)


def test_entry_config_from_mapping_normalizes_str_slot_keys_to_int() -> None:
    """
    from_mapping normalizes str slot keys (JSON storage) to int.

    The whole point of EntryConfig: every consumer sees int keys
    regardless of how the config was loaded.
    """
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {"1": _slot(), "2": _slot()}}
    )
    assert set(config.slots.keys()) == {1, 2}
    assert all(isinstance(k, int) for k in config.slots)


def test_entry_config_accessors_absorb_str_or_int_slot_num() -> None:
    """
    has_slot / slot accept either type and normalize internally.

    Lets callers stop carrying ``int(slot_num)`` casts at every read
    site. The internal storage is still ``int``-keyed; the accessors
    just absorb the type variance.
    """
    config = EntryConfig.from_mapping({CONF_SLOTS: {"1": _slot(pin="abc")}})

    assert config.has_slot(1)
    assert config.has_slot("1")
    # The name comes back too: it is the identity now, not an optional field.
    assert config.slot(1) == _named(1, pin="abc")
    assert config.slot("1") == _named(1, pin="abc")
    # Missing slot returns empty mapping (not KeyError)
    assert config.slot(99) == {}
    assert config.slot("99") == {}


def test_entry_config_from_mapping_preserves_int_slot_keys() -> None:
    """Int keys (voluptuous output) pass through unchanged."""
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    )
    assert set(config.slots.keys()) == {1}


def test_entry_config_from_entry_options_preferred() -> None:
    """from_entry prefers options over data (the options-flow precedence)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: ["lock.old"], CONF_SLOTS: {"1": _slot("old")}},
        options={CONF_LOCKS: ["lock.new"], CONF_SLOTS: {"2": _slot("new")}},
    )
    config = EntryConfig.from_entry(entry)
    # Options wins entirely (not merged)
    assert config.locks == ("lock.new",)
    assert set(config.slots.keys()) == {2}


def test_entry_config_from_entry_falls_back_to_data() -> None:
    """When options is empty, from_entry reads from data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: ["lock.a"], CONF_SLOTS: {"1": _slot()}},
    )
    config = EntryConfig.from_entry(entry)
    assert config.locks == ("lock.a",)
    assert set(config.slots.keys()) == {1}


def test_entry_config_is_deeply_immutable() -> None:
    """EntryConfig is frozen and contains read-only mappings."""
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    )

    with pytest.raises(FrozenInstanceError):
        config.locks = ("lock.b",)  # type: ignore[misc]

    # Outer slots mapping is read-only
    with pytest.raises(TypeError):
        config.slots[99] = _slot()  # type: ignore[index]

    # Inner slot config dict is also read-only
    with pytest.raises(TypeError):
        config.slots[1]["pin"] = "9999"  # type: ignore[index]


def test_get_entry_config_uses_runtime_data_when_present() -> None:
    """
    get_entry_config returns the cached EntryConfig from runtime_data.

    No fresh construction — same instance is returned, allowing the
    listener's cache to act as a true singleton view of the entry.
    """
    cached = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.cached"], CONF_SLOTS: {1: _slot("cached")}}
    )
    fake_entry = SimpleNamespace(
        runtime_data=SimpleNamespace(config=cached),
        # data/options would normally be here too — proving they're not
        # consulted when the cache is present:
        data={CONF_LOCKS: ["lock.different"], CONF_SLOTS: {}},
        options={},
    )

    result = get_entry_config(fake_entry)  # type: ignore[arg-type]

    # Returns the cached instance — same object, not a fresh build
    assert result is cached
    assert result.locks == ("lock.cached",)


def test_get_entry_config_falls_back_when_no_runtime_data() -> None:
    """
    get_entry_config builds fresh from raw data if runtime_data is absent.

    Covers iteration over hass.config_entries.async_entries(DOMAIN) which
    may yield entries not yet loaded.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: ["lock.fresh"], CONF_SLOTS: {"1": _slot()}},
    )
    # MockConfigEntry has no runtime_data attribute by default

    result = get_entry_config(entry)

    assert result.locks == ("lock.fresh",)
    assert set(result.slots.keys()) == {1}


def test_get_entry_config_falls_back_when_runtime_data_lacks_config() -> None:
    """
    If runtime_data exists but doesn't have a .config attr, fall back.

    Defends against the brief window during async_setup_entry before
    runtime_data.config is initialized.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: ["lock.fresh"], CONF_SLOTS: {"1": _slot()}},
    )
    entry.runtime_data = SimpleNamespace()  # no config attr

    result = get_entry_config(entry)

    assert result.locks == ("lock.fresh",)


# --- Immutable update helper tests ---


def test_with_slot_field_set_cannot_create_a_user() -> None:
    """Setting a field on an unoccupied slot is a no-op, not a creation.

    A user is created by name, and a bare slot number supplies none. Inventing
    one would put a user on the lock that nobody asked for.
    """
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    )

    assert config.with_slot_field_set(2, "pin", "5678") is config


def test_with_user_field_set_does_not_create_a_user() -> None:
    """Setting a field on somebody who does not exist is a no-op.

    Creating a user also has to allocate them a slot, which this has no view to
    do. One created without a slot reaches no lock and is dropped the next time
    the configuration round-trips.
    """
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    )

    assert config.with_user_field_set("Alice", "pin", "5678") is config


def test_setting_the_name_re_keys_the_user() -> None:
    """The name is the identity, so setting it moves the user, not a field.

    Stored as a field, the user could not be found by their new name after a
    reload and would hold no slot.
    """
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: {**_slot(), "name": "Alice"}}}
    )

    renamed = config.with_slot_field_set(1, "name", "Bob")
    reloaded = EntryConfig.from_mapping(renamed.to_dict())

    assert set(reloaded.users) == {"Bob"}
    assert "name" not in reloaded.users["Bob"]
    assert reloaded.assignment.slot("Bob") == 1
    assert "Alice" not in reloaded.users


def test_with_slot_field_set_updates_existing_field() -> None:
    """with_slot_field_set replaces an existing field on an existing slot."""
    config = EntryConfig.from_mapping({CONF_SLOTS: {1: _slot(pin="abc")}})

    updated = config.with_slot_field_set(1, "pin", "xyz")

    assert updated.slots[1]["pin"] == "xyz"
    assert updated.slots[1]["enabled"] is True  # other fields preserved


def test_with_slot_field_set_does_not_mutate_original() -> None:
    """with_slot_field_set returns a new EntryConfig — original is untouched."""
    config = EntryConfig.from_mapping({CONF_SLOTS: {1: _slot(pin="abc")}})

    updated = config.with_slot_field_set(1, "pin", "xyz")

    assert config.slots[1]["pin"] == "abc"  # unchanged
    assert updated is not config
    assert updated.slots[1]["pin"] == "xyz"


def test_with_slot_field_set_accepts_str_slot_num() -> None:
    """Normalizes the slot_num argument the same way has_slot does."""
    config = EntryConfig.from_mapping({CONF_SLOTS: {1: _slot()}})

    updated = config.with_slot_field_set("1", "pin", "new")

    assert updated.slots[1]["pin"] == "new"


def test_with_slot_field_set_output_is_deeply_immutable() -> None:
    """The returned EntryConfig is frozen with read-only mappings, same as the input."""
    config = EntryConfig.from_mapping({CONF_SLOTS: {1: {**_slot(), "name": "Raman"}}})
    updated = config.with_user_field_set("Raman", "pin", "9999")

    with pytest.raises(TypeError):
        updated.users["Raman"]["pin"] = "0000"  # type: ignore[index]


def test_with_slot_field_removed_removes_key() -> None:
    """with_slot_field_removed drops the named key from the slot config."""
    config = EntryConfig.from_mapping(
        {CONF_SLOTS: {1: {"pin": "1234", "enabled": True, "entity_id": "binary.a"}}}
    )

    updated = config.with_slot_field_removed(1, "entity_id")

    assert "entity_id" not in updated.slots[1]
    assert updated.slots[1]["pin"] == "1234"  # other fields preserved


def test_with_slot_field_removed_is_noop_when_absent() -> None:
    """Returns self (same instance) when there's nothing to remove."""
    config = EntryConfig.from_mapping({CONF_SLOTS: {1: _slot()}})

    # Slot exists but key doesn't
    assert config.with_slot_field_removed(1, "entity_id") is config
    # Slot doesn't exist at all
    assert config.with_slot_field_removed(99, "pin") is config


def test_to_dict_round_trips_through_from_mapping() -> None:
    """
    to_dict → from_mapping reconstructs an equivalent EntryConfig.

    Guards the write path used by SlotEntityCoordinator and the helpers
    write functions: they build a new EntryConfig, call to_dict(), hand
    it to async_update_entry, and expect the eventual listener re-read
    to produce the same logical config.
    """
    original = EntryConfig.from_mapping(
        {
            CONF_LOCKS: ["lock.a", "lock.b"],
            CONF_SLOTS: {1: _slot("1234"), 2: _slot("5678")},
        }
    )

    round_tripped = EntryConfig.from_mapping(original.to_dict())

    assert round_tripped.locks == original.locks
    assert dict(round_tripped.slots) == dict(original.slots)


def test_to_dict_produces_plain_mutable_dicts() -> None:
    """
    to_dict output is plain dict (not MappingProxyType).

    HA's async_update_entry expects a plain dict it can serialize; the
    read-only wrappers EntryConfig uses internally would break that.
    """
    config = EntryConfig.from_mapping(
        {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}}
    )

    result = config.to_dict()

    assert isinstance(result, dict)
    assert isinstance(result[CONF_USERS], dict)
    assert isinstance(result[CONF_USERS]["User 1"], dict)
    # Mutability — the returned dicts are the caller's to modify
    result[CONF_USERS]["User 1"]["pin"] = "9999"
    result[CONF_LOCKS].append("lock.b")
    # Original EntryConfig is untouched by that mutation
    assert config.slots[1]["pin"] == "1234"
    assert config.locks == ("lock.a",)


# Slot device identifier round-trip (issue #1399)


@pytest.mark.parametrize("slot_num", [1, 2, 0, 30, 255, -1, -42])
def test_slot_device_identifier_round_trips(slot_num: int) -> None:
    """
    Every identifier the builder emits must parse back to the same slot.

    A slot the builder encodes but the parser rejects is invisible to both the
    orphan sweep and the removal hook, which is the stuck undeletable device
    issue #1399 is about. Negative slots are included because the slots YAML
    schema does not bound the key.
    """
    identifier = build_slot_device_identifier("abc123", slot_num)
    assert parse_slot_device_identifier("abc123", identifier) == slot_num


def test_parse_slot_device_identifier_rejects_non_slot_identifiers() -> None:
    """The entry's own device and other entries' devices are not slot devices."""
    # The entry device carries the bare entry_id, with no slot suffix.
    assert parse_slot_device_identifier("abc123", "abc123") is None
    # A different entry's slot device.
    assert parse_slot_device_identifier("abc123", "def456|1") is None
    # Not a slot number at all.
    assert parse_slot_device_identifier("abc123", "abc123|name") is None
    assert parse_slot_device_identifier("abc123", "abc123|") is None


@pytest.mark.parametrize("suffix", ["+1", "1_0", " 1", "01"])
def test_parse_slot_device_identifier_rejects_builder_aliases(suffix: str) -> None:
    """
    Reject int-parseable spellings the builder would never emit.

    ``int()`` accepts all of these, but treating them as slot numbers would map
    two distinct identifiers onto one slot.
    """
    assert parse_slot_device_identifier("abc123", f"abc123|{suffix}") is None


# --- has_changes: one field at a time (mutation-testing gap) ---


@pytest.mark.parametrize(
    ("label", "old", "new"),
    [
        (
            "slots_added",
            {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}},
            {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}},
        ),
        (
            "slots_removed",
            {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(), 2: _slot()}},
            {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}},
        ),
        (
            "locks_added",
            {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}},
            {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}},
        ),
        (
            "locks_removed",
            {CONF_LOCKS: ["lock.a", "lock.b"], CONF_SLOTS: {1: _slot()}},
            {CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot()}},
        ),
    ],
)
def test_has_changes_is_true_for_each_field_alone(
    label: str, old: dict, new: dict
) -> None:
    """
    Any ONE of the four diff fields alone is enough to report a change.

    The existing tests only ever move slots and locks together, or neither,
    so each individual disjunct in ``has_changes`` went unpinned -- mutating
    any single ``or`` to ``and`` survived. ``has_changes`` gates the Lovelace
    dashboard re-render, so a collapsed disjunct means a user who only adds a
    slot (or only removes a lock) silently gets a stale dashboard.
    """
    diff = EntryConfigDiff(old=_cfg(old), new=_cfg(new))

    assert diff.has_changes is True, f"{label} alone should count as a change"
    # Exactly the named field is populated; the other three stay empty.
    populated = {
        name
        for name in ("slots_added", "slots_removed", "locks_added", "locks_removed")
        if getattr(diff, name)
    }
    assert populated == {label}


def test_has_changes_is_false_when_only_slot_contents_change() -> None:
    """
    Editing a slot's PIN is not a structural change.

    ``has_changes`` asks specifically about added/removed slots and locks;
    a PIN edit must not trigger a dashboard re-render.
    """
    old = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(pin="1111")}})
    new = _cfg({CONF_LOCKS: ["lock.a"], CONF_SLOTS: {1: _slot(pin="2222")}})

    assert EntryConfigDiff(old=old, new=new).has_changes is False


# --- build_slot_unique_id format (never referenced by any test) ---


def test_build_slot_unique_id_standard_format() -> None:
    """
    The standard unique id is entry|slot|key, pipe-delimited.

    This string is the entity registry key. Changing the separator silently
    orphans every existing entity, so the exact format is the contract --
    yet no test referenced this function at all.
    """
    assert build_slot_unique_id("abc123", 4, "pin") == "abc123|4|pin"


def test_build_slot_unique_id_per_lock_format() -> None:
    """The per-lock variant appends the lock entity id as a fourth segment."""
    assert (
        build_slot_unique_id("abc123", 4, "in_sync", "lock.front_door")
        == "abc123|4|in_sync|lock.front_door"
    )


def test_build_slot_unique_id_variants_never_collide() -> None:
    """A per-lock id is always distinct from the standard id it extends."""
    standard = build_slot_unique_id("abc123", 4, "code")
    per_lock = build_slot_unique_id("abc123", 4, "code", "lock.front_door")
    assert standard != per_lock
    assert per_lock.startswith(f"{standard}|")


def test_renaming_somebody_who_does_not_exist_is_a_no_op() -> None:
    """A rename naming an unknown user changes nothing.

    Reachable from a rename map computed against a configuration that has since
    moved on. Creating the target would add a user holding no slot.
    """
    config = EntryConfig.from_mapping({CONF_SLOTS: {1: {**_slot(), "name": "Raman"}}})

    assert config.with_user_renamed("Ghost", "Wren") is config


def test_renaming_onto_an_existing_user_is_refused() -> None:
    """A rename onto somebody else's name must not collapse two users into one.

    Re-keying without the check deletes the target: their configuration is
    gone and their slot is freed for reallocation.
    """
    config = EntryConfig.from_mapping(
        {
            CONF_SLOTS: {
                1: {**_slot("1111"), "name": "Alice"},
                2: {**_slot("2222"), "name": "Bob"},
            }
        }
    )

    assert config.with_user_field_set("Bob", "name", "Alice") is config


def test_two_stored_names_with_one_identity_keep_the_first() -> None:
    """Stored users differing only by case or padding collapse to one.

    Keeping both would put them on a single slot while the name lookups
    disagreed about which of them holds it, so a display would show one user
    and a write would land on the other.
    """
    config = EntryConfig.from_mapping(
        {
            CONF_USERS: {"Bob": {"pin": "1111"}, "bob ": {"pin": "2222"}},
            CONF_SLOT_ASSIGNMENT: {"bob": 1},
        }
    )

    assert dict(config.users) == {"Bob": {"pin": "1111"}}


@pytest.mark.parametrize("slot_num", [1, 10, 250, -1])
@pytest.mark.parametrize("lock_entity_id", [None, "lock.front_door"])
def test_slot_unique_id_round_trips(slot_num: int, lock_entity_id: str | None) -> None:
    """Both shapes of unique ID resolve to the slot they were built for.

    A per-lock entity has to land on the same slot device as the shared
    entities beside it; parsing only the shared shape would leave exactly the
    entities this moves stranded.
    """
    unique_id = build_slot_unique_id("abc123", slot_num, "in_sync", lock_entity_id)
    assert parse_slot_unique_id("abc123", unique_id) == slot_num


def test_parse_slot_unique_id_rejects_what_it_did_not_build() -> None:
    """Anything that is not this entry's slot entity resolves to nothing."""
    # Another entry's entity.
    assert parse_slot_unique_id("abc123", "def456|1|in_sync") is None
    # Not a slot number.
    assert parse_slot_unique_id("abc123", "abc123|name|in_sync") is None
    assert parse_slot_unique_id("abc123", "abc123||in_sync") is None
    # Aliases of a number that the builder would never emit.
    assert parse_slot_unique_id("abc123", "abc123|+1|in_sync") is None
    assert parse_slot_unique_id("abc123", "abc123|1_0|in_sync") is None
