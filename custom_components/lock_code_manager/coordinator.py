"""Lock Code Manager Coordinators."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, int | str]]):
    """Class to manage usercode updates."""

    def __init__(self, hass: HomeAssistant, lock: BaseLock, config_entry: Any) -> None:
        """Initialize the usercode update coordinator."""
        self._lock = lock
        self._last_hard_refresh: datetime | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {lock.lock.entity_id}",
            update_method=self.async_get_usercodes,
            update_interval=lock.usercode_scan_interval,
            config_entry=config_entry,
        )
        self.data: dict[int, int | str] = {}

    @property
    def lock(self) -> BaseLock:
        """Return the lock."""
        return self._lock

    def _should_hard_refresh(self) -> bool:
        """Return True if a hard refresh should be performed."""
        # Refresh if never refreshed before or interval has elapsed
        return bool(hard_refresh_interval := self._lock.hard_refresh_interval) and (
            self._last_hard_refresh is None
            or dt_util.utcnow() - self._last_hard_refresh >= hard_refresh_interval
        )

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Update usercodes."""
        try:
            # Periodically perform a hard refresh to detect out-of-band changes
            if self._should_hard_refresh():
                _LOGGER.debug(
                    "Performing periodic hard refresh for %s",
                    self._lock.lock.entity_id,
                )
                await self._lock.async_internal_hard_refresh_codes()
                self._last_hard_refresh = dt_util.utcnow()

            return await self._lock.async_internal_get_usercodes()
        except HomeAssistantError as err:
            # We can silently fail if we've never been able to retrieve data
            if not self.last_update_success:
                return {}
            raise UpdateFailed from err
