"""
Shared helpers for provider implementations.

Provider-internal utilities that don't belong in the integration-wide
``util.py``. Currently: slot-tagging used by providers whose lock APIs
identify codes by user-name rather than by slot number (Schlage, Akuvox).
"""

from __future__ import annotations

import re

# Format: ``[LCM:<slot>] <friendly name>`` — providers prepend this tag
# so we can recover which slot a code belongs to from the lock's stored
# user name.
_SLOT_TAG_RE = re.compile(r"^\[LCM:(\d+)\]\s*(.*)")


def make_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Return a code name tagged with the Lock Code Manager slot number."""
    base = name or f"Code Slot {slot_num}"
    return f"[LCM:{slot_num}] {base}"


def parse_tag(name: str) -> tuple[int | None, str]:
    """
    Parse a Lock Code Manager slot tag from a code name.

    Returns ``(slot_num, friendly_name)`` when a tag is present, or
    ``(None, original_name)`` when no tag is found.
    """
    match = _SLOT_TAG_RE.match(name)
    if match:
        return int(match.group(1)), match.group(2)
    return None, name


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
