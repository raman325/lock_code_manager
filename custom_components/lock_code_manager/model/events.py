"""The unified credential-used bus event."""

from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DOMAIN,
    ATTR_ENTITY_ID,
    ATTR_NAME,
    EVENT_LOGBOOK_ENTRY,
)
from homeassistant.core import Context, HomeAssistant, callback

from ..const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_OPERATION,
    ATTR_SOURCE,
    ATTR_TARGET,
    BUS_EVENT_CREDENTIAL_USED,
    DOMAIN,
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
    cannot classify a notification says so (Matter and Z-Wave JS both
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


# The logbook entry's message key, which only the logbook's own constants name.
_LOGBOOK_MESSAGE = "message"

# How a logbook entry names each kind: "Unlocked by Alice with a PIN".
_CREDENTIAL_PHRASES: dict[CredentialType, str] = {
    CredentialType.PIN: "a PIN",
    CredentialType.RFID: "an RFID tag",
    CredentialType.FINGERPRINT: "a fingerprint",
    CredentialType.FACE: "face recognition",
    CredentialType.PASSWORD: "a password",
    CredentialType.NFC: "an NFC tag",
}

# What the device did, for a message that leads with it. ``UNKNOWN`` is
# deliberately absent: the message then says only that the credential was used.
_OPERATION_PHRASES: dict[CredentialOperation, str] = {
    CredentialOperation.UNLOCK: "Unlocked",
    CredentialOperation.LOCK: "Locked",
}


def credential_use_message(
    name: str, credential_type: CredentialType, operation: CredentialOperation
) -> str:
    """
    Describe a use for the logbook, naming the person in the message itself.

    The entry's own ``name`` never reaches the screen: for an entry with an
    ``entity_id`` the frontend shows the entity's name in its place, and the
    entity's own activity view shows the message alone. So the message has
    to say who, and stand on its own after the target's name.

    An unknown operation is left unsaid rather than guessed, the same
    discipline as the payload.
    """
    credential = _CREDENTIAL_PHRASES[credential_type]
    if (done := _OPERATION_PHRASES.get(operation)) is None:
        return f"{name} used {credential}"
    return f"{done} by {name} with {credential}"


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
    context: Context | None,
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

    Every use is also written to the logbook under ``target``, so it shows in
    that entity's activity alongside the state change it explains.

    ``context`` is whatever reported the use -- the action call, for a use
    reported through ``use_credential`` -- and both the event and the logbook
    entry carry it, so the logbook names that automation or person as the
    cause. It is required so that every caller decides: a lock that observed
    a use passes ``None``, because the lock's own state change has the lock
    integration's context and nothing of ours caused it.
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
        context=context,
    )
    # Filed on the target as a manual logbook entry, because the logbook
    # shows an entity only its own integration's described events and Lock
    # Code Manager owns none of the targets. A described event of ours would
    # reach the unfiltered Logbook alone, never the lock's own activity.
    #
    # Fired directly rather than through ``logbook.async_log_entry``, which
    # fires exactly this: importing the logbook would pull its recorder and
    # frontend modules into every provider, and depending on it would hold
    # this integration's setup behind the recorder's. The recorder stores the
    # event whether or not the logbook is loaded yet.
    hass.bus.async_fire(
        EVENT_LOGBOOK_ENTRY,
        {
            ATTR_NAME: name,
            _LOGBOOK_MESSAGE: credential_use_message(name, credential_type, operation),
            ATTR_DOMAIN: DOMAIN,
            ATTR_ENTITY_ID: target,
        },
        context=context,
    )
