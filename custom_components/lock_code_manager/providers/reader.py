"""Module for credential reader "locks" (keypads and similar devices)."""

from __future__ import annotations

from dataclasses import dataclass

from .virtual import VirtualLock


@dataclass(repr=False, eq=False)
class ReaderLock(VirtualLock):
    """
    A credential reader anchored on a state-bearing entity.

    The anchor entity's state is the last-submitted credential; each
    non-empty value is validated against the entry's users. Slot storage
    reuses the virtual provider's Store so the sync machinery treats the
    reader like any slot-only lock.
    """

    @property
    def domain(self) -> str:
        """Return the anchor entity's integration domain."""
        return self.lock.platform
