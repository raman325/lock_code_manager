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

from enum import StrEnum


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
