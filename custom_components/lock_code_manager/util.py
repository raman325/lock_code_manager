"""Utility functions for lock_code_manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any
import zlib

from homeassistant.components.persistent_notification import async_create
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TURN_OFF
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later

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


class OneShotRetry:
    """Schedule a single delayed retry of an async target.

    Idempotent: calling schedule() while a retry is already pending is a no-op.
    The `active` property is True while the target coroutine is executing,
    allowing callers to distinguish retry-driven invocations.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        delay: timedelta,
        target: Callable[[], Any],
        name: str,
    ) -> None:
        """Initialize the retry helper."""
        self._hass = hass
        self._delay = delay
        self._target = target
        self._name = name
        self._unsub: Callable[[], None] | None = None
        self._active = False

    @property
    def active(self) -> bool:
        """Return True while the retry callback is executing."""
        return self._active

    @property
    def pending(self) -> bool:
        """Return True if a retry is scheduled but not yet executing."""
        return self._unsub is not None

    def schedule(self) -> None:
        """Schedule a retry if one is not already pending."""
        if self._unsub is not None:
            return

        _LOGGER.debug(
            "Scheduling retry for %s in %ss",
            self._name,
            self._delay.total_seconds(),
        )
        self._unsub = async_call_later(
            self._hass,
            self._delay.total_seconds(),
            self._fire,
        )

    @callback
    def cancel(self) -> None:
        """Cancel any pending retry."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._active = False

    async def _fire(self, _now: Any) -> None:
        """Execute the retry callback."""
        self._unsub = None
        self._active = True
        try:
            result = self._target()
            if asyncio.iscoroutine(result):
                await result
        finally:
            self._active = False
