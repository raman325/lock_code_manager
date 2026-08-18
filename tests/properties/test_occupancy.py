"""
Property-based tests for deciding which slot numbers allocation may use.

The slot number is the lock's credential index on most providers, so issuing
an occupied one overwrites a code on a real door. Two failure directions, and
they are not symmetric:

* **issuing an occupied number** overwrites somebody's credential;
* **refusing when nothing is wrong** blocks the user from adding anybody.

The first is worse, so unreadable is treated as unknown rather than free --
but only for the locks that constrain the numbering at all.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.domain.occupancy import (
    LockOccupancy,
    Occupancy,
)

LOCKS = st.sampled_from(["lock.front", "lock.back", "lock.side"])
SLOTS = st.integers(min_value=1, max_value=12)


@st.composite
def occupancies(draw: st.DrawFn) -> Occupancy:
    """An occupancy report across a few locks, any of which may be unreadable."""
    reports = []
    for lock in draw(st.lists(LOCKS, max_size=3, unique=True)):
        follows_slot = draw(st.booleans())
        managed = draw(st.booleans())
        readable = draw(st.booleans())
        reports.append(
            LockOccupancy(
                lock_entity_id=lock,
                credential_index_follows_slot=follows_slot,
                managed=managed,
                occupied=frozenset(draw(st.sets(SLOTS, max_size=4)))
                if readable
                else None,
            )
        )
    return Occupancy(locks=tuple(reports), claimed_by_other_entries=frozenset())


@given(occupancy=occupancies())
def test_only_constraining_locks_can_block_allocation(occupancy: Occupancy) -> None:
    """A lock blocks only if Lock Code Manager writes to it by slot number.

    An unsupported platform, or one that allocates its own credential index,
    says nothing about whether slot n is free. Refusing on those would block
    allocation forever for any setup that includes one.
    """
    blocking = [
        lock
        for lock in occupancy.locks
        if lock.occupied is None and lock.managed and lock.credential_index_follows_slot
    ]

    # Stated in BOTH directions. The forward one alone is vacuous: an
    # implementation that always reports occupancy as known satisfies it by
    # never entering the branch, and that is the failure this exists to catch.
    assert occupancy.is_known == (not blocking)
    assert set(occupancy.unreadable) == {lock.lock_entity_id for lock in blocking}


@given(occupancy=occupancies())
def test_unavailable_never_includes_a_number_from_an_unconstraining_lock(
    occupancy: Occupancy,
) -> None:
    """Codes on a lock we do not address by slot must not reserve a number.

    Reserving them would push real users past the advertised capacity of the
    locks that DO follow the slot number.
    """
    unconstraining = {
        number
        for lock in occupancy.locks
        if not (lock.managed and lock.credential_index_follows_slot)
        for number in (lock.occupied or ())
    }
    constraining = {
        number
        for lock in occupancy.locks
        if lock.managed and lock.credential_index_follows_slot
        for number in (lock.occupied or ())
    }

    assert not (occupancy.unavailable - constraining) & (unconstraining - constraining)


@given(occupancy=occupancies())
def test_every_occupied_number_on_a_constraining_lock_is_unavailable(
    occupancy: Occupancy,
) -> None:
    """The direction that matters: nothing occupied is ever offered.

    Issuing one writes a user's code over a credential already on the door.
    """
    for lock in occupancy.locks:
        if lock.managed and lock.credential_index_follows_slot and lock.occupied:
            assert lock.occupied <= occupancy.unavailable


@given(occupancy=occupancies(), claimed=st.sets(SLOTS, max_size=4))
def test_numbers_another_entry_manages_are_unavailable(
    occupancy: Occupancy, claimed: set[int]
) -> None:
    """Two entries on one lock must not both manage a number.

    Nothing outside the config flow enforces this, so allocation has to.
    """
    with_claims = Occupancy(
        locks=occupancy.locks, claimed_by_other_entries=frozenset(claimed)
    )

    assert claimed <= with_claims.unavailable
