"""
Whether a lock answers requests to read its codes.

Some locks never do. A Zigbee lock that implements PIN Set without PIN Get
answers nothing, or answers every slot with a status that says it cannot
tell; a bridge may expose the code as write-only. Such a lock works -- every
write lands -- but every read of it is ten seconds of silence, and a slot
nothing answered for looks occupied, so allocation could never find it a
free number.

What is known is kept per lock, not per entry: it is a property of the lock
and its bridge, and one lock is shared by every entry that manages it. It is
learned, never configured:

- Any read that comes back with something the lock said makes it
  ``ANSWERED``, and that never changes: a lock that has answered once can
  answer, and later silence is an outage, not a limitation.
- Several silent reads in a row, from a lock never seen to answer, make it
  ``UNANSWERED``. One silence is not enough, because a lossy link drops
  replies routinely and a lock misjudged this way would have its keypad
  codes overwritten.

It is remembered in an internal section of each managing entry's data, keyed
by the lock's entity registry id, which survives renames, and in memory for
the config flow, which reads locks before its entry exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from enum import StrEnum
import logging
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from ..const import CONF_INTERNAL, DOMAIN, INTERNAL_LOCK_READS
from .config import EntryConfig
from .util import lock_display_name, per_lock_issue_id

_LOGGER = logging.getLogger(__name__)

# The repair raised while a lock does not answer reads.
UNANSWERED_ISSUE = "lock_reads_unanswered"

# How many silent reads in a row, from a lock never seen to answer, it takes
# to call it one that does not answer. Chosen against a link that loses half
# its replies (issue #1397), which a lone silence would misjudge half the time
# and five in a row one time in thirty-two. A misjudgment is undone by the
# first answer that arrives, but until then allocation may write over a code
# it could not see, so this errs towards asking more.
SILENT_READS_TO_CLASSIFY = 5

# How often a lock that does not answer is asked again, one slot, in case it
# has started to.
UNANSWERED_PROBE_INTERVAL = 3600.0

_CACHE_KEY = "lock_reads"


class ReadHealth(StrEnum):
    """What a lock has shown about answering requests to read its codes."""

    ANSWERED = "answered"
    UNANSWERED = "unanswered"


def _registry_id(hass: HomeAssistant, lock_entity_id: str) -> str | None:
    """Return the lock's entity registry id, which outlives a rename."""
    entry = er.async_get(hass).async_get(lock_entity_id)
    return entry.id if entry else None


def _cache(hass: HomeAssistant) -> dict[str, ReadHealth]:
    """Return what this run of Home Assistant has learned, by registry id."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(_CACHE_KEY, {})


def _stored(entry: ConfigEntry) -> Mapping[str, str]:
    """Return an entry's stored read health, by registry id."""
    internal = entry.data.get(CONF_INTERNAL) or {}
    return internal.get(INTERNAL_LOCK_READS) or {}


def _entries_managing(
    hass: HomeAssistant, lock_entity_id: str, *, active_only: bool = False
) -> list[ConfigEntry]:
    """
    Return every Lock Code Manager entry configured with this lock.

    A disabled entry still keeps what it knows about the lock, but it is not
    managing it now, so ``active_only`` leaves it out, and ignored ones.
    """
    return [
        entry
        for entry in hass.config_entries.async_entries(
            DOMAIN, include_ignore=not active_only, include_disabled=not active_only
        )
        if lock_entity_id in EntryConfig.from_entry(entry).locks
    ]


@callback
def read_health(hass: HomeAssistant, lock_entity_id: str) -> ReadHealth | None:
    """
    Return what is known about whether a lock answers reads, or ``None``.

    Answered anywhere wins: one entry having seen the lock answer settles it
    for all of them.
    """
    if (registry_id := _registry_id(hass, lock_entity_id)) is None:
        return None
    known = {
        ReadHealth(value)
        for value in (
            _cache(hass).get(registry_id),
            *(
                _stored(entry).get(registry_id)
                for entry in hass.config_entries.async_entries(DOMAIN)
            ),
        )
        if value in set(ReadHealth)
    }
    if ReadHealth.ANSWERED in known:
        return ReadHealth.ANSWERED
    return ReadHealth.UNANSWERED if known else None


@callback
def async_record_read_health(
    hass: HomeAssistant, lock_entity_id: str, health: ReadHealth
) -> None:
    """
    Record what a lock showed, and say so where it matters.

    ``ANSWERED`` is final, so recording ``UNANSWERED`` over it does nothing.
    Written to every entry that manages the lock, so the answer survives a
    restart whichever entry loads first.
    """
    if (registry_id := _registry_id(hass, lock_entity_id)) is None:
        return
    current = read_health(hass, lock_entity_id)
    if current is ReadHealth.ANSWERED or current is health:
        _cache(hass)[registry_id] = current or health
        return
    _cache(hass)[registry_id] = health
    for entry in _entries_managing(hass, lock_entity_id):
        _async_store(hass, entry, {**_stored(entry), registry_id: health.value})
    async_sync_read_health_issue(hass, lock_entity_id)
    _LOGGER.info(
        "%s %s requests to read its codes",
        lock_entity_id,
        "answers" if health is ReadHealth.ANSWERED else "does not answer",
    )


@callback
def async_forget_read_health(
    hass: HomeAssistant, entry: ConfigEntry, lock_entity_id: str
) -> None:
    """
    Drop what an entry remembers about a lock it no longer manages.

    Once no other entry manages it, what this run learned goes too, so a lock
    added again starts from nothing rather than from a verdict whose repair
    went with it.
    """
    if (registry_id := _registry_id(hass, lock_entity_id)) is None:
        return
    stored = _stored(entry)
    if registry_id in stored:
        _async_store(
            hass,
            entry,
            {key: value for key, value in stored.items() if key != registry_id},
        )
    if not [
        other
        for other in _entries_managing(hass, lock_entity_id)
        if other.entry_id != entry.entry_id
    ]:
        _cache(hass).pop(registry_id, None)


@callback
def async_forget_unmanaged_read_health(
    hass: HomeAssistant, lock_entity_id: str
) -> None:
    """
    Drop what this run learned about a lock no entry manages any more.

    For an entry that has been deleted, whose stored record went with it.
    """
    if (
        registry_id := _registry_id(hass, lock_entity_id)
    ) is not None and not _entries_managing(hass, lock_entity_id):
        _cache(hass).pop(registry_id, None)


@callback
def async_persist_read_health(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Bring an entry's stored read health in line with its locks.

    At setup, and whenever locks are added. The config flow reads its locks
    before the entry it creates exists, so a lock classified there is only
    in memory until now; so is one classified by a flow that was abandoned,
    then added to an entry that already existed. A record for a lock
    the entry no longer has is dropped here too: a lock removed through
    reauth may have lost its registry entry, and with it the id that
    removal would have forgotten it by.
    """
    locks = EntryConfig.from_entry(entry).locks
    registry_ids = {
        lock_entity_id: registry_id
        for lock_entity_id in locks
        if (registry_id := _registry_id(hass, lock_entity_id)) is not None
    }
    stored = {
        registry_id: value
        for registry_id, value in _stored(entry).items()
        if registry_id in registry_ids.values()
    }
    for lock_entity_id, registry_id in registry_ids.items():
        # Everything known, not only what this run learned: a lock that does
        # not answer is not read again, so after a restart what is known about
        # it is on the entries that already manage it.
        known = read_health(hass, lock_entity_id)
        if known is not None and stored.get(registry_id) != ReadHealth.ANSWERED:
            stored[registry_id] = known.value
    if stored != dict(_stored(entry)):
        _async_store(hass, entry, stored)
    for lock_entity_id in locks:
        async_sync_read_health_issue(hass, lock_entity_id)


@callback
def async_sync_read_health_issue(hass: HomeAssistant, lock_entity_id: str) -> None:
    """
    Raise the repair while a managed lock does not answer reads; clear it otherwise.

    A lock read by a config flow is not managed until its entry is set up,
    which raises the repair then; a flow that is abandoned leaves none.
    """
    issue_id = per_lock_issue_id(UNANSWERED_ISSUE, lock_entity_id)
    if read_health(
        hass, lock_entity_id
    ) is not ReadHealth.UNANSWERED or not _entries_managing(
        hass, lock_entity_id, active_only=True
    ):
        async_delete_issue(hass, DOMAIN, issue_id)
        return
    async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=IssueSeverity.WARNING,
        translation_key=UNANSWERED_ISSUE,
        translation_placeholders={
            "lock": lock_display_name(hass, lock_entity_id),
            "lock_entity_id": lock_entity_id,
        },
    )


@callback
def _async_store(
    hass: HomeAssistant, entry: ConfigEntry, lock_reads: Mapping[str, str]
) -> None:
    """
    Write an entry's read health, keeping its cached view in step.

    Other writers build the entry's data from that cached view, so a write
    made only to the entry would be undone by the next one of theirs. Only
    the internal section of the view is replaced: the rest is what the next
    update pass diffs against, and refreshing it from the entry would hide a
    change that pass has not applied yet.
    """
    internal: dict[str, Any] = dict(entry.data.get(CONF_INTERNAL) or {})
    if lock_reads:
        internal[INTERNAL_LOCK_READS] = dict(lock_reads)
    else:
        internal.pop(INTERNAL_LOCK_READS, None)
    data = {key: value for key, value in entry.data.items() if key != CONF_INTERNAL}
    if internal:
        data[CONF_INTERNAL] = internal
    hass.config_entries.async_update_entry(entry, data=data)
    if (runtime_data := getattr(entry, "runtime_data", None)) is not None:
        cached = runtime_data.config
        extra = {
            key: value for key, value in cached.extra.items() if key != CONF_INTERNAL
        }
        if internal:
            extra[CONF_INTERNAL] = internal
        runtime_data.config = replace(cached, extra=MappingProxyType(extra))
