"""
Which slot numbers allocation may use, and whether that is knowable.

On most providers the slot number IS the lock's credential index, so issuing
an occupied one writes a user's code over a credential already on the door.
Occupancy is read from the locks to avoid that.

A lock that picks its own credential index (Matter) still has numbers in the
way: an untagged user holding a PIN at index ``n`` is one writing slot ``n``
adopts, so those indices are its occupied ones.

A failed read is therefore not the same as an empty one, and is not treated as
free. But refusing on every unreadable lock would block allocation for setups
that contain one permanently, so a lock constrains the numbering only when
Lock Code Manager writes to it.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LockOccupancy:
    """What one lock reports about the credential indices it already holds."""

    lock_entity_id: str
    # False when Lock Code Manager will not write credentials here at all --
    # an unsupported platform, or a lock it could not build a provider for.
    managed: bool
    # ``None`` means the read FAILED. An empty set means the lock answered and
    # holds nothing.
    occupied: frozenset[int] | None


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
        """Return whether every managed lock could be read."""
        return not self.unreadable

    @property
    def unreadable(self) -> tuple[str, ...]:
        """Return the managed locks that could not be read."""
        return tuple(
            lock.lock_entity_id
            for lock in self.locks
            if lock.occupied is None and lock.managed
        )

    @property
    def unavailable(self) -> frozenset[int]:
        """
        Return the numbers allocation must not issue.

        Only counts managed locks: reserving a number because a lock nothing
        is written to happens to hold something there would push real users
        past the advertised capacity of the locks that are written to.
        """
        return self.claimed_by_other_entries.union(
            *(lock.occupied for lock in self.locks if lock.managed and lock.occupied)
        )
