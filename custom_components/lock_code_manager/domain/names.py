"""
User-name rules for Lock Code Manager slots.

A slot's name is on its way to becoming the identity Lock Code Manager keys
on -- the config entry will store a set of named users rather than a mapping
of slot numbers, and lock users will be tagged ``lcm:{name}``. That only
works if every name is present, unique within its entry, and encodable.

This module is the single place those three rules live, so the config flow
(validating new input) and the migration (repairing existing data) cannot
drift from each other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.const import CONF_NAME

# Reserved because it delimits both the entity unique identifier
# (``{entry_id}|{name}|{key}``) and the device identifier
# (``{entry_id}|{name}``). A name containing it would split into the wrong
# fields on the way back out -- see ``parse_slot_device_identifier``.
NAME_SEPARATOR = "|"


def name_error(name: str | None) -> str | None:
    """
    Return the validation error key for ``name``, or ``None`` when valid.

    Returns a translation key rather than a message so the config flow can
    surface it in the user's language.
    """
    if name is None or not name.strip():
        return "name_required"
    if NAME_SEPARATOR in name:
        return "name_has_separator"
    return None


def fallback_name(slot_num: int) -> str:
    """
    Return the name given to a slot that has none.

    Deliberately mirrors the "Code slot N" language users already see in the
    frontend, so an auto-named slot reads as the same thing it was rather
    than as something new.
    """
    return f"User {slot_num}"


def deduplicate(name: str, taken: Iterable[str]) -> str:
    """
    Return ``name``, suffixed if needed so it does not collide with ``taken``.

    Suffixes with " 2", " 3", and so on. Comparison is case-insensitive
    because two users differing only in case would be indistinguishable in
    the frontend and would collide once slugified into an entity identifier.
    """
    lowered = {existing.casefold() for existing in taken}
    if name.casefold() not in lowered:
        return name
    suffix = 2
    while f"{name} {suffix}".casefold() in lowered:
        suffix += 1
    return f"{name} {suffix}"


def normalize_slot_names(
    slots: Mapping[Any, Mapping[str, Any]],
) -> tuple[dict[Any, dict[str, Any]], list[str]]:
    """
    Give every slot a present, separator-free, entry-unique name.

    Returns the repaired slots alongside the slot numbers that were changed,
    so a caller can report what it touched. Slots are processed in numeric
    order so the result does not depend on mapping iteration order, which
    would make the migration non-deterministic across restarts.

    Names already valid and unique are left exactly as they are: this runs
    over live user configuration, and rewriting a name the user chose would
    also rename the user on every lock that stores one.
    """
    repaired: dict[Any, dict[str, Any]] = {}
    changed: list[str] = []
    taken: list[str] = []

    for slot_num in sorted(slots, key=int):
        slot = dict(slots[slot_num])
        original = slot.get(CONF_NAME)

        name = original
        if name_error(name) == "name_required":
            name = fallback_name(int(slot_num))
        elif name is not None:
            name = name.replace(NAME_SEPARATOR, " ").strip()

        # ``name`` is non-empty by construction here, but a name consisting
        # only of separators collapses to empty after the replacement above.
        if not name:
            name = fallback_name(int(slot_num))

        name = deduplicate(name, taken)
        taken.append(name)

        if name != original:
            slot[CONF_NAME] = name
            changed.append(str(slot_num))
        repaired[slot_num] = slot

    return repaired, changed
