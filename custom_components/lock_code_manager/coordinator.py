"""Lock Code Manager Coordinators."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .exceptions import LockDisconnected
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, int | str]]):
    """Class to manage usercode updates."""

    def __init__(self, hass: HomeAssistant, lock: BaseLock) -> None:
        """Initialize the usercode update coordinator."""
        self._lock = lock
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {lock.lock.entity_id}",
            update_method=self.async_get_usercodes,
            update_interval=lock.usercode_scan_interval,
        )
        self.data: dict[int, int | str] = {}

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Update usercodes."""
        try:
            return await self._lock.async_internal_get_usercodes()
        except LockDisconnected as err:
            # We can silently fail if we've never been able to retrieve data
            if not self.data:
                return {}
            raise UpdateFailed from err
