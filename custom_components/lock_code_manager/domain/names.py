"""
User-name rules for Lock Code Manager slots.

The name is the identity the configuration is keyed on, so it has to be
present and unique within its entry.

This module is the single place those rules live, so the config flow
(validating new input) and the migration (repairing existing data) cannot
drift from each other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.const import CONF_NAME


def normalize_name(name: str | None) -> str:
    """
    Return ``name`` in the form Lock Code Manager stores and compares.

    Surrounding whitespace is removed so two names that differ only by
    padding cannot both be stored. An identity that can differ invisibly is
    worse than useless -- the user would see two identical rows and Lock
    Code Manager would treat them as distinct users.
    """
    return (name or "").strip()


def identity(name: str | None) -> str:
    """
    Return the form a user is recognized by.

    Whitespace normalized and casefolded, so ``Bob`` and ``BOB `` are one
    person everywhere. Comparing any other way lets the same user hold two
    credential indices, and makes a case-only rename read as a deletion plus
    an addition.
    """
    return normalize_name(name).casefold()


def name_error(name: str | None) -> str | None:
    """
    Return the validation error key for ``name``, or ``None`` when valid.

    Returns a translation key rather than a message so callers can surface
    it in the user's language.
    """
    if not normalize_name(name):
        return "name_required"
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
    lowered = {identity(existing) for existing in taken}
    if identity(name) not in lowered:
        return name
    suffix = 2
    while identity(f"{name} {suffix}") in lowered:
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
        if name_error(name) is None and identity(name) not in {
            identity(existing) for existing in reserved
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
            name = normalize_name(original) or fallback_name(int(slot_num))
            name = deduplicate(name, taken)
            taken.append(name)

        if name != original:
            slot[CONF_NAME] = name
            changed.append(str(slot_num))
        repaired[slot_num] = slot

    return repaired, changed


def validate_slot_names(
    slots: Mapping[Any, Mapping[str, Any]],
) -> tuple[str, str] | None:
    """
    Return the first ``(slot_num, error_key)`` problem in ``slots``, else None.

    The single-slot config flow validates one name at a time against the
    slots already accepted, but the YAML and options flows submit every slot
    at once and need the whole set checked together. Returning the offending
    slot number lets the caller name it in the error, since "one of your
    slots has a duplicate name" is not actionable.

    Uniqueness is compared on the normalized, case-folded name, matching how
    the migration deduplicates -- otherwise a pair the migration would repair
    could be re-entered by hand.
    """
    seen: dict[str, str] = {}
    for slot_num in sorted(slots, key=int):
        name = slots[slot_num].get(CONF_NAME)
        if error := name_error(name):
            return str(slot_num), error
        key = identity(name)
        if key in seen:
            return str(slot_num), "name_not_unique"
        seen[key] = str(slot_num)
    return None


def validate_user_names(users: Mapping[str, Any]) -> tuple[str, str] | None:
    """
    Return the first ``(name, error_key)`` problem in ``users``, else None.

    The editor submits users keyed by name, so a duplicate key cannot reach
    here -- but two keys can still mean one user (``Bob`` and ``bob ``), and
    that pair would collapse into a single user on the way into storage,
    silently taking one of their credentials with it.

    Returns the offending name so the caller can say which one, since "one of
    your users has a duplicate name" is not actionable.
    """
    seen: dict[str, str] = {}
    for name in users:
        if error := name_error(name):
            return name, error
        key = identity(name)
        if key in seen:
            return name, "name_not_unique"
        seen[key] = name
    return None
