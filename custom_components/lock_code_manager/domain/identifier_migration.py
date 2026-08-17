"""
Rewrite slot-numbered registry identifiers to user names, in place.

Entity and device identifiers used to carry the slot number; they now carry
the user's name. Rewriting them **in place** is what makes the upgrade
invisible: a registry entry found by its old unique identifier and updated
keeps its entity identifier, so every automation, dashboard, and blueprint
reference still resolves. Letting the old entries go stale instead would
orphan them and register duplicates alongside, which is the one outcome this
whole design is required to avoid.

The same rewrite runs on rename, for the same reason.
"""

from __future__ import annotations

import logging

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import DOMAIN
from .config import build_slot_device_identifier, build_user_device_identifier

_LOGGER = logging.getLogger(__name__)


def _rewritten_unique_id(
    unique_id: str, entry_id: str, mapping: dict[str, str]
) -> str | None:
    """
    Return ``unique_id`` with its slot segment replaced by a name, else ``None``.

    Unique identifiers are ``{entry_id}|{slot}|{key}`` with an optional
    trailing ``|{lock_entity_id}``. Splitting is unambiguous because a name
    cannot contain ``|`` (rejected at every write path), entry identifiers
    are ULIDs, and lock entity identifiers use ``.``.

    ``None`` means "leave alone": not this entry's, malformed, or a slot
    segment that is not a slot this entry configures -- which is also what
    an already-rewritten identifier looks like.
    """
    parts = unique_id.split("|")
    if len(parts) < 3 or parts[0] != entry_id:
        return None
    if (name := mapping.get(parts[1])) is None:
        return None
    return "|".join([parts[0], name, *parts[2:]])


@callback
def async_migrate_identifiers_to_names(
    hass: HomeAssistant, entry_id: str, slots: dict[int, dict[str, str]]
) -> int:
    """
    Rewrite this entry's registry identifiers from slot numbers to names.

    Returns the number of registry rows changed, for logging.

    Safe to run twice: an identifier whose slot segment is no longer a
    configured slot number is skipped, and after a successful pass every
    segment holds a name.

    The one input that could confuse that test is a user who names a slot
    with digits that match a *different* slot's number. Re-running would
    then rewrite that entry again. It cannot happen in practice because the
    config entry version gates this to a single run, and the rename path
    maps from the exact previous name rather than by pattern.
    """
    mapping = {
        str(slot_num): slot[CONF_NAME]
        for slot_num, slot in slots.items()
        if slot.get(CONF_NAME)
    }
    if not mapping:
        return 0

    changed = 0
    ent_reg = er.async_get(hass)
    for entity in list(er.async_entries_for_config_entry(ent_reg, entry_id)):
        new_unique_id = _rewritten_unique_id(entity.unique_id, entry_id, mapping)
        if new_unique_id is None or new_unique_id == entity.unique_id:
            continue
        try:
            ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
        except ValueError:
            # Home Assistant raises when the target identifier is already
            # taken. Leaving the old row alone is strictly better than
            # removing it: the entity keeps working under its old
            # identifier, where a delete would take the user's automations
            # with it.
            _LOGGER.warning(
                "%s: cannot rewrite %s to %s -- target already in use; leaving as-is",
                entry_id,
                entity.unique_id,
                new_unique_id,
            )
            continue
        changed += 1

    dev_reg = dr.async_get(hass)
    for slot_num, name in mapping.items():
        old = build_slot_device_identifier(entry_id, int(slot_num))
        device = dev_reg.async_get_device(identifiers={(DOMAIN, old)})
        if device is None:
            continue
        new = build_user_device_identifier(entry_id, name)
        if dev_reg.async_get_device(identifiers={(DOMAIN, new)}) is not None:
            _LOGGER.warning(
                "%s: cannot rewrite device %s to %s -- target already exists",
                entry_id,
                old,
                new,
            )
            continue
        # Replace only the matching identifier; a device may carry others.
        dev_reg.async_update_device(
            device.id,
            new_identifiers={
                (DOMAIN, new) if identifier == (DOMAIN, old) else identifier
                for identifier in device.identifiers
            },
        )
        changed += 1

    return changed


@callback
def async_rename_identifiers(
    hass: HomeAssistant, entry_id: str, old_name: str, new_name: str
) -> None:
    """
    Move a user's registry identifiers from ``old_name`` to ``new_name``.

    Driven by the config entry update listener, which is the only place that
    observes renames from BOTH write paths -- the name text entity and the
    options flow. Entity identifiers are untouched, which is the point:
    renaming a user must not break the automations that reference them.

    A collision on either registry is logged and skipped rather than raised,
    so one unmovable row cannot leave the rest half-renamed.
    """
    ent_reg = er.async_get(hass)
    old_prefix = f"{entry_id}|{old_name}|"
    new_prefix = f"{entry_id}|{new_name}|"
    for entity in list(er.async_entries_for_config_entry(ent_reg, entry_id)):
        if not entity.unique_id.startswith(old_prefix):
            continue
        new_unique_id = new_prefix + entity.unique_id.removeprefix(old_prefix)
        try:
            ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
        except ValueError:
            # Same reasoning as the migration: a taken target is skipped, not
            # resolved by deleting. Without this the loop would raise
            # part-way, leaving some rows renamed and some not.
            _LOGGER.warning(
                "%s: cannot rename %s to %s -- target already in use; leaving as-is",
                entry_id,
                entity.unique_id,
                new_unique_id,
            )

    dev_reg = dr.async_get(hass)
    old = build_user_device_identifier(entry_id, old_name)
    if device := dev_reg.async_get_device(identifiers={(DOMAIN, old)}):
        new = build_user_device_identifier(entry_id, new_name)
        # Pre-checked because async_update_device does NOT validate identifier
        # collisions -- it just assigns. Two devices sharing one identifier
        # would corrupt every async_get_device lookup that follows.
        if dev_reg.async_get_device(identifiers={(DOMAIN, new)}) is not None:
            _LOGGER.warning(
                "%s: cannot rename device %s to %s -- target already exists",
                entry_id,
                old,
                new,
            )
        else:
            dev_reg.async_update_device(
                device.id,
                new_identifiers={
                    (DOMAIN, new) if identifier == (DOMAIN, old) else identifier
                    for identifier in device.identifiers
                },
            )
