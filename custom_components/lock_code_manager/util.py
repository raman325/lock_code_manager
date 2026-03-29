"""Utility functions for lock_code_manager."""

from __future__ import annotations

import zlib

# Cached instance ID, set during async_setup
_instance_id: str = ""


def set_instance_id(instance_id: str) -> None:
    """Cache the HA instance ID for PIN masking."""
    global _instance_id  # noqa: PLW0603
    _instance_id = instance_id


def mask_pin(pin: str | None, lock_entity_id: str) -> str:
    """Return a deterministic masked representation of a PIN for logging.

    Uses CRC32 salted with the HA instance ID and lock entity ID so the
    same PIN on the same lock always produces the same 8-char hex token.
    Different locks or HA instances produce different tokens, preventing
    cross-correlation.
    """
    if not pin:
        return "<empty>"
    salt = f"{_instance_id}:{lock_entity_id}:{pin}"
    return f"pin#{zlib.crc32(salt.encode()) & 0xFFFFFFFF:08x}"
