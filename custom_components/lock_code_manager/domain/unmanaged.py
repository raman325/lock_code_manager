"""
Codes on a lock that no Lock Code Manager entry accounts for.

A code can be sitting in a slot this integration does not manage for two
quite different reasons, and from the outside they are indistinguishable:
somebody programmed it at the keypad, or this integration left it there
when its slot was removed (a defect fixed in a later release, but the
codes it stranded stay stranded).

Either way it costs the slot: allocation will not issue a number it can
still read a code at, because overwriting somebody's own code is the
worse mistake. So each one is raised as its own repair and the choice is
handed to the person who knows which it is.

Swept ONCE, from the config entry migration, and never again. The point
is to settle what was already on the lock when this version arrived;
going on to report every code programmed afterwards would nag the people
who deliberately keep some codes outside this integration, and there is
no way to tell those from anybody else. The entry version is the record
that it happened -- an entry at the new version has been swept, which
needs no marker of its own and cannot drift from the entry it describes.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from ..const import CONF_LOCKS, DOMAIN
from .locks import async_create_lock_instance
from .queries import get_managed_slots

_LOGGER = logging.getLogger(__name__)

UNMANAGED_ISSUE_KEY = "unmanaged_code"


def unmanaged_issue_id(lock_entity_id: str, slot: int) -> str:
    """Build the repair issue id for one unmanaged code."""
    return f"{UNMANAGED_ISSUE_KEY}_{lock_entity_id}_{slot}"


async def async_sweep_unmanaged_codes(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """
    Raise a repair for each code on this entry's locks that nothing manages.

    Runs from the migration, so it builds throwaway providers rather than
    using the entry's own: setup has not happened yet. A lock that cannot
    be read is skipped with a warning rather than failing the migration --
    the entry has to load, and a sleeping battery lock must not stop it.
    That lock's codes go unreported, which is the accepted cost of doing
    this exactly once.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    locks = config_entry.options.get(CONF_LOCKS, config_entry.data.get(CONF_LOCKS, []))

    for lock_entity_id in locks:
        try:
            lock = async_create_lock_instance(
                hass, dev_reg, ent_reg, config_entry, lock_entity_id
            )
            credentials = await lock.async_get_usercodes()
        except Exception:
            _LOGGER.warning(
                "Could not read %s while checking for codes this integration does "
                "not manage; any it holds will not be reported",
                lock_entity_id,
                exc_info=True,
            )
            continue

        # Across every entry, not just this one: two entries sharing a lock
        # would otherwise each report the other's codes.
        managed = get_managed_slots(hass, lock_entity_id)
        unmanaged = sorted(
            slot
            for slot, credential in credentials.items()
            if slot not in managed and credential.is_present
        )
        if not unmanaged:
            continue

        _LOGGER.info(
            "Lock %s holds %s code(s) no configuration accounts for, in slot(s) %s; "
            "raising a repair for each",
            lock_entity_id,
            len(unmanaged),
            ", ".join(str(slot) for slot in unmanaged),
        )
        for slot in unmanaged:
            ir.async_create_issue(
                hass,
                DOMAIN,
                unmanaged_issue_id(lock_entity_id, slot),
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=UNMANAGED_ISSUE_KEY,
                translation_placeholders={
                    "lock": lock_entity_id,
                    "slot": str(slot),
                },
                data={"lock_entity_id": lock_entity_id, "slot": slot},
            )
