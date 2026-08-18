"""
Reshape a slot-keyed config entry into a user-keyed one.

::

    slots:                      users:
      1:                 ->       Raman:
        name: Raman                 pin: "1234"
        pin: "1234"

The slot number is demoted to the ``slot_assignment`` bookkeeping that
:mod:`.slot_assignment` owns.

Pure, and separate from ``async_migrate_entry``, so what it must guarantee can
be stated over it directly: nothing lost, nobody renumbered, no retired key
left behind. The migration has no rollback, so each of those failing is
permanent for the user it happens to.

No registry writes happen here. Entity and device identifiers keep the slot
number, so there is nothing to move.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..const import CONF_NUM_SLOTS, CONF_SLOTS, CONF_START_SLOT, CONF_USERS
from .slot_assignment import CONF_SLOT_ASSIGNMENT, users_from_slots

# Keys this migration consumes. Everything else is carried through verbatim.
_CONSUMED = frozenset({CONF_SLOTS, CONF_START_SLOT, CONF_NUM_SLOTS})


def migrate_to_users(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """
    Return the user-keyed form of ``config``, and the slots whose name changed.

    ``start_slot`` and ``num_slots`` are dropped rather than translated:
    allocation takes the lowest unoccupied slot, and the number of users is
    the size of the mapping.

    Already-migrated input is returned unchanged, so running twice is a no-op
    rather than relying on the version stamp for that.
    """
    if CONF_SLOTS not in config:
        return {k: v for k, v in config.items() if k not in _CONSUMED}, []

    users, assignment, renamed = users_from_slots(config[CONF_SLOTS])
    return {
        **{k: v for k, v in config.items() if k not in _CONSUMED},
        # Keyed by the name as displayed. The configuration is hand-editable,
        # and this key is the only place a user's capitalization survives now
        # that the name is not a field. Uniqueness stays case-insensitive:
        # storage keeps the display form, comparison folds the case.
        CONF_USERS: users,
        CONF_SLOT_ASSIGNMENT: dict(assignment.slots),
    }, renamed
