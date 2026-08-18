"""
Reshape a slot-keyed config entry into a user-keyed one.

This is the version 3 release's whole point::

    slots:                      users:
      1:                 ->       raman:
        name: Raman                 pin: "1234"
        pin: "1234"

with the slot number demoted to the internal ``slot_assignment`` bookkeeping
that :mod:`.slot_assignment` owns.

Kept pure, and separate from ``async_migrate_entry``, so the properties that
matter can be stated over it directly: nothing lost, nobody renumbered, no
retired key left behind. The migration has no rollback, so those are not
stylistic preferences -- each failure is permanent for the user it happens to.

**No registry writes happen here or anywhere in this migration.** Entity and
device identifiers keep the slot number, so there is nothing to move. That is
the property that makes this release safe to ship, and the reason the earlier
name-keyed identifier design was abandoned.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..const import CONF_NUM_SLOTS, CONF_SLOTS, CONF_START_SLOT, CONF_USERS
from .slot_assignment import CONF_SLOT_ASSIGNMENT, users_from_slots

# Keys this migration consumes. Everything else in the entry is carried
# through verbatim, so a key added by a later release is not silently dropped
# by an older migration running against it.
_CONSUMED = frozenset({CONF_SLOTS, CONF_START_SLOT, CONF_NUM_SLOTS})


def migrate_to_users(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """
    Return the user-keyed form of ``config``, and the slots whose name changed.

    ``start_slot`` and ``num_slots`` are dropped rather than translated. There
    is no start slot any more: allocation takes the lowest slot not already
    occupied, which needs no configuration and cannot go stale. The number of
    users is just the size of the mapping.

    Already-migrated input is returned unchanged, so running twice is a no-op.
    The version stamp should prevent a second run, but a migration with no
    rollback should not rely on that for its safety.
    """
    if CONF_SLOTS not in config:
        return {k: v for k, v in config.items() if k not in _CONSUMED}, []

    users, assignment, renamed = users_from_slots(config[CONF_SLOTS])
    return {
        **{k: v for k, v in config.items() if k not in _CONSUMED},
        # Keyed by the name AS DISPLAYED, not by the identity form. The
        # configuration is hand-editable, so someone who typed "Raman" must
        # get "Raman" back rather than a casefolded version of it -- and the
        # name is the only place that capitalization now survives, since it
        # stopped being a field.
        #
        # Uniqueness is still case-insensitive: names.deduplicate has already
        # guaranteed no two users differ only by case, and SlotAssignment
        # compares on the identity form. Storage keeps the display, lookups
        # fold the case.
        CONF_USERS: users,
        CONF_SLOT_ASSIGNMENT: dict(assignment.slots),
    }, renamed
