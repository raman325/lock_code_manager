"""A slot's status, from what it wants, what was written and what the lock shows."""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.core.slot import (
    CLEARED,
    Holds,
    Repair,
    Seen,
    Slot,
    Status,
    fingerprint,
)

from .test_worker import SALT

# In sync -------------------------------------------------------------------------

PINS = st.text(alphabet="0123456789", min_size=4, max_size=8)
WROTE = st.one_of(st.none(), st.just(CLEARED), PINS.map(lambda p: fingerprint(p, SALT)))
HIDDEN = st.sampled_from([Seen.MASKED, Seen.SILENT])


def _slot(want: str | None, wrote: str | None, seen) -> Slot:
    slot = Slot(want)
    slot.wrote = wrote
    slot.seen = seen
    return slot


@given(want=PINS, shown=PINS, wrote=WROTE, same=st.booleans())
def test_a_code_the_lock_shows_is_in_sync_exactly_when_it_is_the_pin(
    want: str, shown: str, wrote: str | None, same: bool
) -> None:
    """Whatever was written, even the PIN itself, a code the lock shows decides."""
    if same:
        wrote = fingerprint(want, SALT)
    status = _slot(want, wrote, Holds(shown)).status(SALT)
    assert status is (Status.IN_SYNC if shown == want else Status.OUT_OF_SYNC)


@given(want=PINS, wrote=WROTE, same=st.booleans())
def test_an_empty_slot_is_never_in_sync_with_a_pin(
    want: str, wrote: str | None, same: bool
) -> None:
    """The lock says empty: out of sync, even right after writing the PIN (#1538)."""
    if same:
        wrote = fingerprint(want, SALT)
    assert _slot(want, wrote, Seen.EMPTY).status(SALT) is Status.OUT_OF_SYNC


@given(want=PINS, wrote=WROTE, seen=HIDDEN, same=st.booleans())
def test_a_hidden_code_is_in_sync_exactly_when_the_pin_was_written(
    want: str, wrote: str | None, seen: Seen, same: bool
) -> None:
    """A lock that will not show or say: the last write is the only evidence."""
    if same:
        wrote = fingerprint(want, SALT)
    expected = wrote == fingerprint(want, SALT)
    status = _slot(want, wrote, seen).status(SALT)
    assert status is (Status.IN_SYNC if expected else Status.OUT_OF_SYNC)


@given(
    wrote=WROTE,
    seen=st.one_of(
        st.sampled_from([Seen.EMPTY, Seen.MASKED, Seen.SILENT, Seen.DISABLED]),
        PINS.map(Holds),
    ),
)
def test_a_slot_wanting_nothing_is_in_sync_once_no_code_remains(
    wrote: str | None, seen
) -> None:
    """
    Empty is in sync; a code it shows (or one not in force) never is; one it
    hides is in sync only once a clear was the last write (#1437).
    """
    if seen is Seen.EMPTY:
        expected = True
    elif seen in (Seen.MASKED, Seen.SILENT):
        expected = wrote == CLEARED
    else:
        expected = False
    status = _slot(None, wrote, seen).status(SALT)
    assert status is (Status.IN_SYNC if expected else Status.OUT_OF_SYNC)


@given(want=st.one_of(st.none(), PINS), wrote=WROTE)
def test_a_slot_never_read_is_unknown_and_a_held_one_suspended(
    want: str | None, wrote: str | None
) -> None:
    """Nothing is claimed before the lock has spoken; a hold outranks everything."""
    assert _slot(want, wrote, None).status(SALT) is Status.UNKNOWN
    held = _slot(want, wrote, Seen.EMPTY)
    held.hold = Repair.CANT_SET
    assert held.status(SALT) is Status.SUSPENDED
