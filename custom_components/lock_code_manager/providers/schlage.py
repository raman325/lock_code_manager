"""
Schlage WiFi lock provider.

Schlage locks manage access codes by name rather than numeric slot numbers.
This provider bridges that gap by tagging code names with a slot prefix
in the format ``lcm:<slot>:<friendly name>`` (older releases wrote
``[LCM:<slot>]``, which is still read). A code without the tag is not Lock
Code Manager's: it is never read as a slot, renamed or deleted.

All lock operations go through the Home Assistant Schlage integration
services (``schlage.get_codes``, ``schlage.add_code``,
``schlage.delete_code``).

PINs are write-only from the Schlage API perspective: the ``get_codes``
service returns masked PINs (``****``), so occupied slots report
``SlotCredential.unreadable()`` and cleared slots report
``SlotCredential.empty()``.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, override

from homeassistant.const import STATE_UNAVAILABLE

from ..const import PENDING_WRITE_TTL
from ..model.credentials import (
    Credential,
    CredentialRef,
    SlotCredential,
    User,
    WriteResult,
    user_from_slot,
)
from ..model.exceptions import (
    CodeRejectedError,
    LockCodeManagerProviderError,
)
from ._base import BaseLock
from ._util import (
    make_tagged_name as _make_tagged_name,
    parse_full_tag,
)
from .const import LOGGER

SCHLAGE_DOMAIN = "schlage"


@dataclass(repr=False, eq=False)
class SchlageLock(BaseLock):
    """
    Schlage WiFi lock provider implementation.

    Codes on Schlage locks are identified by friendly name, not by slot
    number.  This provider assigns virtual slot numbers by embedding a
    ``lcm:<slot>:`` tag in each code's name.

    PINs are write-only: ``get_codes`` returns masked values, so the
    coordinator sees ``SlotCredential.unreadable()`` for occupied slots
    and ``SlotCredential.empty()`` for cleared slots.
    """

    # The cloud reads back the table from before a write for a while (#1083).
    read_lag = PENDING_WRITE_TTL

    @property
    @override
    def domain(self) -> str:
        """Return integration domain."""
        return SCHLAGE_DOMAIN

    @property
    @override
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False

    @property
    @override
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=5)

    async def _async_get_codes(self) -> dict[str, dict[str, str]]:
        """
        Call ``schlage.get_codes`` and return the response dict.

        Returns a dict mapping access-code IDs to ``{"name": ..., "code": ...}``.
        """
        entity_id = self.lock.entity_id
        response = await self.async_call_service(
            SCHLAGE_DOMAIN,
            "get_codes",
            service_data={"entity_id": entity_id},
            return_response=True,
        )

        if not isinstance(response, dict):
            raise LockCodeManagerProviderError(
                f"Schlage get_codes returned malformed response for {entity_id}: "
                f"expected dict, got {type(response).__name__}"
            )

        # Platform entity services wrap the response per entity_id.
        entity_response = response.get(entity_id, response)
        if not isinstance(entity_response, dict):
            raise LockCodeManagerProviderError(
                f"Schlage get_codes returned malformed entity response for "
                f"{entity_id}: expected dict, got {type(entity_response).__name__}"
            )
        return entity_response

    async def _async_add_code(
        self, name: str, code: str, code_slot: int | None = None
    ) -> None:
        """Add a new code with the given name and PIN; a refusal is ``code_slot``'s."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            SCHLAGE_DOMAIN,
            "add_code",
            service_data={"entity_id": entity_id, "name": name, "code": code},
            code_slot=code_slot,
        )

    async def _async_delete_code(self, name: str) -> None:
        """Delete a code by its full name (including any Lock Code Manager tag)."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            SCHLAGE_DOMAIN,
            "delete_code",
            service_data={"entity_id": entity_id, "name": name},
        )

    @override
    async def async_is_device_available(self) -> bool:
        """
        Return whether the Schlage lock is reachable, per the lock entity's state.

        The Schlage integration already asks the cloud about every lock every
        30 seconds, and its lock entity is available only while that answers
        and the cloud says the lock itself is connected. Reading that state
        costs nothing; reading the codes to find out was a full cloud read of
        them every connection check, outside the worker's pacing.
        """
        state = self.hass.states.get(self.lock.entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    @override
    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Return users by reading all tagged codes from the Schlage lock.

        Schlage Personal Identification Numbers are write-only (returned as
        masked values by ``get_codes``), so occupied slots surface as
        ``SlotCredential.unreadable()`` users and empty managed slots surface
        as ``SlotCredential.empty()`` users. Only codes whose slot numbers
        fall within the managed set are returned; a code without the tag is
        not one of them.
        """
        managed_slots = self.managed_slots if slots is None else set(slots)
        if not managed_slots:
            return []

        codes = await self._async_get_codes()

        occupied_slots: set[int] = set()

        # Collect tagged codes; keep the first per slot (sort by code_id for determinism).
        tagged: list[tuple[str, int, str]] = []
        for code_id, code_data in codes.items():
            name = code_data.get("name", "")
            slot_num, friendly_name = parse_full_tag(name)
            if slot_num is not None:
                tagged.append((code_id, slot_num, friendly_name))

        tagged.sort(key=lambda t: t[0])
        seen_slots: set[int] = set()
        for code_id, slot_num, friendly_name in tagged:
            if slot_num not in managed_slots:
                LOGGER.debug(
                    "Lock %s: ignoring tagged code slot %d outside managed range",
                    self.lock.entity_id,
                    slot_num,
                )
                continue
            if slot_num in seen_slots:
                LOGGER.warning(
                    "Lock %s: duplicate tag for slot %d (code_id=%s, name='%s'), "
                    "skipping in favor of earlier entry",
                    self.lock.entity_id,
                    slot_num,
                    code_id,
                    friendly_name,
                )
                continue
            seen_slots.add(slot_num)
            occupied_slots.add(slot_num)

        slot_states = {
            slot: (
                SlotCredential.unreadable()
                if slot in occupied_slots
                else SlotCredential.empty()
            )
            for slot in managed_slots
        }
        return [user_from_slot(slot, state) for slot, state in slot_states.items()]

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
        Set a Personal Identification Number credential on a slot.

        If a code already exists for the given slot it is replaced.
        Returns True unconditionally because Schlage Personal Identification
        Numbers are write-only and we cannot determine whether the value
        actually changed. The get/delete/add sequence runs under the sequence
        lock so no other caller (the unmanaged-code sweep, allocation) can
        interleave its own multi-step writes against the same slot. Ignores ``user_id``; slot-only providers
        address the credential by slot.
        """
        code_slot = credential.slot
        async with self._serialize_sequence():
            codes = await self._async_get_codes()

            # Preserve the friendly name from an existing code if the caller doesn't supply one.
            existing_full_name: str | None = None
            existing_friendly_name: str | None = None
            for code_data in codes.values():
                code_name = code_data.get("name", "")
                parsed_slot, friendly = parse_full_tag(code_name)
                if parsed_slot == code_slot:
                    existing_full_name = code_name
                    existing_friendly_name = friendly
                    break

            effective_name = name or existing_friendly_name
            tagged_name = _make_tagged_name(code_slot, effective_name)

            # Clear any existing code on this slot before adding. Schlage rejects
            # add_code with a duplicate name, and eventual consistency in the cloud
            # API means get_codes may not reflect a recently-added code. A name
            # not on the lock is a quiet success in core's delete_code, so a
            # failure here is real: it propagates before the add, or the old
            # code stays live beside (or, same name, in place of) the new one.
            names_to_delete = {n for n in (existing_full_name, tagged_name) if n}
            deleted_pins = {
                code_data.get("code")
                for code_data in codes.values()
                if code_data.get("name") in names_to_delete
            }
            for code_name in names_to_delete:
                await self._async_delete_code(code_name)

            try:
                await self._async_add_code(tagged_name, pin, code_slot)
            except CodeRejectedError as err:
                # Schlage's cloud API has eventual consistency: a delete may not
                # propagate before the add, so the code just deleted is still
                # read back and the add is refused for its name or its PIN
                # "already existing". Since Personal Identification Numbers are
                # write-only we can't verify the value, but the code IS on the
                # lock — treat it as success so sync doesn't loop.
                # Any other refusal, a PIN another code holds included, is the
                # lock's answer.
                key = getattr(err.__cause__, "translation_key", None)
                if key != "schlage_name_exists" and (
                    key != "schlage_code_exists" or pin not in deleted_pins
                ):
                    raise

                LOGGER.debug(
                    "Lock %s: code for slot %s already exists on lock "
                    "(eventual consistency), treating as successful set",
                    self.lock.entity_id,
                    code_slot,
                )

        LOGGER.debug(
            "Lock %s: set Personal Identification Number credential on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return WriteResult.CONFIRMED

    @override
    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ``ref``; return whether it changed.

        Returns True if a code was deleted, False if the slot was already
        empty. The get/delete sequence runs under the sequence lock so
        concurrent callers cannot interleave their own multi-step reads
        against the same slot.
        """
        code_slot = ref.slot
        async with self._serialize_sequence():
            codes = await self._async_get_codes()
            target_name: str | None = None
            for code_data in codes.values():
                parsed_slot, _ = parse_full_tag(code_data.get("name", ""))
                if parsed_slot == code_slot:
                    target_name = code_data.get("name", "")
                    break

            if not target_name:
                LOGGER.debug(
                    "Lock %s: no code found for slot %s, already clear",
                    self.lock.entity_id,
                    code_slot,
                )
                return False

            await self._async_delete_code(target_name)

        LOGGER.debug(
            "Lock %s: deleted Personal Identification Number credential on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True

    @override
    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Return all codes; Schlage keeps no cache, so every read is fresh."""
        return await self.async_get_usercodes()
