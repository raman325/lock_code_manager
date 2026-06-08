"""
Cross-platform User and Credential domain model.

A first-class internal abstraction over the per-platform credential
vocabularies (the Z-Wave User Credential Command Class and the Matter
DoorLock cluster). The model is shaped so the current one-managed-slot to
one-user to one-Personal-Identification-Number-credential projection is a
thin pure function, while a future "user is the unit, multiple credentials"
world only needs to append to ``User.credentials`` -- no field changes.

These are pure, immutable value types. They do not import Home Assistant and
do not change ``SlotCredential``, which remains the coordinator's currency.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from .models import SlotCredential


class CredentialType(StrEnum):
    """
    Kind of credential a user presents to a lock.

    Only ``PIN`` is exercised today. The remaining members are reserved so a
    future expansion to multiple credential kinds is additive: they map
    cleanly to both the Z-Wave User Credential Command Class and the Matter
    DoorLock cluster credential-type vocabularies. ``PIN`` deliberately
    serializes to the lowercase wire value the providers already send.
    """

    PIN = "pin"
    RFID = "rfid"  # Radio Frequency Identification tag/card.
    FINGERPRINT = "fingerprint"
    FACE = "face"
    PASSWORD = "password"
    NFC = "nfc"  # Near Field Communication tag.


class UserType(StrEnum):
    """
    How a user is constrained on the lock.

    Modeled minimally for now. ``UNRESTRICTED`` matches the Matter and Z-Wave
    "normal" user with no schedule restrictions, which is the only kind Lock
    Code Manager manages today. Additional kinds (for example schedule- or
    duress-restricted users) can be appended without breaking callers.
    """

    UNRESTRICTED = "unrestricted"


class CredentialRule(StrEnum):
    """
    How many credentials a user must present to operate the lock.

    Modeled minimally for now. ``SINGLE`` means one credential is sufficient,
    matching today's one-PIN-per-user reality. Multi-credential rules can be
    appended later without breaking callers.
    """

    SINGLE = "single"


# CredentialState is the read-state of a credential value: known(value),
# unreadable (write-only code present), or empty. It deliberately reuses
# SlotCredential's existing semantics rather than duplicating them, so the
# coordinator's currency and the new model never drift. Construct states via
# SlotCredential.known(value) / .unreadable() / .empty().
CredentialState = SlotCredential


@dataclass(frozen=True, slots=True)
class Credential:
    """
    One credential instance addressed within a user.

    Pairs the credential ``type`` and its lock ``slot`` index with a reused
    ``CredentialState`` (a ``SlotCredential``). The read-state accessors
    delegate to that state so there is one source of truth for empty /
    write-only / readable. Treat as an immutable value; consume via the
    accessors rather than reaching into ``state``.
    """

    type: CredentialType
    slot: int
    state: CredentialState

    @property
    def is_empty(self) -> bool:
        """Return True when the credential's slot holds no code."""
        return self.state.is_empty

    @property
    def is_present(self) -> bool:
        """Return True when the credential's slot holds a code."""
        return self.state.is_present

    @property
    def is_readable(self) -> bool:
        """Return True when the credential exposes a comparable value."""
        return self.state.is_readable

    @property
    def readable_pin(self) -> str | None:
        """Return the value when readable, otherwise ``None``."""
        return self.state.readable_pin

    def matches(self, pin: str) -> bool:
        """Return True when this credential is readable and equals ``pin``."""
        return self.state.matches(pin)


class CredentialRef(NamedTuple):
    """
    Stable address for a single credential.

    A lightweight ``(user_id, type, slot)`` tuple used to point at a
    credential without carrying its state. Hashable, so it works as a
    dictionary key or set member when correlating credentials across reads.
    """

    user_id: int
    type: CredentialType
    slot: int
