"""
Which slot numbers allocation may use, and whether that is knowable.

On most providers the slot number IS the lock's credential index, so issuing
an occupied one writes a user's code over a credential already on the door.
Occupancy is read from the locks to avoid that.

A failed read is therefore not the same as an empty one, and is not treated as
free. But refusing on every unreadable lock would block allocation for setups
that contain one permanently, so a lock constrains the numbering only when
Lock Code Manager writes to it AND addresses it by slot number.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LockOccupancy:
    """What one lock reports about the credential indices it already holds."""

    lock_entity_id: str
    # False on providers that allocate their own credential index, where slot
    # ``n`` says nothing about credential ``n``.
    credential_index_follows_slot: bool
    # False when Lock Code Manager will not write credentials here at all --
    # an unsupported platform, or a lock it could not build a provider for.
    managed: bool
    # ``None`` means the read FAILED. An empty set means the lock answered and
    # holds nothing.
    occupied: frozenset[int] | None

    @property
    def constrains_allocation(self) -> bool:
        """Return whether this lock's contents bound the numbers we may issue."""
        return self.managed and self.credential_index_follows_slot


@dataclass(frozen=True, slots=True)
class Occupancy:
    """The numbers allocation must avoid, across every lock in an entry."""

    locks: tuple[LockOccupancy, ...]
    # Numbers another Lock Code Manager entry manages on a shared lock.
    # Nothing outside the config flow prevents two entries claiming one
    # credential index, so allocation has to.
    claimed_by_other_entries: frozenset[int]

    @property
    def is_known(self) -> bool:
        """Return whether every lock that constrains allocation could be read."""
        return not any(
            lock.occupied is None and lock.constrains_allocation for lock in self.locks
        )

    @property
    def unreadable(self) -> tuple[str, ...]:
        """Return the constraining locks that could not be read."""
        return tuple(
            lock.lock_entity_id
            for lock in self.locks
            if lock.occupied is None and lock.constrains_allocation
        )

    @property
    def unavailable(self) -> frozenset[int]:
        """
        Return the numbers allocation must not issue.

        Only counts locks that constrain allocation: reserving a number
        because an unaddressed lock happens to hold something there would push
        real users past the advertised capacity of the locks that do follow
        the slot number.
        """
        return self.claimed_by_other_entries.union(
            *(
                lock.occupied
                for lock in self.locks
                if lock.constrains_allocation and lock.occupied
            )
        )
