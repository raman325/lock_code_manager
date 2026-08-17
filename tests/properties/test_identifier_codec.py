"""
Property-based tests for the user identifier codecs.

The sibling tag codec in ``providers/_util.py`` has had round-trip properties
since the property suite was created; the slot codecs these replaced did not,
and the asymmetry cost a real bug -- ``parse_slot_device_identifier`` gated on
``str.isdigit()``, which rejects the negative slot the builder would happily
encode, so such a device became invisible to both the orphan sweep and the
removal hook (issue #1399).

Keying on the **name** widens the input space enormously: a slot number was a
bounded integer, a name is an arbitrary user-supplied string. The single thing
holding the codec together is that ``|`` is rejected at every write path, and
that is exactly the kind of invariant an example-based test states and a
property test actually exercises.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.domain.config import (
    build_user_device_identifier,
    build_user_unique_id,
    parse_user_device_identifier,
)
from custom_components.lock_code_manager.domain.names import NAME_SEPARATOR

# Home Assistant generates entry ids as fixed-length lowercase alphanumerics.
ENTRY_IDS = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=26, max_size=26
)

# Every name the write paths can actually produce: non-empty, separator-free.
# Whitespace-only names are excluded because ``normalize_name`` rejects them
# before storage, so no identifier is ever built from one.
NAMES = st.text(min_size=1, max_size=40).filter(
    lambda name: NAME_SEPARATOR not in name and name.strip()
)

KEYS = st.sampled_from(["pin", "name", "enabled", "active", "code", "in_sync"])
LOCK_ENTITY_IDS = st.sampled_from(["lock.front_door", "lock.back_door", "lock.a"])


@given(entry_id=ENTRY_IDS, name=NAMES)
def test_device_identifier_round_trips(entry_id: str, name: str) -> None:
    """Any storable name survives a build/parse round trip unchanged."""
    identifier = build_user_device_identifier(entry_id, name)
    assert parse_user_device_identifier(entry_id, identifier) == name


@given(entry_id=ENTRY_IDS, other_entry_id=ENTRY_IDS, name=NAMES)
def test_device_identifier_rejects_a_foreign_entry(
    entry_id: str, other_entry_id: str, name: str
) -> None:
    """An identifier belonging to another entry never parses as this one's.

    This is what keeps one entry's registry sweep from deleting another
    entry's devices.
    """
    identifier = build_user_device_identifier(other_entry_id, name)
    if other_entry_id != entry_id:
        assert parse_user_device_identifier(entry_id, identifier) is None


@given(entry_id=ENTRY_IDS)
def test_bare_entry_id_is_not_a_user_device(entry_id: str) -> None:
    """The entry's own device must not look like a user's.

    It has to outlive every user -- it is what their devices hang off of.
    """
    assert parse_user_device_identifier(entry_id, entry_id) is None


@given(entry_id=ENTRY_IDS, name=NAMES, key=KEYS)
def test_unique_id_recovers_its_parts(entry_id: str, name: str, key: str) -> None:
    """The migration recovers name and key by splitting; that must be exact.

    ``_rewritten_unique_id`` and the rename path both split on the
    separator, so an identifier whose parts do not come back out in order
    would be rewritten into the wrong shape.
    """
    unique_id = build_user_unique_id(entry_id, name, key)
    parts = unique_id.split(NAME_SEPARATOR)

    assert parts == [entry_id, name, key]


@given(entry_id=ENTRY_IDS, name=NAMES, key=KEYS, lock=LOCK_ENTITY_IDS)
def test_per_lock_unique_id_recovers_its_parts(
    entry_id: str, name: str, key: str, lock: str
) -> None:
    """The per-lock variant keeps the lock in its own trailing segment."""
    unique_id = build_user_unique_id(entry_id, name, key, lock)

    assert unique_id.split(NAME_SEPARATOR) == [entry_id, name, key, lock]


@given(entry_id=ENTRY_IDS, name=NAMES, key=KEYS, lock=LOCK_ENTITY_IDS)
def test_unique_id_variants_never_collide(
    entry_id: str, name: str, key: str, lock: str
) -> None:
    """The standard and per-lock forms are always distinguishable.

    If they could collide, a per-lock entity and its entry-level sibling
    would fight over one registry row.
    """
    assert build_user_unique_id(entry_id, name, key) != build_user_unique_id(
        entry_id, name, key, lock
    )


@given(entry_id=ENTRY_IDS, name_a=NAMES, name_b=NAMES, key=KEYS)
def test_distinct_names_give_distinct_unique_ids(
    entry_id: str, name_a: str, name_b: str, key: str
) -> None:
    """Two users can never share a registry row.

    Name uniqueness is enforced case-insensitively at the write paths, but
    the codec itself must not merge two names that differ at all.
    """
    if name_a != name_b:
        assert build_user_unique_id(entry_id, name_a, key) != build_user_unique_id(
            entry_id, name_b, key
        )
