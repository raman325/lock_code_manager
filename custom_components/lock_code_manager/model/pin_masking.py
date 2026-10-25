"""
Credential masking for logs, and its reverse for diagnostics.

Logs carry ``pin#xxxxxxxx`` and ``pwd#xxxxxxxx`` tokens instead of PINs and
passwords. The deobfuscation service maps those tokens back using the
credentials currently configured.
"""

from collections.abc import Iterable
import re
from typing import Any
import zlib

from homeassistant.config_entries import ConfigEntry

from .config import EntryConfig
from .credentials import CredentialType

# Token prefix by credential type. A PIN's is what logs have always carried.
_TOKEN_PREFIX = {CredentialType.PIN: "pin", CredentialType.PASSWORD: "pwd"}


def mask_pin(pin: str | None, slot_num: int | str, instance_id: str) -> str:
    """Return the masked form of a PIN for logging (see ``mask_credential``)."""
    return mask_credential(pin, slot_num, instance_id, CredentialType.PIN)


def mask_credential(
    value: str | None,
    slot_num: int | str,
    instance_id: str,
    credential_type: CredentialType,
) -> str:
    """
    Return a deterministic masked representation of a credential for logging.

    CRC32 salted with the Home Assistant instance ID and slot number, so the same value
    on the same slot always produces the same 8-char hex token regardless of
    which lock or config entry it belongs to. Any type but the PIN is salted
    with its type too, so a PIN and a password of the same value differ.
    """
    if not value:
        return "<empty>"
    if credential_type is CredentialType.PIN:
        salt = f"{instance_id}:{slot_num}:{value}"
    else:
        salt = f"{instance_id}:{slot_num}:{credential_type}:{value}"
    prefix = _TOKEN_PREFIX[credential_type]
    return f"{prefix}#{zlib.crc32(salt.encode()) & 0xFFFFFFFF:08x}"


# Tokens emitted by ``mask_credential``: a type prefix, "#", then 8 lowercase
# hex chars. Anchored by word boundary on the right so a longer hex string
# isn't truncated and reported as a match.
_PIN_TOKEN_RE = re.compile(r"(?:pin|pwd)#[0-9a-f]{8}\b")


def build_pin_deobfuscation_map(
    entries: Iterable[ConfigEntry], instance_id: str
) -> dict[str, str]:
    """
    Build a ``{masked_token: plaintext}`` lookup.

    Covers every configured PIN and password. Reads each entry's
    options-or-data view via ``EntryConfig`` so it works whether or not the
    entry has been migrated to options yet. An empty credential is skipped
    because ``mask_credential`` returns ``<empty>`` for it and that's not a
    token we need to reverse.

    Slot is part of the salt, so different ``(slot, value)`` pairs produce
    different tokens with overwhelming probability — CRC32 has a 32-bit
    output so collisions are mathematically possible but vanishingly
    unlikely at any plausible slot count. The resulting map has one
    entry per configured credential.
    """
    table: dict[str, str] = {}
    for entry in entries:
        config = EntryConfig.from_entry(entry)
        for slot_num, slot_config in config.slots.items():
            for credential_type in _TOKEN_PREFIX:
                if value := slot_config.get(credential_type.value):
                    table[
                        mask_credential(value, slot_num, instance_id, credential_type)
                    ] = value
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
