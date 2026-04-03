"""Schlage WiFi lock provider.

Schlage locks manage access codes by name rather than numeric slot numbers.
This provider bridges that gap by tagging code names with a slot prefix
in the format ``[LCM:<slot>] <friendly name>``.  Pre-existing codes
discovered on the lock are automatically tagged and assigned to the next
available slot number.

All lock operations go through the Home Assistant Schlage integration
services (``schlage.get_codes``, ``schlage.add_code``,
``schlage.delete_code``) rather than importing pyschlage directly.

PINs are write-only from the Schlage API perspective: the ``get_codes``
service returns masked PINs (``****``), so occupied slots report
SlotCode.UNKNOWN and cleared slots report SlotCode.EMPTY.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import re

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import get_entry_data
from ..exceptions import LockDisconnected
from ..models import SlotCode
from ._base import BaseLock
from .const import LOGGER

SCHLAGE_DOMAIN = "schlage"

# Regex to parse the Lock Code Manager slot tag from code names.
# Format: [LCM:XX] Friendly Name
_SLOT_TAG_RE = re.compile(r"^\[LCM:(\d+)\]\s*(.*)")


def _make_tagged_name(slot_num: int, name: str | None = None) -> str:
    """Create a tagged code name with Lock Code Manager slot number."""
    base = name or f"Code Slot {slot_num}"
    return f"[LCM:{slot_num}] {base}"


def _parse_tag(name: str) -> tuple[int | None, str]:
    """Parse a Lock Code Manager slot tag from a code name.

    Returns ``(slot_num, friendly_name)`` when a tag is present, or
    ``(None, original_name)`` when no tag is found.
    """
    match = _SLOT_TAG_RE.match(name)
    if match:
        return int(match.group(1)), match.group(2)
    return None, name


def _is_masked_pin(pin: str) -> bool:
    """Return True if a PIN looks masked or placeholder (e.g. '****', empty)."""
    if not pin:
        return True
    return len(set(pin)) == 1 and pin[0] == "*"


@dataclass(repr=False, eq=False)
class SchlageLock(BaseLock):
    """Schlage WiFi lock provider implementation.

    Codes on Schlage locks are identified by friendly name, not by slot
    number.  This provider assigns virtual slot numbers by embedding a
    ``[LCM:<slot>]`` tag in each code's name.

    PINs are write-only: ``get_codes`` returns masked values, so the
    coordinator sees SlotCode.UNKNOWN for occupied slots and SlotCode.EMPTY
    for cleared slots.
    """

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

    def _get_managed_slots(self) -> set[int]:
        """Return the set of slot numbers managed by Lock Code Manager for this lock."""
        return {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }

    async def _async_get_codes(self) -> dict[str, dict[str, str]]:
        """Call ``schlage.get_codes`` and return the response dict.

        Returns a dict mapping access-code IDs to ``{"name": ..., "code": ...}``.
        """
        entity_id = self.lock.entity_id
        try:
            response = await self.hass.services.async_call(
                SCHLAGE_DOMAIN,
                "get_codes",
                service_data={"entity_id": entity_id},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Schlage get_codes failed for {entity_id}: {err}"
            ) from err

        if not isinstance(response, dict):
            return {}

        # Platform entity services wrap the response per entity_id.
        entity_response = response.get(entity_id, response)
        if isinstance(entity_response, dict):
            return entity_response
        return {}

    async def _async_add_code(self, name: str, code: str) -> None:
        """Add a new code with the given name and PIN."""
        entity_id = self.lock.entity_id
        try:
            await self.hass.services.async_call(
                SCHLAGE_DOMAIN,
                "add_code",
                service_data={"entity_id": entity_id, "name": name, "code": code},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Schlage add_code failed for {entity_id}: {err}"
            ) from err

    async def _async_delete_code(self, name: str) -> None:
        """Delete a code by its full name (including any Lock Code Manager tag)."""
        entity_id = self.lock.entity_id
        try:
            await self.hass.services.async_call(
                SCHLAGE_DOMAIN,
                "delete_code",
                service_data={"entity_id": entity_id, "name": name},
                blocking=True,
            )
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Schlage delete_code failed for {entity_id}: {err}"
            ) from err

    async def async_is_integration_connected(self) -> bool:
        """Return whether the Schlage integration is loaded."""
        if not self.lock_config_entry:
            return False
        return self.lock_config_entry.state == ConfigEntryState.LOADED

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Get dictionary of code slots and usercodes.

        Schlage PINs are write-only (returned as masked values), so occupied
        slots return SlotCode.UNKNOWN and cleared slots return SlotCode.EMPTY.

        Untagged codes discovered on the lock are automatically tagged and
        assigned to the next available managed slot.
        """
        managed_slots = self._get_managed_slots()
        if not managed_slots:
            return {}

        codes = await self._async_get_codes()

        assigned_slots: set[int] = set()
        # (code_id, pin, slot, friendly_name)
        tagged: list[tuple[str, str, int, str]] = []
        # (code_id, pin, original_name)
        untagged: list[tuple[str, str, str]] = []

        for code_id, code_data in codes.items():
            name = code_data.get("name", "")
            pin = code_data.get("code", "")
            slot_num, friendly_name = _parse_tag(name)
            if slot_num is not None:
                tagged.append((code_id, pin, slot_num, friendly_name))
                assigned_slots.add(slot_num)
            else:
                untagged.append((code_id, pin, name))

        # Track which managed slots have tagged codes
        occupied_slots: set[int] = set()

        # Process tagged codes: keep the first per slot (sorted by code_id for determinism)
        tagged.sort(key=lambda t: t[0])
        seen_slots: set[int] = set()
        for code_id, pin, slot_num, friendly_name in tagged:
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

        # Auto-tag untagged codes into the next available managed slot
        sorted_managed = sorted(managed_slots)
        next_slot_idx = 0
        for _code_id, pin, original_name in untagged:
            if not original_name or not original_name.strip():
                LOGGER.debug(
                    "Lock %s: skipping code with empty or whitespace name",
                    self.lock.entity_id,
                )
                continue

            if _is_masked_pin(pin):
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
            except LockDisconnected:
                LOGGER.error(
                    "Lock %s: failed to tag code '%s' for slot %d",
                    self.lock.entity_id,
                    original_name,
                    prospective_slot,
                )
                continue

            try:
                await self._async_delete_code(original_name)
            except LockDisconnected:
                LOGGER.warning(
                    "Lock %s: tagged code added but failed to delete original '%s' "
                    "for slot %d, attempting rollback",
                    self.lock.entity_id,
                    original_name,
                    prospective_slot,
                )
                try:
                    await self._async_delete_code(tagged_name)
                except LockDisconnected:
                    LOGGER.error(
                        "Lock %s: rollback failed for tagged code '%s', "
                        "lock may have duplicate entries",
                        self.lock.entity_id,
                        tagged_name,
                    )
                continue

            assigned_slots.add(prospective_slot)
            occupied_slots.add(prospective_slot)
            LOGGER.debug(
                "Lock %s: tagged code '%s' as slot %d: '%s'",
                self.lock.entity_id,
                original_name,
                prospective_slot,
                tagged_name,
            )

        # Build final result: UNKNOWN for occupied, EMPTY for unoccupied managed slots
        return {
            slot: SlotCode.UNKNOWN if slot in occupied_slots else SlotCode.EMPTY
            for slot in managed_slots
        }

    async def async_set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
    ) -> bool:
        """Set user code on a virtual slot.

        If a code already exists for the given slot it is replaced.
        Returns True unconditionally because Schlage PINs are write-only
        and we cannot determine whether the value actually changed.
        """
        codes = await self._async_get_codes()

        # Look for an existing code on this slot so we can preserve its
        # friendly name when the caller does not supply one.
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

        # Add the new code first to avoid data loss if the add fails.
        await self._async_add_code(tagged_name, usercode)

        if existing_full_name and existing_full_name != tagged_name:
            try:
                await self._async_delete_code(existing_full_name)
            except LockDisconnected:
                LOGGER.warning(
                    "Lock %s: code set on slot %s but failed to remove old entry '%s'",
                    self.lock.entity_id,
                    code_slot,
                    existing_full_name,
                )

        LOGGER.debug(
            "Lock %s: set usercode on slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear user code from a virtual slot.

        Returns True if a code was deleted, False if the slot was already empty.
        """
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
            "Lock %s: cleared usercode from slot %s",
            self.lock.entity_id,
            code_slot,
        )
        return True

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Perform hard refresh and return all codes.

        Schlage has no cache to invalidate, so this is identical to
        async_get_usercodes.
        """
        return await self.async_get_usercodes()
