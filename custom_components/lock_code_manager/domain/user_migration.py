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

from homeassistant.const import CONF_CONDITION, CONF_ENTITY_ID

from ..const import (
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    CONF_USERS,
)
from .slot_assignment import CONF_SLOT_ASSIGNMENT, users_from_slots

# Keys this migration consumes. Everything else is carried through verbatim.
_CONSUMED = frozenset({CONF_SLOTS, CONF_START_SLOT, CONF_NUM_SLOTS})


def migrate_to_users(
    config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """
    Return the user-keyed form of ``config``, and what it had to touch.

    Reports the slots whose name changed, and the empty ones dropped: a slot
    with neither a name nor a PIN was never a user, and carrying it across
    would invent one.

    ``start_slot`` and ``num_slots`` are dropped rather than translated:
    allocation takes the lowest unoccupied slot, and the number of users is
    the size of the mapping.

    Already-migrated input is returned unchanged, so running twice is a no-op
    rather than relying on the version stamp for that.
    """
    if CONF_SLOTS not in config:
        carried = {k: v for k, v in config.items() if k not in _CONSUMED}
        if CONF_USERS in carried:
            carried[CONF_USERS] = _condition_renamed(carried[CONF_USERS])
        return carried, [], []

    users, assignment, renamed, dropped = users_from_slots(config[CONF_SLOTS])
    users = _condition_renamed(users)
    return (
        {
            **{k: v for k, v in config.items() if k not in _CONSUMED},
            # Keyed by the name as displayed. The configuration is hand-editable,
            # and this key is the only place a user's capitalization survives now
            # that the name is not a field. Uniqueness stays case-insensitive:
            # storage keeps the display form, comparison folds the case.
            CONF_USERS: users,
            CONF_SLOT_ASSIGNMENT: dict(assignment.slots),
        },
        renamed,
        dropped,
    )


def _condition_renamed(users: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """
    Rename each user's ``entity_id`` field to ``condition``.

    The field names the entity whose state gates the user's credential, which
    ``entity_id`` said nothing about while colliding with every other entity
    id in the configuration. Applied to already-converted users too, so an
    entry part-way through this release's changes lands in the same shape as
    one coming straight from version 3.
    """
    return {
        name: (
            {
                **{k: v for k, v in fields.items() if k != CONF_ENTITY_ID},
                CONF_CONDITION: fields[CONF_ENTITY_ID],
            }
            if CONF_ENTITY_ID in fields and CONF_CONDITION not in fields
            else {k: v for k, v in fields.items() if k != CONF_ENTITY_ID}
        )
        for name, fields in users.items()
    }
