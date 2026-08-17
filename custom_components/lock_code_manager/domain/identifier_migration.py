"""
Move registry identifiers between key segments, in place.

Entity and device identifiers carry the user's name in their second segment.
Two operations move them: the version 4 to 5 migration, which moves slot
numbers to names, and a rename, which moves one name to another. Both are the
same problem -- remap segment 2 across a set of rows -- so both go through
``_async_remap_segment``.

Rewriting **in place** is what makes either invisible: a registry row found
by its old identifier and updated keeps its entity identifier, so every
automation, dashboard, and blueprint reference still resolves. Letting rows
go stale instead orphans them and registers duplicates alongside, which is
the one outcome this design is required to avoid.
"""

from __future__ import annotations

import logging

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import DOMAIN
from .config import build_slot_device_identifier, build_user_device_identifier
from .names import normalize_name

_LOGGER = logging.getLogger(__name__)


def _remapped(unique_id: str, entry_id: str, mapping: dict[str, str]) -> str | None:
    """
    Return ``unique_id`` with segment 2 remapped, or ``None`` to leave it alone.

    Unique identifiers are ``{entry_id}|{segment}|{key}`` with an optional
    trailing ``|{lock_entity_id}``. Splitting is unambiguous because ``|`` is
    rejected in names at every write path, entry identifiers are ULIDs, and
    lock entity identifiers use ``.``.

    ``None`` covers "not this entry's", "malformed", and "segment not in the
    mapping" -- which is also what an already-moved identifier looks like.
    """
    parts = unique_id.split("|")
    if len(parts) < 3 or parts[0] != entry_id:
        return None
    if (replacement := mapping.get(parts[1])) is None:
        return None
    return "|".join([parts[0], replacement, *parts[2:]])


def _async_remap_segment(
    hass: HomeAssistant,
    entry_id: str,
    mapping: dict[str, str],
    old_device_identifier: dict[str, str],
) -> int:
    """
    Remap segment 2 of this entry's identifiers, returning rows changed.

    ``mapping`` is old-segment to new-segment. ``old_device_identifier`` maps
    the same old segments to the device identifier they currently use, since
    the migration and a rename spell that differently.

    Repeats until a pass moves nothing, because one move can unblock another.
    Renaming ``a -> b`` and ``b -> c`` in one submission has ``a``'s target
    occupied until ``b`` moves; a single pass would strand ``a``, and its
    rows would re-register under fresh entity identifiers on the next
    restart -- the duplicate this module exists to prevent. A true cycle
    stops making progress and is reported rather than looping.

    Rows already moved this run are never moved again. Without that, a
    mapping like ``{"1": "2", "2": "Bob"}`` walks slot 1's rows to ``2`` on
    the first pass and then on to ``Bob`` on the second, handing slot 1's
    entity identifier and history to a different user.
    """
    if not mapping:
        return 0

    ent_reg = er.async_get(hass)
    moved_entity_ids: set[str] = set()
    changed = 0

    while True:
        moved_this_pass = 0
        for entity in list(er.async_entries_for_config_entry(ent_reg, entry_id)):
            if entity.entity_id in moved_entity_ids:
                continue
            new_unique_id = _remapped(entity.unique_id, entry_id, mapping)
            if new_unique_id is None or new_unique_id == entity.unique_id:
                continue
            if (
                ent_reg.async_get_entity_id(entity.domain, DOMAIN, new_unique_id)
                is not None
            ):
                continue
            ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
            moved_entity_ids.add(entity.entity_id)
            moved_this_pass += 1
        changed += moved_this_pass
        if not moved_this_pass:
            break

    # Anything still on an old identifier could not be moved at all.
    for entity in list(er.async_entries_for_config_entry(ent_reg, entry_id)):
        if entity.entity_id in moved_entity_ids:
            continue
        new_unique_id = _remapped(entity.unique_id, entry_id, mapping)
        if new_unique_id is not None and new_unique_id != entity.unique_id:
            # Left alone deliberately: the entity keeps working under its old
            # identifier, where deleting the row to free the target would take
            # the user's automations with it.
            _LOGGER.warning(
                "%s: cannot move %s to %s -- target already in use; leaving as-is",
                entry_id,
                entity.unique_id,
                new_unique_id,
            )

    dev_reg = dr.async_get(hass)
    for old_segment, new_segment in mapping.items():
        old = old_device_identifier[old_segment]
        device = dev_reg.async_get_device(identifiers={(DOMAIN, old)})
        if device is None:
            continue
        new = build_user_device_identifier(entry_id, new_segment)
        # Pre-checked because async_update_device does NOT validate identifier
        # collisions -- it just assigns. Two devices sharing an identifier
        # would corrupt every lookup that followed.
        if dev_reg.async_get_device(identifiers={(DOMAIN, new)}) is not None:
            _LOGGER.warning(
                "%s: cannot move device %s to %s -- target already exists",
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
def async_migrate_identifiers_to_names(
    hass: HomeAssistant, entry_id: str, slots: dict[int, dict[str, str]]
) -> int:
    """
    Rewrite this entry's registry identifiers from slot numbers to names.

    Returns the number of registry rows changed, for logging.

    Names are normalized, matching what ``queries.slot_name`` does at runtime.
    Version 4 configurations can hold a padded name -- the YAML path only
    started normalizing on store in this change -- and migrating to a padded
    identifier would leave every row orphaned, because the entities that
    register afterwards use the normalized form.
    """
    mapping = {
        str(slot_num): normalize_name(slot[CONF_NAME])
        for slot_num, slot in slots.items()
        if normalize_name(slot.get(CONF_NAME))
    }
    return _async_remap_segment(
        hass,
        entry_id,
        mapping,
        {old: build_slot_device_identifier(entry_id, int(old)) for old in mapping},
    )


@callback
def async_rename_identifiers(
    hass: HomeAssistant, entry_id: str, renames: dict[str, str]
) -> int:
    """
    Move users' registry identifiers from their old names to their new ones.

    Takes every rename in the update at once rather than one at a time, so a
    submission that swaps or chains names resolves instead of stranding the
    ones whose targets are momentarily occupied.

    Driven by the config entry update listener, the only place that observes
    renames from BOTH write paths -- the name text entity and the options
    flow. Entity identifiers are untouched, which is the point: renaming a
    user must not break the automations that reference them.
    """
    mapping = {
        normalize_name(old): normalize_name(new)
        for old, new in renames.items()
        if normalize_name(old) and normalize_name(new)
    }
    return _async_remap_segment(
        hass,
        entry_id,
        mapping,
        {old: build_user_device_identifier(entry_id, old) for old in mapping},
    )
