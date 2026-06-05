"""Utility functions for lock_code_manager."""

from __future__ import annotations

from collections.abc import Iterable
import logging
import re
from typing import Any
import zlib

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TURN_OFF
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue

from ..const import DOMAIN
from .config import EntryConfig, build_slot_unique_id

_LOGGER = logging.getLogger(__name__)


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


async def async_disable_slot(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    config_entry_id: str,
    slot_num: int,
    *,
    reason: str | None = None,
    lock_name: str | None = None,
    lock_entity_id: str | None = None,
) -> bool:
    """
    Disable a slot via the enabled switch and optionally create a repair issue.

    Returns True if the switch was found and turned off, False otherwise.
    When reason is provided, a repair issue is created so the user can
    acknowledge it through the Home Assistant repairs dashboard.
    """
    enabled_entity_id = ent_reg.async_get_entity_id(
        SWITCH_DOMAIN,
        DOMAIN,
        build_slot_unique_id(config_entry_id, slot_num, CONF_ENABLED),
    )
    if not enabled_entity_id:
        lock_context = f" on {lock_name} ({lock_entity_id})" if lock_name else ""
        _LOGGER.warning(
            "Cannot disable slot %s%s — switch entity not found",
            slot_num,
            lock_context,
        )
        return False

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: enabled_entity_id},
        blocking=True,
    )

    if reason:
        issue_id = f"slot_disabled_{config_entry_id}_{slot_num}"
        async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="slot_disabled",
            translation_placeholders={
                "slot_num": str(slot_num),
                "reason": reason,
            },
        )

    return True
