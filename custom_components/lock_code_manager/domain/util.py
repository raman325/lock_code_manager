"""Utility functions for lock_code_manager."""

from collections.abc import Iterable
import re
from typing import Any
import zlib

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    CONF_PIN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .config import EntryConfig


@callback
def lock_display_name(hass: HomeAssistant, lock_entity_id: str) -> str:
    """
    Name a lock the way Home Assistant does.

    The friendly name first, because that is the string on screen and the
    one a person would use to say which lock they mean. Everything after
    it reconstructs that name for a lock with no state yet, in the order
    Home Assistant composes it.

    The device step is not a nicety. A lock entity that sets
    ``has_entity_name`` with no name of its own -- which is how Z-Wave,
    Matter and most modern integrations do it -- carries an empty
    ``original_name`` and takes its whole visible name from its device.
    Stopping at the registry row names such a lock ``lock.front_door``.
    """
    if (state := hass.states.get(lock_entity_id)) and (
        friendly_name := state.attributes.get(ATTR_FRIENDLY_NAME)
    ):
        return str(friendly_name)
    if entity := er.async_get(hass).async_get(lock_entity_id):
        if name := entity.name or entity.original_name:
            return name
        if (
            entity.device_id
            and (device := dr.async_get(hass).async_get(entity.device_id))
            and (device_name := device.name_by_user or device.name)
        ):
            return device_name
    # Nothing left to ask, so fall back to the object id, which is what the
    # lock's own entity id was slugged from in the first place.
    return lock_entity_id.split(".", 1)[-1].replace("_", " ")


# Every repair issue keyed on a lock entity id. Creation sites live in
# different layers (lock_offline in the coordinator, lock_setup_failed in
# the provider base), but cleanup on entry removal iterates this tuple —
# add new per-lock issue keys here or their persistent issues orphan when
# the entry is deleted.
PER_LOCK_ISSUE_KEYS = ("lock_offline", "lock_setup_failed", "lock_dropped")


def per_lock_issue_id(key: str, lock_entity_id: str) -> str:
    """Build the repair issue id for a per-lock issue key."""
    return f"{key}_{lock_entity_id}"


def mask_pin(
    pin: str | None,
    slot_num: int | str,
    instance_id: str,
) -> str:
    """
    Return a deterministic masked representation of a PIN for logging.

    Uses CRC32 salted with the HA instance ID and slot number so the same
    PIN on the same slot always produces the same 8-char hex token regardless
    of which lock or config entry it belongs to.  Different slots or HA
    instances produce different tokens.
    """
    if not pin:
        return "<empty>"
    salt = f"{instance_id}:{slot_num}:{pin}"
    return f"pin#{zlib.crc32(salt.encode()) & 0xFFFFFFFF:08x}"


# Token format emitted by ``mask_pin``: literal "pin#" followed by 8 lowercase
# hex chars. Anchored by word boundary on the right so a longer hex string
# isn't truncated and reported as a match.
_PIN_TOKEN_RE = re.compile(r"pin#[0-9a-f]{8}\b")


def build_pin_deobfuscation_map(
    entries: Iterable[ConfigEntry], instance_id: str
) -> dict[str, str]:
    """
    Build a ``{masked_token: plaintext_pin}`` lookup for every configured PIN.

    Reads each entry's options-or-data view via ``EntryConfig`` so it works
    whether or not the entry has been migrated to options yet. Slots whose
    PIN is empty are skipped because ``mask_pin`` returns ``<empty>`` for
    them and that's not a token we need to reverse.

    Slot is part of the salt, so different ``(slot, pin)`` pairs produce
    different tokens with overwhelming probability — CRC32 has a 32-bit
    output so collisions are mathematically possible but vanishingly
    unlikely at any plausible slot count. The resulting map has one
    entry per configured slot with a PIN.
    """
    table: dict[str, str] = {}
    for entry in entries:
        config = EntryConfig.from_entry(entry)
        for slot_num, slot_config in config.slots.items():
            pin = slot_config.get(CONF_PIN)
            if not pin:
                continue
            table[mask_pin(pin, slot_num, instance_id)] = pin
    return table


def deobfuscate_pins(text: str, table: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """
    Replace ``pin#xxxxxxxx`` tokens in ``text`` using ``table``.

    Tokens not in the table are left verbatim so the output stays
    paste-compatible with the original log (useful when only some PINs
    have been rotated since the log was written). The summary lists the
    distinct unmatched tokens so the caller can see what didn't resolve
    without scanning the text.
    """
    counts = {"total": 0, "matched": 0}
    unmatched: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        counts["total"] += 1
        if token in table:
            counts["matched"] += 1
            return table[token]
        unmatched.add(token)
        return token

    deobfuscated = _PIN_TOKEN_RE.sub(_replace, text)
    summary: dict[str, Any] = {
        "total": counts["total"],
        "matched": counts["matched"],
        "unmatched_tokens": sorted(unmatched),
    }
    return deobfuscated, summary
