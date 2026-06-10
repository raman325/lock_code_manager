"""
Schlage WiFi lock provider.

Schlage locks manage access codes by name rather than numeric slot numbers.
This provider bridges that gap by tagging code names with a slot prefix
in the format ``[LCM:<slot>] <friendly name>``.  Pre-existing codes
discovered on the lock are automatically tagged and assigned to the next
available slot number.

All lock operations go through the Home Assistant Schlage integration
services (``schlage.get_codes``, ``schlage.add_code``,
``schlage.delete_code``).

PINs are write-only from the Schlage API perspective: the ``get_codes``
service returns masked PINs (``****``), so occupied slots report
``SlotCredential.unreadable()`` and cleared slots report
``SlotCredential.empty()``.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from homeassistant.config_entries import ConfigEntry

from ..domain.credentials import Credential, CredentialRef, User, user_from_slot
from ..domain.exceptions import (
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
)
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import make_legacy_tagged_name as _make_tagged_name, parse_tag as _parse_tag
from .const import LOGGER

SCHLAGE_DOMAIN = "schlage"


@dataclass(repr=False, eq=False)
class SchlageLock(BaseLock):
    """
    Schlage WiFi lock provider implementation.

    Codes on Schlage locks are identified by friendly name, not by slot
    number.  This provider assigns virtual slot numbers by embedding a
    ``[LCM:<slot>]`` tag in each code's name.

    PINs are write-only: ``get_codes`` returns masked values, so the
    coordinator sees ``SlotCredential.unreadable()`` for occupied slots
    and ``SlotCredential.empty()`` for cleared slots.
    """

    # Tracks whether the initial auto-tag pass already ran for this
    # provider instance. Skips re-tagging on reconnects so a drifted
    # device list does not produce double-tag / rename storms. Reset
    # naturally when the provider instance is recreated on full reload.
    _tagged_once: bool = field(default=False, init=False)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return SCHLAGE_DOMAIN

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False

    @property
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

    async def _async_add_code(self, name: str, code: str) -> None:
        """Add a new code with the given name and PIN."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            SCHLAGE_DOMAIN,
            "add_code",
            service_data={"entity_id": entity_id, "name": name, "code": code},
        )

    async def _async_delete_code(self, name: str) -> None:
        """Delete a code by its full name (including any Lock Code Manager tag)."""
        entity_id = self.lock.entity_id
        await self.async_call_service(
            SCHLAGE_DOMAIN,
            "delete_code",
            service_data={"entity_id": entity_id, "name": name},
        )

    async def async_is_device_available(self) -> bool:
        """Return whether the Schlage lock device is available for commands."""
        try:
            await self._async_get_codes()
        except LockCodeManagerProviderError as err:
            LOGGER.debug(
                "Lock %s: availability check failed: %s",
                self.lock.entity_id,
                err,
            )
            return False
        return True

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """
        Set up lock by performing initial auto-tagging of unmanaged codes.

        Idempotent: the tag pass runs only once per provider instance to
        prevent double-tag / rename storms on reconnect (the device list
        may have drifted since the previous load).
        """
        await super().async_setup(config_entry)
        if self._tagged_once:
            return
        await self._async_tag_unmanaged_codes()
        self._tagged_once = True

    async def _async_tag_unmanaged_codes(self) -> None:
        """
        Tag unmanaged codes on the lock with Lock Code Manager slot numbers.

        Discovers untagged codes and assigns them to the next available managed
        slot by renaming (add tagged, delete original). Runs under the
        sequence lock so concurrent set/clear/hard-refresh callers do not
        interleave with the multi-step rename.
        """
        managed_slots = self.managed_slots
        if not managed_slots:
            return

        async with self._serialize_sequence():
            await self._async_run_tag_pass(managed_slots)

    async def _async_run_tag_pass(self, managed_slots: set[int]) -> None:
        """
        Body of the tag pass; caller holds the sequence lock.

        Per-code ``LockDisconnected`` is logged and the loop continues so
        any remaining codes still get a chance to tag, but the disconnect
        is re-raised at the end. ``async_setup`` then leaves
        ``_tagged_once`` False so the reconnect path retries once the lock
        is reachable. ``LockOperationFailed`` is a per-code condition and
        does not gate idempotency.
        """
        codes = await self._async_get_codes()

        assigned_slots: set[int] = set()
        # (code_id, original_name, pin)
        untagged: list[tuple[str, str, str]] = []

        for code_id, code_data in codes.items():
            name = code_data.get("name", "")
            pin = code_data.get("code", "")
            slot_num, _friendly_name = _parse_tag(name)
            if slot_num is not None:
                assigned_slots.add(slot_num)
            else:
                untagged.append((code_id, name, pin))

        sorted_managed = sorted(managed_slots)
        next_slot_idx = 0
        first_disconnect: LockDisconnected | None = None
        for _code_id, original_name, pin in untagged:
            if not original_name or not original_name.strip():
                LOGGER.debug(
                    "Lock %s: skipping code with empty or whitespace name",
                    self.lock.entity_id,
                )
                continue

            # Empty or all-asterisk (masked) PINs are untaggable.
            if pin == "*" * len(pin):
                LOGGER.debug(
                    "Lock %s: skipping untaggable code '%s' (PIN is masked or empty)",
                    self.lock.entity_id,
                    original_name,
                )
                continue

            # Find next available managed slot
            prospective_slot: int | None = None
            while next_slot_idx < len(sorted_managed):
                candidate = sorted_managed[next_slot_idx]
                next_slot_idx += 1
                if candidate not in assigned_slots:
                    prospective_slot = candidate
                    break

            if prospective_slot is None:
                LOGGER.debug(
                    "Lock %s: no managed slot available for untagged code '%s'",
                    self.lock.entity_id,
                    original_name,
                )
                continue

            tagged_name = _make_tagged_name(prospective_slot, original_name)

            try:
                await self._async_add_code(tagged_name, pin)
            except (LockDisconnected, LockOperationFailed) as err:
                LOGGER.error(
                    "Lock %s: failed to tag code '%s' for slot %d: %s",
                    self.lock.entity_id,
                    original_name,
                    prospective_slot,
                    err,
                )
                if first_disconnect is None and isinstance(err, LockDisconnected):
                    first_disconnect = err
                continue

            try:
                await self._async_delete_code(original_name)
            except (LockDisconnected, LockOperationFailed) as err:
                LOGGER.warning(
                    "Lock %s: tagged code added but failed to delete original '%s' "
                    "for slot %d: %s, attempting rollback",
                    self.lock.entity_id,
                    original_name,
                    prospective_slot,
                    err,
                )
                if first_disconnect is None and isinstance(err, LockDisconnected):
                    first_disconnect = err
                try:
                    await self._async_delete_code(tagged_name)
                except (LockDisconnected, LockOperationFailed) as rollback_err:
                    LOGGER.error(
                        "Lock %s: rollback failed for tagged code '%s', "
                        "lock may have duplicate entries: %s",
                        self.lock.entity_id,
                        tagged_name,
                        rollback_err,
                    )
                    if first_disconnect is None and isinstance(
                        rollback_err, LockDisconnected
                    ):
                        first_disconnect = rollback_err
                continue

            assigned_slots.add(prospective_slot)
            LOGGER.debug(
                "Lock %s: tagged code '%s' as slot %d: '%s'",
                self.lock.entity_id,
                original_name,
                prospective_slot,
                tagged_name,
            )

        if first_disconnect is not None:
            raise LockDisconnected(
                f"Lock {self.lock.entity_id}: disconnect during tag pass; "
                "will be retried on reconnect"
            ) from first_disconnect

    async def async_get_users(self) -> list[User]:
        """
        Return users by reading all tagged codes from the Schlage lock.

        Schlage Personal Identification Numbers are write-only (returned as
        masked values by ``get_codes``), so occupied slots surface as
        ``SlotCredential.unreadable()`` users and empty managed slots surface
        as ``SlotCredential.empty()`` users. Only codes whose slot numbers
        fall within the managed set are returned. Auto-tagging of unmanaged
        codes is handled separately by ``_async_tag_unmanaged_codes()``.
        """
        managed_slots = self.managed_slots
        if not managed_slots:
            return []

        codes = await self._async_get_codes()

        occupied_slots: set[int] = set()

        # Collect tagged codes; keep the first per slot (sort by code_id for determinism).
        tagged: list[tuple[str, int, str]] = []
        for code_id, code_data in codes.items():
            name = code_data.get("name", "")
            slot_num, friendly_name = _parse_tag(name)
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

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """
        Set a Personal Identification Number credential on a slot.

        If a code already exists for the given slot it is replaced.
        Returns True unconditionally because Schlage Personal Identification
        Numbers are write-only and we cannot determine whether the value
        actually changed. The get/delete/add sequence runs under the sequence
        lock so concurrent sync ticks cannot interleave their own multi-step
        writes against the same slot. Ignores ``user_id``; slot-only providers
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
                parsed_slot, friendly = _parse_tag(code_name)
                if parsed_slot == code_slot:
                    existing_full_name = code_name
                    existing_friendly_name = friendly
                    break

            effective_name = name or existing_friendly_name
            tagged_name = _make_tagged_name(code_slot, effective_name)

            # Clear any existing code on this slot before adding. Schlage rejects
            # add_code with a duplicate name, and eventual consistency in the cloud
            # API means get_codes may not reflect a recently-added code.
            names_to_delete = {n for n in (existing_full_name, tagged_name) if n}
            for code_name in names_to_delete:
                # code may not exist — that's fine
                with contextlib.suppress(LockDisconnected, LockOperationFailed):
                    await self._async_delete_code(code_name)

            try:
                await self._async_add_code(tagged_name, pin)
            except (LockDisconnected, LockOperationFailed) as err:
                # Schlage's cloud API has eventual consistency: a delete may not
                # propagate before the add, causing "already exists".  Since Personal
                # Identification Numbers are write-only we can't verify the value,
                # but the code IS on the lock — treat it as success so the sync
                # manager doesn't loop.
                if "already exists" not in str(err).lower():
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
        return True

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
                parsed_slot, _ = _parse_tag(code_data.get("name", ""))
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

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """
        Perform hard refresh and return all codes.

        Schlage has no cache to invalidate; re-tags unmanaged codes and then
        reads the current state. Hard refresh is an explicit drift-detection
        request, so the setup-time idempotency gate does not apply here.
        """
        managed_slots = self.managed_slots
        if managed_slots:
            async with self._serialize_sequence():
                await self._async_run_tag_pass(managed_slots)
        return await self.async_get_usercodes()
