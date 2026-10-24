"""Module for Virtual locks."""

from collections.abc import Collection
from dataclasses import dataclass, field
import logging
from typing import Literal, NotRequired, TypedDict, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..model.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SlotCredential,
    User,
    WriteResult,
    user_from_slot,
)
from ._base import BaseLock
from ._util import parse_slot_num

_LOGGER = logging.getLogger(__name__)


class CodeSlotData(TypedDict):
    """What a virtual lock holds in one slot: a PIN, a password, or both."""

    code: NotRequired[str]
    password: NotRequired[str]
    name: str | None


# Where a slot's record keeps each credential type it stores. The PIN is
# under ``code``, the key the store has always used for it.
_FIELDS: dict[CredentialType, Literal["code", "password"]] = {
    CredentialType.PIN: "code",
    CredentialType.PASSWORD: "password",
}


def _user(slot_num: int, record: CodeSlotData | None) -> User:
    """Return the user a virtual slot projects to: its PIN, then its password."""
    pin = (
        SlotCredential.known(str(record["code"]))
        if record is not None and "code" in record
        else SlotCredential.empty()
    )
    user = user_from_slot(slot_num, pin)
    if record is not None and "password" in record:
        user.credentials.append(
            Credential(
                type=CredentialType.PASSWORD,
                slot=slot_num,
                state=SlotCredential.known(record["password"]),
            )
        )
        user.active = True
    return user


@dataclass(repr=False, eq=False)
class VirtualLock(BaseLock):
    """Class to represent Virtual lock."""

    _store: Store[dict[str, CodeSlotData]] = field(init=False, repr=False)
    _data: dict[str, CodeSlotData] = field(default_factory=dict, init=False, repr=False)

    @property
    @override
    def domain(self) -> str:
        """Return integration domain."""
        return "virtual"

    @property
    @override
    def supports_code_slot_events(self) -> bool:
        """
        Return False: a virtual lock observes nothing.

        It is a recording surface -- somewhere to point a use that happened
        beyond Home Assistant's reach -- rather than a device that reports
        one. Recording a use against it does not need this to say otherwise.
        """
        return False

    @property
    @override
    def writable_credential_types(self) -> frozenset[CredentialType]:
        """Return the types a virtual lock's store keeps: PIN and password."""
        return frozenset(_FIELDS)

    @override
    async def async_get_capabilities(self) -> LockCapabilities:
        """
        Advertise every credential type with no limits of any kind.

        A virtual lock is backed by a dictionary, so it genuinely has no
        capacity and no value-length range to report. Every field follows
        the ``CredentialTypeCapability`` convention that a non-positive
        number means "not advertised": ``num_slots`` 0 leaves the slot
        capacity checks skipped, and 0 length bounds mean any credential
        already saved against a virtual lock stays saveable.

        Not a user-managing lock, so ``supports_user_management`` stays
        False and the base orchestration keeps addressing the credential by
        slot -- this class implements no user primitives.
        """
        return LockCapabilities(
            supports_user_management=False,
            max_users=0,
            credential_types={
                credential_type: CredentialTypeCapability(
                    num_slots=0,
                    min_length=0,
                    max_length=0,
                    supports_learn=False,
                )
                for credential_type in CredentialType
            },
        )

    @override
    async def async_is_integration_connected(self) -> bool:
        """Virtual locks are always connected."""
        return True

    @override
    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock by provider."""
        self._store = Store(
            self.hass, 1, f"{self.domain}_{DOMAIN}_{self.lock.entity_id}"
        )
        await self.async_hard_refresh_codes()

    @override
    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        # Chain the base teardown before touching the store: it releases the
        # config-entry-state listener on the lock's provider entry. Left in
        # place, a later provider reload re-runs async_setup against the
        # unloaded Lock Code Manager entry, and anything that then consults
        # the entry crashes on its missing runtime data.
        await super().async_unload(remove_permanently)
        if remove_permanently:
            await self._store.async_remove()
        else:
            await self._store.async_save(self._data)

    @override
    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Reload from store and return all codes."""
        self._data = data if (data := await self._store.async_load()) else {}
        return await self.async_get_usercodes()

    @override
    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set one credential on a code slot; the slot's other credential stays.

        NO_CHANGE if the slot already holds the value under the same name.
        Ignores ``user_id``; slot-only providers address the credential by slot.
        """
        slot_key = str(credential.slot)
        current = self._data.get(slot_key)
        record = CodeSlotData(name=name) if current is None else current.copy()
        record["name"] = name
        record[_FIELDS[credential.type]] = pin
        if record == current:
            return WriteResult.NO_CHANGE
        self._data[slot_key] = record
        return WriteResult.CONFIRMED

    @override
    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ``ref``; return whether it changed.

        A slot holding no credential once it is gone leaves the store.
        """
        slot_key = str(ref.slot)
        key = _FIELDS[ref.type]
        if (current := self._data.get(slot_key)) is None or key not in current:
            return False
        del current[key]
        if not any(field in current for field in _FIELDS.values()):
            del self._data[slot_key]
        return True

    @override
    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Return users by reading all stored and managed slots.

        Each slot is a user with its PIN, empty if it has none, and its
        password if it has one. Managed empty slots are included so the base
        projection can surface them, and unmanaged occupied slots so callers
        like the lock-reset config flow step can detect codes not managed by
        Lock Code Manager.
        """
        managed_slots = self.managed_slots if slots is None else set(slots)
        stored_slots = set()
        for k in self._data:
            slot_num = parse_slot_num(k)
            if slot_num is None:
                _LOGGER.warning(
                    "Virtual lock %s: skipping stored slot with invalid key %r",
                    self.lock.entity_id,
                    k,
                )
                continue
            stored_slots.add(slot_num)
        return [
            _user(slot_num, self._data.get(str(slot_num)))
            for slot_num in managed_slots | stored_slots
        ]
