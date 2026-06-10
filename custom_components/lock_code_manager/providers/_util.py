"""
Shared helpers for provider implementations.

Provider-internal utilities that don't belong in the integration-wide
``util.py``. Currently: slot-tagging used by providers whose lock APIs
identify codes by user-name rather than by slot number (Schlage, Akuvox,
and -- once the unified-user migration lands -- Matter and Z-Wave User
Credential CC).

Three tag formats coexist during the migration window:

* Canonical ``lcm:<slot>:<name>`` -- the consolidated format every new
  write uses when the lock's ``max_user_name_length`` can fit the full
  ``lcm:<slot>:`` prefix. Two colons delimit the slot field clearly for
  visual parseability on the lock UI.
* Slot-only ``<digits>`` -- the length-constrained fallback, used when
  the lock's name field is too small to fit even the canonical prefix.
  Just the slot number, written as ``str(slot)``. Ambiguous with any
  external user named with only digits, but only encountered on locks
  with absurdly small name limits, so the tradeoff is accepted.
* Legacy ``[LCM:<slot>] <name>`` -- written by Schlage/Akuvox today.
  Still emitted by ``make_legacy_tagged_name`` while those providers
  migrate; their reads tolerate it via ``parse_tag_with_rewrite``, which
  signals a legacy match so the caller can rewrite the lock-stored name
  in place at the next write opportunity. The legacy emitter is the
  deprecation marker -- ``_legacy_`` in the name is intentional so
  reviewers see at the call site that the writer hasn't migrated yet.
"""

from __future__ import annotations

import re

_LEGACY_SLOT_TAG_RE = re.compile(r"^\[LCM:(\d+)\]\s*(.*)")
_TAG_RE = re.compile(r"^lcm:(\d+):\s*(.*)")
_SLOT_ONLY_RE = re.compile(r"^(\d+)$")


def make_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Return a code name in the canonical ``lcm:<slot>:`` format."""
    base = name or f"Code Slot {slot_num}"
    return f"lcm:{slot_num}:{base}"


def make_legacy_tagged_name(slot_num: int, name: str | None = None) -> str:
    """
    Return a code name in the legacy ``[LCM:<slot>]`` format.

    Deprecated: kept for Schlage/Akuvox until they migrate to the
    canonical format with read-time detect-and-rewrite. New call sites
    should use ``make_tagged_name``.
    """
    base = name or f"Code Slot {slot_num}"
    return f"[LCM:{slot_num}] {base}"


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
    caller can re-emit via ``make_tagged_name`` on the next write to
    migrate the lock-stored name in place. The canonical
    ``lcm:<slot>:`` format, the slot-only fallback, and untagged names
    all return ``needs_rewrite=False``.

    Match priority: canonical, then legacy, then slot-only digits. The
    slot-only branch is the length-constrained encoding emitted by
    :func:`._base.BaseLock._build_tagged_user_name` when the lock's
    ``max_user_name_length`` cannot fit the canonical prefix; the
    display portion is empty. Bare digits being treated as a slot tag
    is intentional but ambiguous with external users whose names
    happen to be digit-only -- the ambiguity is the cost of preserving
    the slot binding on length-constrained locks.
    """
    match = _TAG_RE.match(name)
    if match:
        return int(match.group(1)), match.group(2), False
    match = _LEGACY_SLOT_TAG_RE.match(name)
    if match:
        return int(match.group(1)), match.group(2), True
    match = _SLOT_ONLY_RE.match(name)
    if match:
        return int(match.group(1)), "", False
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
