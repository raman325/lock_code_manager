"""Utility functions for lock_code_manager."""

from __future__ import annotations

import logging
import zlib

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TURN_OFF
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


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


async def async_disable_slot(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    config_entry_id: str,
    slot_num: int,
    *,
    reason: str | None = None,
    title: str = "Lock Code Manager: Slot Disabled",
    lock_name: str | None = None,
    lock_entity_id: str | None = None,
) -> bool:
    """Disable a slot via the enabled switch and optionally create a notification.

    Returns True if the switch was found and turned off, False otherwise.
    When reason is provided, a persistent notification is created with the
    given title.
    """
    enabled_entity_id = ent_reg.async_get_entity_id(
        SWITCH_DOMAIN,
        DOMAIN,
        f"{config_entry_id}|{slot_num}|{CONF_ENABLED}",
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
        async_create(
            hass,
            reason,
            title=title,
            notification_id=f"{DOMAIN}_{config_entry_id}_{slot_num}_slot_disabled",
        )

    return True
