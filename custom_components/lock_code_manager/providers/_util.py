"""
Shared helpers for provider implementations.

Provider-internal utilities that don't belong in the integration-wide
``util.py``. Currently: slot-tagging used by providers whose lock APIs
identify codes by user-name rather than by slot number (Schlage, Akuvox,
and -- once the unified-user migration lands -- Matter and Z-Wave User
Credential CC).

Three formats are emitted today; a fourth (legacy) is still tolerated on
read for locks whose names were written by older releases.

* Canonical ``lcm:<slot>:<name>`` -- the consolidated format every new
  write uses when the lock's ``max_user_name_length`` can fit the full
  ``lcm:<slot>:`` prefix. Two colons delimit the slot field clearly for
  visual parseability on the lock UI.
* Compact ``lcm<slot>`` -- the charset-constrained fallback, used when
  the lock's firmware rejects the colons (Matter spec is permissive but
  many firmwares restrict ``userName`` to alphanumeric). Drops the
  display portion but keeps the LCM ownership marker, so the lock-side
  name is unambiguous about who owns the user record. Strictly better
  than the slot-only fallback for coexistence with other controllers,
  which is why providers try this tier first before falling all the way
  back to ``str(slot)``.
* Slot-only ``<digits>`` -- the deepest fallback, used when neither
  canonical nor compact fit. Just the slot number, written as
  ``str(slot)``. Ambiguous with any external user named with only
  digits, but only encountered on locks with absurdly small name limits
  or firmwares that reject even ``lcm<slot>``, so the tradeoff is
  accepted.
* Legacy ``[LCM:<slot>] <name>`` -- read-only. Older Schlage/Akuvox
  releases wrote this format; ``parse_tag`` still recognizes it so the
  integration can identify its own users on locks that haven't been
  rewritten yet. The next write naturally replaces the lock-stored name
  with the canonical format.
"""

from __future__ import annotations

import re

_LEGACY_SLOT_TAG_RE = re.compile(r"^\[LCM:(\d+)\]\s*(.*)")
_TAG_RE = re.compile(r"^lcm:(\d+):\s*(.*)")
_COMPACT_TAG_RE = re.compile(r"^lcm(\d+)$")
_SLOT_ONLY_RE = re.compile(r"^(\d+)$")


def make_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Return a code name in the canonical ``lcm:<slot>:`` format."""
    base = name or f"Code Slot {slot_num}"
    return f"lcm:{slot_num}:{base}"


def make_compact_tagged_name(slot_num: int) -> str:
    """
    Return a code name in the compact ``lcm<slot>`` format.

    Charset-safe (alphanumeric only) fallback used when a lock's
    firmware rejects the colons in the canonical format. Drops the
    display portion but preserves the LCM ownership marker so the
    on-lock name doesn't collide with externally-created users in the
    way that the bare slot-only fallback would.
    """
    return f"lcm{slot_num}"


def parse_tag(name: str) -> tuple[int | None, str]:
    """
    Parse a Lock Code Manager slot tag, tolerant of all known formats.

    Returns ``(slot_num, friendly_name)`` when any format matches, or
    ``(None, original_name)`` otherwise. Match priority: canonical,
    legacy, compact, slot-only. The compact and slot-only branches are
    charset-/length-constrained fallbacks emitted by Matter (and
    eventually other providers) when the lock rejects the canonical
    name; their display portion is empty because the name only carries
    the slot binding. The legacy ``[LCM:<slot>]`` format is read-only
    -- nothing emits it anymore -- but is still recognized so older
    lock-stored names continue to identify as LCM-owned until the next
    write rewrites them in the canonical format. Bare digits being
    treated as a slot tag is intentional but ambiguous with external
    users whose names happen to be digit-only; the ambiguity is the
    cost of preserving the slot binding on constrained locks.
    """
    if match := _TAG_RE.match(name):
        return int(match.group(1)), match.group(2)
    if match := _LEGACY_SLOT_TAG_RE.match(name):
        return int(match.group(1)), match.group(2)
    if match := _COMPACT_TAG_RE.match(name):
        return int(match.group(1)), ""
    if match := _SLOT_ONLY_RE.match(name):
        return int(match.group(1)), ""
    return None, name


def parse_slot_num(value: object) -> int | None:
    """
    Convert a slot identifier to an int, or return None if not convertible.

    Mirrors ``int(value)`` while collapsing the ``TypeError``/``ValueError``
    that providers otherwise catch when a lock reports a non-numeric slot key.
    JSON booleans are rejected rather than coerced (``int(True)`` is 1, so a
    malformed ``true`` would otherwise silently address slot 1).
    Call sites remain responsible for their own logging and skip/return flow.
    """
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None
