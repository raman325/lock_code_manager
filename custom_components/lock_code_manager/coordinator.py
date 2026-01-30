"""Lock Code Manager Coordinators."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .exceptions import LockCodeManagerError, LockDisconnected

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

# Retry delay for failed sync operations
RETRY_DELAY = timedelta(seconds=10)


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, int | str]]):
    """Class to manage usercode updates."""

    def __init__(self, hass: HomeAssistant, lock: BaseLock, config_entry: Any) -> None:
        """Initialize the usercode update coordinator."""
        self._lock = lock
        self._drift_unsub: Callable[[], None] | None = None
        self._connection_unsub: Callable[[], None] | None = None
        # Sync state tracking: slot_num -> is_synced (None = unknown)
        self._sync_state: dict[int, bool] = {}
        # Pending retry callbacks: slot_num -> cancel function
        self._pending_retries: dict[int, Callable[[], None]] = {}
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

        # Merge updates into a new dict to ensure proper change detection
        # (avoids passing the same object reference to listeners)
        self.async_set_updated_data({**self.data, **updates})

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
        # Skip if we haven't successfully loaded initial data yet
        if not self.last_update_success:
            return

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

        # Retry push subscription if supported but not yet subscribed
        # (handles case where initial setup failed but lock is now available)
        if self._lock.supports_push:
            self._lock.subscribe_push_updates()

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
            await self._lock.async_internal_is_connection_up()
        except LockCodeManagerError as err:
            _LOGGER.debug(
                "Connection check failed for %s: %s", self._lock.lock.entity_id, err
            )

    # =========================================================================
    # Sync Operation Methods
    # =========================================================================

    def get_sync_state(self, slot_num: int) -> bool | None:
        """Get sync state for a slot.

        Returns:
            True if slot is synced, False if out of sync, None if unknown.

        """
        return self._sync_state.get(slot_num)

    @callback
    def mark_synced(self, slot_num: int) -> None:
        """Mark slot as synced.

        Called by binary sensor when it verifies the slot is in sync.
        """
        if self._sync_state.get(slot_num) is not True:
            self._sync_state[slot_num] = True
            self._cancel_retry(slot_num)
            self.async_set_updated_data(self.data)

    @callback
    def mark_out_of_sync(self, slot_num: int) -> None:
        """Mark slot as out of sync.

        Called by binary sensor when it detects slot is out of sync on initial load.
        This does NOT trigger a sync operation - use async_request_sync for that.
        """
        if self._sync_state.get(slot_num) is not False:
            self._sync_state[slot_num] = False
            self.async_set_updated_data(self.data)

    async def async_request_sync(
        self,
        slot_num: int,
        operation: Literal["set", "clear"],
        usercode: str | None = None,
        name: str | None = None,
    ) -> bool:
        """Request sync operation for a slot.

        Args:
            slot_num: The slot number to sync.
            operation: "set" to set usercode, "clear" to clear it.
            usercode: The usercode to set (required for "set" operation).
            name: Optional name for the slot.

        Returns:
            True if operation succeeded, False if it failed (retry scheduled).

        """
        # Cancel any pending retry for this slot - new request takes precedence
        self._cancel_retry(slot_num)

        # Mark as out of sync and notify listeners
        self._sync_state[slot_num] = False
        self.async_set_updated_data(self.data)

        _LOGGER.debug(
            "Sync requested for %s slot %s: %s",
            self._lock.lock.entity_id,
            slot_num,
            operation,
        )

        try:
            if operation == "set":
                if usercode is None:
                    raise ValueError("usercode is required for 'set' operation")
                await self._lock.async_internal_set_usercode(slot_num, usercode, name)
            else:
                await self._lock.async_internal_clear_usercode(slot_num)

            # Refresh to verify the operation completed
            await self.async_request_refresh()
            return True

        except LockDisconnected as err:
            _LOGGER.debug(
                "Sync failed for %s slot %s (%s): %s - scheduling retry",
                self._lock.lock.entity_id,
                slot_num,
                operation,
                err,
            )
            self._schedule_retry(slot_num, operation, usercode, name)
            return False

        except UpdateFailed as err:
            _LOGGER.debug(
                "Sync verification failed for %s slot %s: %s - scheduling retry",
                self._lock.lock.entity_id,
                slot_num,
                err,
            )
            self._schedule_retry(slot_num, operation, usercode, name)
            return False

    def _schedule_retry(
        self,
        slot_num: int,
        operation: Literal["set", "clear"],
        usercode: str | None,
        name: str | None,
    ) -> None:
        """Schedule retry for failed sync.

        Retries infinitely until:
        - Operation succeeds, OR
        - A new sync request comes in (replaces pending operation)

        This ensures the latest requested state always takes precedence.
        E.g., if set fails and user disables the slot, we switch to clear.
        """
        self._cancel_retry(slot_num)

        _LOGGER.debug(
            "Scheduling retry for %s slot %s in %ss",
            self._lock.lock.entity_id,
            slot_num,
            RETRY_DELAY.total_seconds(),
        )

        @callback
        def _retry_callback(_now: datetime) -> None:
            """Handle retry callback."""
            self._pending_retries.pop(slot_num, None)
            self.hass.async_create_task(
                self.async_request_sync(slot_num, operation, usercode, name),
                f"Retry sync for {self._lock.lock.entity_id} slot {slot_num}",
            )

        self._pending_retries[slot_num] = async_call_later(
            self.hass,
            RETRY_DELAY.total_seconds(),
            _retry_callback,
        )

    @callback
    def _cancel_retry(self, slot_num: int) -> None:
        """Cancel pending retry for slot."""
        if unsub := self._pending_retries.pop(slot_num, None):
            unsub()

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and clean up resources."""
        if self._drift_unsub:
            self._drift_unsub()
            self._drift_unsub = None
        if self._connection_unsub:
            self._connection_unsub()
            self._connection_unsub = None
        await super().async_shutdown()
