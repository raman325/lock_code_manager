"""The unified credential-used bus event."""

from __future__ import annotations

from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_NAME
from homeassistant.core import HomeAssistant, callback

from ..const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_OPERATION,
    ATTR_SOURCE,
    ATTR_TARGET,
    BUS_EVENT_CREDENTIAL_USED,
)
from .credentials import CredentialType


class CredentialOperation(StrEnum):
    """
    What the device did when the credential was accepted.

    Reporting, not instruction. Lock Code Manager never actuates a lock, so
    this is never an action anybody chose -- it is the observation a lock
    passed on about what it did next, which is why the actuation vocabulary
    deliberately kept out of this integration does not belong here either.

    ``UNKNOWN`` is a first-class answer rather than a gap. A provider that
    cannot classify a notification says so (Matter, ZHA and Z-Wave JS all
    have such a path), and so does the ``use_credential`` action: the caller
    told this integration a credential was used, not what happened
    afterwards, and guessing on their behalf would put a fact in the payload
    that nobody observed.
    """

    UNLOCK = "unlock"
    LOCK = "lock"
    UNKNOWN = "unknown"

    @classmethod
    def from_to_locked(cls, to_locked: bool | None) -> CredentialOperation:
        """Read the operation off the lock-direction flag providers report."""
        if to_locked is None:
            return cls.UNKNOWN
        return cls.LOCK if to_locked else cls.UNLOCK


@callback
def async_fire_credential_used(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    *,
    name: str,
    source: str,
    target: str,
    credential_type: CredentialType,
    operation: CredentialOperation,
) -> None:
    """
    Announce that a credential belonging to ``name`` was used.

    One event for both ways a use becomes known: a lock that observed it, and
    a caller that recorded one entered somewhere this integration cannot see.
    The user is the subject, so the payload names the person rather than the
    slot number they happen to occupy -- that number is internal bookkeeping
    and putting it here would tie every consumer to it.

    All seven fields are always present and never empty. ``source`` (where
    the credential was entered) and ``target`` (what it was used against) are
    the same entity when a lock observed the use itself. Neither is
    restricted to a domain: a use can target a lock, a cover, an alarm panel,
    anything the caller associates it with. A caller with no natural entity
    for one of them is expected to supply one -- a template entity as a
    source, a Virtual lock as a target. That is the deliberate trade: a rigid
    payload every consumer can read without key tests or null checks, with
    documented escape hatches for the setups that need them. Do not add a
    default, a fallback, or an optional field here.

    ``credential_type`` is which kind of credential was presented and
    ``operation`` is what the device did with it. Both are stated rather than
    left to be inferred, which is what lets a consumer act on one kind of use
    without acting on every other -- a usage limiter that spends a budget on
    entries and not on the guest locking up behind them, say.

    Nothing dereferences ``source``: a code source's own state can be the
    cleartext credential that was typed, so it travels as a bare identifier
    and is never looked up or read. ``target`` is read for its friendly name
    -- the slot card names what a use happened against -- and for nothing
    else.
    """
    hass.bus.async_fire(
        BUS_EVENT_CREDENTIAL_USED,
        event_data={
            ATTR_NAME: name,
            ATTR_CONFIG_ENTRY_ID: config_entry.entry_id,
            ATTR_CONFIG_ENTRY_TITLE: config_entry.title,
            ATTR_SOURCE: source,
            ATTR_TARGET: target,
            ATTR_CREDENTIAL_TYPE: credential_type,
            ATTR_OPERATION: operation,
        },
    )
