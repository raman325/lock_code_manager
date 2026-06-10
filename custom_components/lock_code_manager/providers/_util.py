"""
Shared helpers for provider implementations.

Provider-internal utilities that don't belong in the integration-wide
``util.py``. Currently: slot-tagging used by providers whose lock APIs
identify codes by user-name rather than by slot number (Schlage, Akuvox,
and -- once the unified-user migration lands -- Matter and Z-Wave User
Credential CC).

Two tag formats coexist during the migration window:

* Legacy ``[LCM:<slot>] <name>`` -- written by Schlage/Akuvox today.
* New ``lcm<slot>:<name>`` -- the consolidated format the rest of the
  integration will move to. One character shorter, matters on locks with
  small ``max_user_name_length``.

``parse_tag_with_rewrite`` accepts both and signals when the matched name
is in the legacy format so callers can rewrite it in place at the next
write opportunity.
"""

from __future__ import annotations

import re

_LEGACY_SLOT_TAG_RE = re.compile(r"^\[LCM:(\d+)\]\s*(.*)")
_NEW_SLOT_TAG_RE = re.compile(r"^lcm(\d+):(.*)")


def make_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Return a code name in the legacy ``[LCM:<slot>]`` format."""
    base = name or f"Code Slot {slot_num}"
    return f"[LCM:{slot_num}] {base}"


def make_new_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Return a code name in the new ``lcm<slot>:`` format."""
    base = name or f"Code Slot {slot_num}"
    return f"lcm{slot_num}:{base}"


def parse_tag(name: str) -> tuple[int | None, str]:
    """
    Parse a Lock Code Manager slot tag, tolerant of both formats.

    Returns ``(slot_num, friendly_name)`` when either format matches, or
    ``(None, original_name)`` otherwise. Callers that care about
    rewriting legacy tags in place should use ``parse_tag_with_rewrite``
    instead.
    """
    slot, friendly, _ = parse_tag_with_rewrite(name)
    return slot, friendly


def parse_tag_with_rewrite(name: str) -> tuple[int | None, str, bool]:
    """
    Parse a slot tag and signal whether it needs format migration.

    Returns ``(slot_num, friendly_name, needs_rewrite)``. ``needs_rewrite``
    is True only when the legacy ``[LCM:<slot>]`` format matched; the
    caller can re-emit via ``make_new_tagged_name`` on the next write to
    migrate the lock-stored name in place. The new format and untagged
    names both return ``needs_rewrite=False``.
    """
    match = _NEW_SLOT_TAG_RE.match(name)
    if match:
        return int(match.group(1)), match.group(2), False
    match = _LEGACY_SLOT_TAG_RE.match(name)
    if match:
        return int(match.group(1)), match.group(2), True
    return None, name, False


def parse_slot_num(value: object) -> int | None:
    """
    Convert a slot identifier to an int, or return None if not convertible.

    Mirrors ``int(value)`` while collapsing the ``TypeError``/``ValueError``
    that providers otherwise catch when a lock reports a non-numeric slot key.
    Call sites remain responsible for their own logging and skip/return flow.
    """
    try:
        return int(value)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None
