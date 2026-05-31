"""Utility functions for lock_code_manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
import zlib

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TURN_OFF
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue

from .const import DOMAIN
from .domain.config import build_slot_unique_id

if TYPE_CHECKING:
    from .domain.coordinator import LockUsercodeUpdateCoordinator
    from .models import LockCodeManagerConfigEntry
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


def get_slot_coordinator(
    config_entry: LockCodeManagerConfigEntry,
    lock: BaseLock,
    slot_num: int,
) -> LockUsercodeUpdateCoordinator | None:
    """
    Return the lock's coordinator, or None if it is not yet available.

    When the coordinator is missing a warning is logged so the slot
    entities are simply skipped rather than raising during setup.
    """
    coordinator = lock.coordinator
    if coordinator is None:
        _LOGGER.warning(
            "%s (%s): Coordinator missing for lock %s when adding slot %s entities",
            config_entry.entry_id,
            config_entry.title,
            lock.lock.entity_id,
            slot_num,
        )
    return coordinator


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
