"""Manages the slot->code mapping for a single lock.

Stores ALL slots (managed and unmanaged). See ARCHITECTURE.md for the full data flow.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BACKOFF_FAILURE_THRESHOLD,
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    CONF_SLOTS,
    DOMAIN,
    POLL_FAILURE_ALERT_THRESHOLD,
)
from .data import get_entry_data
from .exceptions import LockCodeManagerError
from .models import SlotCode

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, str | SlotCode]]):
    """Class to manage usercode updates."""

    def __init__(self, hass: HomeAssistant, lock: BaseLock, config_entry: Any) -> None:
        """Initialize the usercode update coordinator."""
        self._lock = lock
        self._drift_unsub: Callable[[], None] | None = None
        self._connection_unsub: Callable[[], None] | None = None
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
        self.data: dict[int, str | SlotCode] = {}
        self._config_entry = config_entry
        self._consecutive_failures: int = 0
        self._original_update_interval: timedelta | None = update_interval

        # Set up drift detection timer for locks with hard_refresh_interval
        if lock.hard_refresh_interval:
            self._drift_unsub = async_track_time_interval(
                hass,
                self._async_drift_check,
                lock.hard_refresh_interval,
            )

        if lock.connection_check_interval:
            # Periodic connection checks drive reconnect handling for non-push providers.
            self._connection_unsub = async_track_time_interval(
                hass,
                self._async_connection_check,
                lock.connection_check_interval,
            )

    @property
    def lock(self) -> BaseLock:
        """Return the lock."""
        return self._lock

    def get_expected_pin(self, slot_num: int) -> str | None:
        """Return configured PIN for a slot, or None if disabled/unconfigured."""
        slot_data = get_entry_data(self._config_entry, CONF_SLOTS, {}).get(
            str(slot_num), {}
        )
        if not slot_data.get(CONF_ENABLED):
            return None
        return slot_data.get(CONF_PIN) or None

    def slot_expects_pin(self, slot_num: int) -> bool:
        """Return whether LCM expects a PIN on this slot (enabled with PIN)."""
        return self.get_expected_pin(slot_num) is not None

    @callback
    def push_update(self, updates: dict[int, str | SlotCode]) -> None:
        """Push one or more slot updates and notify listening entities."""
        if not updates:
            return

        new_data = {**self.data, **updates}
        # Skip update if data hasn't actually changed to avoid redundant logging
        # and unnecessary listener notifications
        if new_data == self.data:
            return

        # A successful push update proves the lock is reachable, so reset
        # backoff to re-enable drift checks and normal polling.
        self._reset_backoff()

        self.async_set_updated_data(new_data)

    def _apply_backoff(self) -> None:
        """Increment failure counter and apply exponential backoff if threshold met."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= BACKOFF_FAILURE_THRESHOLD:
            backoff_secs = min(
                BACKOFF_INITIAL_SECONDS
                * 2 ** (self._consecutive_failures - BACKOFF_FAILURE_THRESHOLD),
                BACKOFF_MAX_SECONDS,
            )
            if self._original_update_interval is not None:
                new_interval = timedelta(seconds=backoff_secs)
                if new_interval != self.update_interval:  # type: ignore[has-type]
                    self.update_interval = new_interval
                    _LOGGER.warning(
                        "Update failed %d consecutive times for %s, "
                        "backing off polling interval to %ds",
                        self._consecutive_failures,
                        self._lock.lock.entity_id,
                        backoff_secs,
                    )
            else:
                _LOGGER.warning(
                    "Update failed %d consecutive times for %s, "
                    "suppressing drift checks until recovery",
                    self._consecutive_failures,
                    self._lock.lock.entity_id,
                )

        if self._consecutive_failures == POLL_FAILURE_ALERT_THRESHOLD:
            async_create_issue(
                self.hass,
                DOMAIN,
                f"lock_offline_{self._lock.lock.entity_id}",
                is_fixable=False,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="lock_offline",
                translation_placeholders={
                    "lock_entity_id": self._lock.lock.entity_id,
                },
            )

    def _reset_backoff(self) -> None:
        """Reset failure counter and restore original update interval."""
        if self._consecutive_failures > 0:
            _LOGGER.info(
                "Lock %s recovered after %d consecutive failures",
                self._lock.lock.entity_id,
                self._consecutive_failures,
            )
            self._consecutive_failures = 0
            if self._original_update_interval is not None:
                self.update_interval = self._original_update_interval
            # Always attempt to delete — async_delete_issue is a no-op if
            # the issue doesn't exist, so no need to track whether it was created.
            async_delete_issue(
                self.hass,
                DOMAIN,
                f"lock_offline_{self._lock.lock.entity_id}",
            )

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Update usercodes."""
        try:
            data = await self._lock.async_internal_get_usercodes()
        except LockCodeManagerError as err:
            self._apply_backoff()
            # We can silently fail if we've never been able to retrieve data
            if not self.last_update_success:
                return {}
            raise UpdateFailed from err

        self._reset_backoff()
        return data

    async def _async_drift_check(self, now: datetime) -> None:
        """
        Perform periodic drift detection.

        Hard refresh re-fetches codes from the lock to detect out-of-band changes
        (e.g., codes changed at the lock's keypad). If changes are detected,
        updates coordinator data and notifies listeners.
        """
        # Skip if we haven't successfully loaded initial data yet
        if not self.last_update_success:
            return

        if self._consecutive_failures >= BACKOFF_FAILURE_THRESHOLD:
            _LOGGER.debug(
                "Skipping drift check for %s (in backoff after %d failures)",
                self._lock.lock.entity_id,
                self._consecutive_failures,
            )
            return

        _LOGGER.debug(
            "Performing drift detection hard refresh for %s",
            self._lock.lock.entity_id,
        )
        try:
            new_data = await self._lock.async_internal_hard_refresh_codes()
        except LockCodeManagerError as err:
            self._apply_backoff()
            _LOGGER.warning(
                "Drift detection hard refresh failed for %s: %s",
                self._lock.lock.entity_id,
                err,
            )
            return

        # Push subscription retry is handled by BaseLock's OneShotRetry
        # and the config entry state listener — no need to retry here.

        # Compare with current data and notify if changed
        if new_data != self.data:
            _LOGGER.debug(
                "Drift detected for %s, updating coordinator data",
                self._lock.lock.entity_id,
            )
            self.async_set_updated_data(new_data)

    async def _async_connection_check(self, now: datetime) -> None:
        """Poll connection state so providers can resubscribe on reconnect."""
        try:
            await self._lock.async_internal_is_integration_connected()
        except LockCodeManagerError as err:
            _LOGGER.debug(
                "Connection check failed for %s: %s", self._lock.lock.entity_id, err
            )

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and clean up resources."""
        if self._drift_unsub:
            self._drift_unsub()
            self._drift_unsub = None
        if self._connection_unsub:
            self._connection_unsub()
            self._connection_unsub = None
        await super().async_shutdown()
