"""
Cross-platform User and Credential domain model.

A first-class internal abstraction over the per-platform credential
vocabularies (the Z-Wave User Credential Command Class and the Matter
DoorLock cluster). The model is shaped so the current one-managed-slot to
one-user to one-Personal-Identification-Number-credential projection is a
thin pure function, while a future "user is the unit, multiple credentials"
world only needs to append to ``User.credentials`` -- no field changes.

These are pure value types with no Home Assistant dependencies. The credential
and capability types are immutable; ``User`` is a mutable aggregate of immutable
credentials. They do not change ``SlotCredential``, which remains the
coordinator's currency.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
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


@dataclass(slots=True)
class User:
    """
    A lock user and the credentials they present.

    Modeled so the future "user is the unit, multiple credentials" world is
    additive: today every user carries a single Personal Identification
    Number credential, but ``credentials`` is already a list and the rule and
    type fields already exist. ``credentials`` is mutable (a list), so this
    aggregate is intentionally not frozen; the contained ``Credential``
    values are themselves immutable.
    """

    user_id: int
    name: str | None = None
    user_type: UserType = UserType.UNRESTRICTED
    active: bool = True
    credential_rule: CredentialRule = CredentialRule.SINGLE
    credentials: list[Credential] = field(default_factory=list)

    def credentials_of_type(self, credential_type: CredentialType) -> list[Credential]:
        """
        Return this user's credentials of ``credential_type``.

        Generic per-type accessor used by the base orchestration to project
        users into a slot-shaped view for a given credential kind. Providers
        store every type they can map (see each provider's ``async_get_users``);
        this accessor is the seam where the integration narrows that store to
        the type the caller cares about, which today is always Personal
        Identification Number but is wired through here so that adding a
        second supported type (Z-Wave User Credential CC also exposes
        ``PASSWORD``) is a caller-side change, not a provider-side change.
        """
        return [
            credential
            for credential in self.credentials
            if credential.type is credential_type
        ]

    @property
    def pin_credentials(self) -> list[Credential]:
        """
        Return this user's Personal Identification Number credentials.

        Thin alias for ``credentials_of_type(CredentialType.PIN)``; kept as
        a property because most call sites are written against the PIN-only
        world and read cleanly that way.
        """
        return self.credentials_of_type(CredentialType.PIN)

    def credential_for(self, credential_type: CredentialType) -> Credential | None:
        """Return the first credential of ``credential_type``, else ``None``."""
        return next(iter(self.credentials_of_type(credential_type)), None)


class WriteResult(StrEnum):
    """
    Outcome of a credential write (``async_set_credential``).

    - ``NO_CHANGE`` -- the value was already set; nothing was written. The
      coordinator is not refreshed.
    - ``CONFIRMED`` -- the lock acknowledged the write. The slot is marked
      verified; non-push providers refresh to read it back.
    - ``OPTIMISTIC`` -- the write returned an ambiguous result we treat as
      completed but have NOT confirmed (e.g. a Z-Wave driver
      ``ERROR_UNKNOWN`` from a masked read-back). The slot is marked
      unverified and awaits confirmation via a push event or hard refresh;
      if none arrives, it re-syncs rather than silently reporting success.
      See the Phase 2 push-as-commit spec.
    """

    NO_CHANGE = "no_change"
    CONFIRMED = "confirmed"
    OPTIMISTIC = "optimistic"

    @property
    def changed(self) -> bool:
        """Return whether a write actually occurred (CONFIRMED or OPTIMISTIC)."""
        return self is not WriteResult.NO_CHANGE


@dataclass(frozen=True, slots=True)
class SetUserResult:
    """
    Outcome of creating or updating a lock user.

    ``user_id`` is the resolved identifier, which the integration may allocate
    rather than echo back. ``created`` is True when the call added a new user
    and False when it updated an existing one. The base orchestration uses
    ``created`` to roll back -- delete the user -- only when a newly created
    user would otherwise be left with no credential by a failed credential
    write, preserving the invariant that a user exists if and only if it owns
    at least one credential.
    """

    user_id: int
    created: bool


@dataclass(frozen=True, slots=True)
class CredentialTypeCapability:
    """
    Per-credential-type limits advertised by a lock.

    ``num_slots`` is the number of slots the lock exposes for this credential
    type, ``min_length`` / ``max_length`` bound an acceptable value, and
    ``supports_learn`` is True when the lock can enroll the credential at the
    device (for example a fingerprint learn flow) rather than being told the
    value.
    """

    num_slots: int
    min_length: int
    max_length: int
    supports_learn: bool


@dataclass(frozen=True, slots=True)
class LockCapabilities:
    """
    What a lock can do, as a platform-neutral snapshot.

    ``supports_user_management`` mirrors the providers' existing gate;
    ``max_users`` is the total number of users the lock can hold; and
    ``credential_types`` maps each supported ``CredentialType`` to its
    per-type limits. A type absent from the mapping is unsupported.
    """

    supports_user_management: bool
    max_users: int
    credential_types: Mapping[CredentialType, CredentialTypeCapability]
    # Maximum user-name length the lock will store; 0 means the lock has
    # no concept of named users (e.g. Z-Wave User Code CC) and the user
    # IS the credential. The base orchestration skips the separate user
    # write when ``supports_user_management`` is False OR this is 0.
    max_user_name_length: int = 0

    def __post_init__(self) -> None:
        """Snapshot credential_types so the value object cannot be mutated later."""
        object.__setattr__(
            self,
            "credential_types",
            MappingProxyType(dict(self.credential_types)),
        )

    def capability_for(
        self, credential_type: CredentialType
    ) -> CredentialTypeCapability | None:
        """Return the per-type limits for ``credential_type``, else ``None``."""
        return self.credential_types.get(credential_type)

    def supports(self, credential_type: CredentialType) -> bool:
        """Return True when the lock advertises ``credential_type``."""
        return credential_type in self.credential_types

    def length_bounds(
        self, credential_type: CredentialType
    ) -> tuple[int, int | None] | None:
        """
        Return the effective ``(min, max)`` value length for a credential type.

        ``None`` when the type is unsupported. A non-positive advertised
        bound means "unbounded" rather than a literal limit: Matter reports
        ``max_pin_length`` as ``... or 0``, where ``0`` is "unknown", so it
        normalizes to no upper bound (``max`` of ``None``). A non-positive
        minimum normalizes to ``0`` (no minimum).
        """
        cap = self.capability_for(credential_type)
        if cap is None:
            return None
        return (
            max(cap.min_length, 0),
            cap.max_length if cap.max_length > 0 else None,
        )


def aggregate_length_bounds(
    capabilities: Iterable[LockCapabilities | None],
    credential_type: CredentialType,
) -> tuple[int | None, int | None]:
    """
    Fold many locks' length limits into one tightest-common ``(min, max)``.

    Each bound is ``None`` when nothing constrains it. Locks with no
    capabilities (``None``) or that do not support ``credential_type``
    contribute nothing, so an all-unknown set yields ``(None, None)``.

    The result is the tightest range every lock can satisfy: the largest
    minimum and the smallest maximum. A returned ``min`` greater than ``max``
    signals an unsatisfiable intersection across locks; the caller decides how
    to present it. User-interface defaults deliberately live in the caller, not
    here, so non-interface callers can reuse this unchanged.
    """
    mins: list[int] = []
    maxes: list[int] = []
    for caps in capabilities:
        if caps is None:
            continue
        bounds = caps.length_bounds(credential_type)
        if bounds is None:
            continue
        lo, hi = bounds
        mins.append(lo)
        if hi is not None:
            maxes.append(hi)
    return (max(mins) if mins else None, min(maxes) if maxes else None)


def credential_from_slot(slot: int, state: SlotCredential) -> Credential:
    """
    Build the Personal Identification Number credential for a managed slot.

    The new model relates to the coordinator's ``SlotCredential`` by a
    one-to-one projection: a managed slot index becomes a PIN ``Credential``
    at the same index, reusing the ``SlotCredential`` verbatim as its state.
    """
    return Credential(type=CredentialType.PIN, slot=slot, state=state)


def slot_credential_of(credential: Credential) -> SlotCredential:
    """
    Project a Personal Identification Number credential back to a SlotCredential.

    This is the inverse of ``credential_from_slot`` and is identity on the
    state, so the coordinator can keep consuming ``SlotCredential`` unchanged.
    It rejects non-PIN credentials because only PIN projects one-to-one onto a
    managed slot today.
    """
    if credential.type is not CredentialType.PIN:
        raise ValueError(
            f"Only PIN credentials project to a slot, got {credential.type}"
        )
    return credential.state


def user_from_slot(slot: int, state: SlotCredential, name: str | None = None) -> User:
    """
    Build the single-credential user for a managed slot.

    Realizes today's one-managed-slot to one-user to one-PIN-credential
    projection: the user identifier shares the slot index, the user owns
    exactly one PIN credential at that index, and the user is active exactly
    when the slot holds a code. A future multi-credential world only appends
    to ``credentials`` -- no field changes here.
    """
    return User(
        user_id=slot,
        name=name,
        active=state.is_present,
        credentials=[credential_from_slot(slot, state)],
    )
