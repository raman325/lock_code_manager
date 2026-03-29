"""Utility functions for lock_code_manager."""

from __future__ import annotations

import zlib


def mask_pin(pin: str | None, lock_entity_id: str, instance_id: str) -> str:
    """Return a deterministic masked representation of a PIN for logging.

    Uses CRC32 salted with the HA instance ID and lock entity ID so the
    same PIN on the same lock always produces the same 8-char hex token.
    Different locks or HA instances produce different tokens, preventing
    cross-correlation.
    """
    if not pin:
        return "<empty>"
    salt = f"{instance_id}:{lock_entity_id}:{pin}"
    return f"pin#{zlib.crc32(salt.encode()) & 0xFFFFFFFF:08x}"
