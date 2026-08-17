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
from typing import Any, NamedTuple

from homeassistant.const import CONF_NAME

# Reserved because it delimits every identifier Lock Code Manager builds:
# the entity unique identifier (``{entry_id}|{name}|{key}``, plus a trailing
# ``|{lock_entity_id}`` for per-lock entities) and the device identifier
# (``{entry_id}|{name}``). A name containing it would split into the wrong
# fields on the way back out -- see ``parse_user_device_identifier`` and the
# segment remapping in ``domain.identifier_migration``.
NAME_SEPARATOR = "|"


def normalize_name(name: str | None) -> str:
    """
    Return ``name`` in the form Lock Code Manager stores and compares.

    Surrounding whitespace is removed so two names that differ only by
    padding cannot both be stored. An identity that can differ invisibly is
    worse than useless -- the user would see two identical rows and Lock
    Code Manager would treat them as distinct users.
    """
    return (name or "").strip()


def name_error(name: str | None) -> str | None:
    """
    Return the validation error key for ``name``, or ``None`` when valid.

    Returns a translation key rather than a message so callers can surface
    it in the user's language.
    """
    if not normalize_name(name):
        return "name_required"
    if NAME_SEPARATOR in name:  # type: ignore[operator]
        return "name_has_separator"
    return None


def fallback_name(slot_num: int) -> str:
    """Return the name given to a slot that has none."""
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

    A name that is already valid and unique keeps its text; the only change
    it can undergo is whitespace normalization. That restraint matters
    because this runs over live user configuration, and every rewrite here
    renames that user on every lock that stores user names -- so a rewrite
    is a device write, not a config edit.

    Whitespace is the one exception worth paying for: ``"Raman "`` and
    ``"Raman"`` are indistinguishable on screen, so leaving both storable
    would let two rows that look identical be two different users.
    """
    ordered = sorted(slots, key=int)

    # Two passes, because a single pass lets a repaired name consume a later
    # slot's name and force that slot to be renamed too. With
    # {1: "Raman", 2: "Raman", 3: "Raman 2"}, one pass renames slot 2 to
    # "Raman 2" and then pushes slot 3 -- which was valid and unique all
    # along -- to "Raman 2 2". Reserving the already-good names first means
    # only the slots that actually needed repairing are touched, and each
    # avoided rewrite is an avoided rename on every lock storing user names.
    reserved: list[str] = []
    needs_repair: set[Any] = set()
    for slot_num in ordered:
        name = slots[slot_num].get(CONF_NAME)
        if name_error(name) is None and normalize_name(name).casefold() not in {
            existing.casefold() for existing in reserved
        }:
            reserved.append(normalize_name(name))
        else:
            needs_repair.add(slot_num)

    repaired: dict[Any, dict[str, Any]] = {}
    changed: list[str] = []
    taken = list(reserved)

    for slot_num in ordered:
        slot = dict(slots[slot_num])
        original = slot.get(CONF_NAME)

        if slot_num not in needs_repair:
            name = normalize_name(original)
        else:
            name = normalize_name(
                (original or "").replace(NAME_SEPARATOR, " ")
            ) or fallback_name(int(slot_num))
            name = deduplicate(name, taken)
            taken.append(name)

        if name != original:
            slot[CONF_NAME] = name
            changed.append(str(slot_num))
        repaired[slot_num] = slot

    return repaired, changed


class NameCheck(NamedTuple):
    """
    Outcome of checking one candidate name against the names already taken.

    Exactly one field is set: ``name`` is the normalized value to store, or
    ``error`` is the translation key describing why it was refused.
    """

    name: str | None
    error: str | None
    conflicting_slot: int | None = None


def check_name(
    candidate: str | None, taken: Mapping[Any, str | None] | None = None
) -> NameCheck:
    """
    Validate and normalize one candidate name against the names in use.

    The single entry point for all three write paths -- the per-slot config
    flow step, the whole-block YAML and options flows, and the name text
    entity. They previously each ran their own combination of
    ``name_error``, normalization, and a case-folded uniqueness scan, and
    the differences between those copies were real bugs: one compared a
    stripped candidate against unstripped stored names, so a padded
    duplicate was refused in one order and accepted in the other.

    ``taken`` maps slot number to that slot's current name. A caller
    renaming an existing slot omits that slot from the mapping, so a user
    can rewrite their own name without colliding with themselves.
    """
    if error := name_error(candidate):
        return NameCheck(None, error)

    name = normalize_name(candidate)
    folded = name.casefold()
    for slot_num, existing in (taken or {}).items():
        if normalize_name(existing).casefold() == folded:
            return NameCheck(None, "name_not_unique", slot_num)
    return NameCheck(name, None)


def validate_slot_names(
    slots: Mapping[Any, Mapping[str, Any]],
) -> tuple[str, str] | None:
    """
    Return the first ``(slot_num, error_key)`` problem in ``slots``, else None.

    The per-slot config flow checks one name at a time against the slots
    already accepted; the YAML and options flows submit every slot at once
    and need the whole set checked together. Returning the offending slot
    number lets the caller name it, since "one of your slots has a duplicate
    name" is not actionable.

    Walks the same :func:`check_name` every other write path uses, feeding it
    the slots already seen, so the three paths cannot drift apart in what
    they accept.
    """
    seen: dict[Any, str] = {}
    for slot_num in sorted(slots, key=int):
        checked = check_name(slots[slot_num].get(CONF_NAME), seen)
        if checked.error:
            return str(slot_num), checked.error
        seen[slot_num] = checked.name or ""
    return None
