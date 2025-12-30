"""Lock Code Manager Coordinators."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .exceptions import LockCodeManagerError

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, int | str]]):
    """Class to manage usercode updates."""

    def __init__(self, hass: HomeAssistant, lock: BaseLock, config_entry: Any) -> None:
        """Initialize the usercode update coordinator."""
        self._lock = lock
        self._drift_unsub: Callable[[], None] | None = None
        # Disable periodic polling when push updates are supported.
        # Polling is still used for initial load.
        update_interval = None if lock.supports_push else lock.usercode_scan_interval
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {lock.lock.entity_id}",
            update_method=self.async_get_usercodes,
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.data: dict[int, int | str] = {}

        # Set up drift detection timer for locks with hard_refresh_interval
        if lock.hard_refresh_interval:
            self._drift_unsub = async_track_time_interval(
                hass,
                self._async_drift_check,
                lock.hard_refresh_interval,
            )

    @property
    def lock(self) -> BaseLock:
        """Return the lock."""
        return self._lock

    @callback
    def push_update(self, updates: dict[int, int | str]) -> None:
        """
        Push one or more slot updates and notify listening entities.

        Args:
            updates: Dict mapping slot numbers to usercode values.
                     Single: {1: "1234"}
                     Bulk: {1: "1234", 2: "5678", 3: ""}

        """
        if not updates:
            return

        # Merge updates into existing data
        self.data.update(updates)

        # Notify all listening entities
        self.async_set_updated_data(self.data)

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Update usercodes."""
        try:
            return await self._lock.async_internal_get_usercodes()
        except LockCodeManagerError as err:
            # We can silently fail if we've never been able to retrieve data
            if not self.last_update_success:
                return {}
            raise UpdateFailed from err

    async def _async_drift_check(self, now: datetime) -> None:
        """
        Perform periodic drift detection.

        Hard refresh re-fetches codes from the lock to detect out-of-band changes
        (e.g., codes changed at the lock's keypad). If changes are detected,
        updates coordinator data and notifies listeners.
        """
        _LOGGER.debug(
            "Performing drift detection hard refresh for %s",
            self._lock.lock.entity_id,
        )
        try:
            new_data = await self._lock.async_internal_hard_refresh_codes()
        except LockCodeManagerError as err:
            _LOGGER.warning(
                "Drift detection hard refresh failed for %s: %s",
                self._lock.lock.entity_id,
                err,
            )
            return

        # Compare with current data and notify if changed
        if new_data != self.data:
            _LOGGER.debug(
                "Drift detected for %s, updating coordinator data",
                self._lock.lock.entity_id,
            )
            self.async_set_updated_data(new_data)

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and clean up resources."""
        if self._drift_unsub:
            self._drift_unsub()
            self._drift_unsub = None
        await super().async_shutdown()
