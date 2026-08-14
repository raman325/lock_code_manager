"""
Property-based tests for the slot identifier codecs.

The sibling tag codec in ``providers/_util.py`` has had round-trip properties
since the property suite was created; these codecs did not, and the asymmetry
cost a real bug. ``parse_slot_device_identifier`` gated on ``str.isdigit()``,
which rejects the negative slot the builder will happily encode, so such a
device parsed to ``None`` and became invisible to both the orphan sweep and
the removal hook (issue #1399). A round-trip property finds it in well under a
second; the example-based tests that shipped alongside the builder did not,
because nobody thinks to write down slot ``-1`` by hand.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.domain.config import (
    build_slot_device_identifier,
    build_slot_unique_id,
    parse_slot_device_identifier,
)

# Home Assistant generates entry ids as fixed-length lowercase alphanumerics.
# Fixed length matters to the codec: it is what makes one entry id incapable
# of being a prefix of another.
ENTRY_IDS = st.text(
    alphabet="0123456789abcdefghijklmnopqrstuvwxyz", min_size=26, max_size=26
)

# Deliberately unbounded, including negative and zero. The slots YAML schema
# puts no lower bound on the key, so the codec must survive whatever a user
# can configure -- constraining this strategy to "sensible" slots would
# reproduce exactly the blind spot that let the bug through.
SLOT_NUMS = st.integers()

KEYS = st.text(min_size=1, max_size=20).filter(lambda s: "|" not in s)


@given(entry_id=ENTRY_IDS, slot=SLOT_NUMS)
def test_device_identifier_round_trips(entry_id: str, slot: int) -> None:
    """Anything the builder encodes, the parser recovers unchanged."""
    identifier = build_slot_device_identifier(entry_id, slot)
    assert parse_slot_device_identifier(entry_id, identifier) == slot


@given(entry_id=ENTRY_IDS)
def test_entry_device_is_not_a_slot_device(entry_id: str) -> None:
    """
    The entry's own device carries a bare entry id and must not parse.

    The removal hook distinguishes the two by exactly this: a ``None`` here
    means "the hub device", which must outlive every slot hanging off it.
    """
    assert parse_slot_device_identifier(entry_id, entry_id) is None


@given(entry_id=ENTRY_IDS, other_id=ENTRY_IDS, slot=SLOT_NUMS)
def test_other_entrys_device_never_parses(
    entry_id: str, other_id: str, slot: int
) -> None:
    """A device belonging to a different entry is never claimed as ours."""
    if entry_id == other_id:
        return
    foreign = build_slot_device_identifier(other_id, slot)
    assert parse_slot_device_identifier(entry_id, foreign) is None


@given(entry_id=ENTRY_IDS, identifier=st.text(max_size=60))
def test_parse_is_total(entry_id: str, identifier: str) -> None:
    """
    The parser never raises, whatever it is handed.

    It runs over every device already in the registry, including ones written
    by older versions and by other integrations, so a crash here would take
    out setup for the whole entry.
    """
    result = parse_slot_device_identifier(entry_id, identifier)
    assert result is None or isinstance(result, int)


@given(entry_id=ENTRY_IDS, slot=SLOT_NUMS)
def test_parse_accepts_only_the_builders_own_spelling(entry_id: str, slot: int) -> None:
    """
    Int-parseable spellings the builder never emits are rejected.

    ``int()`` happily accepts ``+1``, ``1_0``, ``01`` and surrounding
    whitespace. Admitting those would map several distinct identifiers onto
    one slot, so two registry devices could both claim to be slot 1.
    """
    canonical = build_slot_device_identifier(entry_id, slot)
    for alias in (f"+{slot}", f"0{slot}", f" {slot}", f"{slot} "):
        candidate = f"{entry_id}|{alias}"
        if candidate == canonical:
            continue
        assert parse_slot_device_identifier(entry_id, candidate) is None


@given(entry_id=ENTRY_IDS, slot=SLOT_NUMS, key=KEYS)
def test_slot_unique_id_is_injective(entry_id: str, slot: int, key: str) -> None:
    """
    Distinct (slot, key) pairs never collide into one unique id.

    A collision would make two entities fight over one registry row. The
    per-lock variant must also stay distinct from the standard one, which is
    what the trailing lock entity id buys.
    """
    standard = build_slot_unique_id(entry_id, slot, key)
    assert standard == f"{entry_id}|{slot}|{key}"

    per_lock = build_slot_unique_id(entry_id, slot, key, "lock.front_door")
    assert per_lock == f"{standard}|lock.front_door"
    assert per_lock != standard
