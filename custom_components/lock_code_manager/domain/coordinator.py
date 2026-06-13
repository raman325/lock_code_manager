"""
Manages the slot->code mapping for a single lock.

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

from ..const import (
    BACKOFF_FAILURE_THRESHOLD,
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    DOMAIN,
    POLL_FAILURE_ALERT_THRESHOLD,
)
from .exceptions import LockCodeManagerError
from .models import SlotCredential
from .queries import get_entry_config
from .resilience import CircuitBreaker

if TYPE_CHECKING:
    from ..providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, SlotCredential]]):
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
        self.data: dict[int, SlotCredential] = {}
        # Per-slot "verified" flag, kept in lockstep with ``data``. A slot is
        # unverified only while an optimistic (ambiguous-but-treated-as-completed)
        # write awaits confirmation; every other source -- genuine push events,
        # polls, hard refreshes, and authoritative writes -- is verified. Absent
        # slots read as verified, so poll/cloud providers (which never push an
        # optimistic update) are unaffected. See the Phase 2 push-as-commit spec.
        self._verified: dict[int, bool] = {}
        self._config_entry = config_entry
        self._lock_breaker = CircuitBreaker(
            BACKOFF_FAILURE_THRESHOLD,
            backoff_initial=timedelta(seconds=BACKOFF_INITIAL_SECONDS),
            backoff_max=timedelta(seconds=BACKOFF_MAX_SECONDS),
        )
        self._original_update_interval: timedelta | None = update_interval

        # Set up drift detection timer for locks with hard_refresh_interval
        if lock.hard_refresh_interval:
            self._drift_unsub = async_track_time_interval(
                hass,
                self._async_drift_check,
                lock.hard_refresh_interval,
                cancel_on_shutdown=True,
            )

        if lock.connection_check_interval:
            self._connection_unsub = async_track_time_interval(
                hass,
                self._async_connection_check,
                lock.connection_check_interval,
                cancel_on_shutdown=True,
            )

    @property
    def lock(self) -> BaseLock:
        """Return the lock."""
        return self._lock

    def desired_credential(self, slot_num: int) -> SlotCredential:
        """
        Return the credential LCM wants on a slot.

        Disabled slots and enabled-but-blank slots map to
        ``SlotCredential.empty()``; an enabled slot with a configured PIN
        maps to ``SlotCredential.known(pin)``.
        """
        slot_data = get_entry_config(self._config_entry).slot(slot_num)
        if not slot_data.get(CONF_ENABLED):
            return SlotCredential.empty()
        pin = slot_data.get(CONF_PIN)
        if not pin:
            return SlotCredential.empty()
        return SlotCredential.known(pin)

    @staticmethod
    def _normalize_keys(
        data: dict[Any, SlotCredential],
    ) -> dict[int, SlotCredential]:
        """Coerce slot keys to ``int``. Raises ValueError/TypeError if a key cannot be cast."""
        return {int(k): v for k, v in data.items()}

    def _apply_read(
        self, observed: dict[int, SlotCredential]
    ) -> dict[int, SlotCredential]:
        """
        Resolve a genuine read (poll or hard refresh) against pending writes.

        A read is the dropped-push backstop for the verified-credential
        lifecycle: for a slot with an outstanding optimistic write, observing
        the slot present confirms our write -- keep the believed value and mark
        it verified. Observing it still absent means the write has not landed
        yet, so keep waiting (stay unverified, pending intact). Slots with no
        pending write are genuine observations and are marked verified. See the
        Phase 2 push-as-commit spec.
        """
        out: dict[int, SlotCredential] = {}
        for slot, cred in observed.items():
            pending = self._lock._pending_writes.get(slot)
            if pending is not None and cred.is_present:
                pin, _deadline = pending
                del self._lock._pending_writes[slot]
                out[slot] = SlotCredential.known(pin)
                self._verified[slot] = True
            elif pending is not None:
                out[slot] = cred
                self._verified[slot] = False
            else:
                out[slot] = cred
                self._verified.pop(slot, None)
        # Keep the verified map in lockstep with the read.
        self._verified = {
            slot: flag for slot, flag in self._verified.items() if slot in out
        }
        return out

    def is_verified(self, slot: int) -> bool:
        """
        Return whether the slot's credential is a confirmed observation.

        Absent slots default to verified: a slot is only unverified while an
        optimistic write awaits confirmation (push event or hard refresh).
        """
        return self._verified.get(slot, True)

    @callback
    def push_update(
        self, updates: dict[int, SlotCredential], *, optimistic: bool = False
    ) -> None:
        """
        Push one or more slot updates and notify listening entities.

        ``optimistic=True`` marks the pushed slots unverified (an ambiguous
        write we are treating as completed but have not yet confirmed). The
        default, ``False``, marks them verified -- every existing caller keeps
        today's behavior.
        """
        if not updates:
            return

        normalized = self._normalize_keys(updates)
        new_data = {**self.data, **normalized}
        verified = not optimistic

        # Record the verified flag for the pushed slots regardless of whether
        # the value changed: an optimistic re-push of the same value still
        # flips the slot to unverified.
        for slot in normalized:
            self._verified[slot] = verified
        # Keep the verified map in lockstep with data.
        self._verified = {
            slot: flag for slot, flag in self._verified.items() if slot in new_data
        }

        if new_data == self.data:
            # Verified-flag-only change: the sync layer reads ``is_verified``
            # directly on its next tick, and entities don't render the flag, so
            # there's nothing to notify and no reachability proof (no new data).
            return

        # A successful push update proves the lock is reachable, so reset
        # backoff to re-enable drift checks and normal polling.
        self._reset_backoff()

        self.async_set_updated_data(new_data)

    def note_connectivity_failure(self) -> None:
        """
        Record a connectivity failure observed outside the poll path.

        Lets the sync layer feed set/clear transport failures into the same
        lock breaker that polling uses, so "lock is unreachable" converges
        from both code paths. When this is what trips the breaker, kick a
        refresh so a provider that does not normally poll (push) starts
        probing for recovery.
        """
        was_tripped = self._lock_breaker.tripped
        self._apply_backoff()
        if self._lock_breaker.tripped and not was_tripped:
            self.hass.async_create_task(self.async_request_refresh())

    def _apply_backoff(self) -> None:
        """Record a connectivity failure and poll on a backoff until recovery."""
        self._lock_breaker.record_failure()
        if self._lock_breaker.tripped:
            # Poll on the backoff interval until a successful update clears the
            # breaker. Push providers normally do not poll, but while the lock
            # is unreachable we poll to probe for recovery -- otherwise a push
            # provider whose writes fail (with no push arriving) could stay
            # suspended indefinitely.
            new_interval = self._lock_breaker.backoff_delay
            if new_interval != self.update_interval:  # type: ignore[has-type]
                self.update_interval = new_interval
                _LOGGER.warning(
                    "Update failed %d consecutive times for %s, "
                    "polling every %ds until it recovers",
                    self._lock_breaker.failure_count,
                    self._lock.lock.entity_id,
                    new_interval.total_seconds(),
                )

        if self._lock_breaker.failure_count == POLL_FAILURE_ALERT_THRESHOLD:
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

    @property
    def unreachable(self) -> bool:
        """Return whether the lock is currently considered unreachable."""
        return self._lock_breaker.tripped

    def _reset_backoff(self) -> None:
        """Reset the lock breaker and restore the original update interval."""
        if self._lock_breaker.failure_count > 0:
            _LOGGER.info(
                "Lock %s recovered after %d consecutive failures",
                self._lock.lock.entity_id,
                self._lock_breaker.failure_count,
            )
            self._lock_breaker.reset()
            # Restore the normal cadence. For push providers this is None,
            # which stops the recovery probe polling.
            self.update_interval = self._original_update_interval  # type: ignore[assignment]
        # Unconditionally clear lock_offline issue on any successful poll.
        # Runs outside the if-block so it also clears persisted issues that
        # survive HA restarts (where the breaker resets to 0).
        async_delete_issue(
            self.hass,
            DOMAIN,
            f"lock_offline_{self._lock.lock.entity_id}",
        )

    async def async_get_usercodes(self) -> dict[int, SlotCredential]:
        """Fetch usercodes from the provider, normalize slot keys, and apply backoff handling."""
        try:
            data = await self._lock.async_internal_get_usercodes()
        except LockCodeManagerError as err:
            self._apply_backoff()
            # During cold start (before the first successful poll), do not
            # raise UpdateFailed. That would fail the initial refresh and
            # keep coordinator-backed entities unavailable until a
            # successful poll completes.
            if not self.last_update_success:
                return {}
            raise UpdateFailed from err

        self._reset_backoff()
        return self._apply_read(self._normalize_keys(data))

    async def _async_drift_check(self, now: datetime) -> None:
        """Perform a hard refresh to detect out-of-band code changes."""
        if not self.last_update_success:
            return

        if self._lock_breaker.tripped:
            _LOGGER.debug(
                "Skipping drift check for %s (in backoff after %d failures)",
                self._lock.lock.entity_id,
                self._lock_breaker.failure_count,
            )
            return

        _LOGGER.debug(
            "Performing drift detection hard refresh for %s",
            self._lock.lock.entity_id,
        )
        try:
            new_data = self._apply_read(
                self._normalize_keys(
                    await self._lock.async_internal_hard_refresh_codes()
                )
            )
        except LockCodeManagerError as err:
            self._apply_backoff()
            _LOGGER.warning(
                "Drift detection hard refresh failed for %s: %s",
                self._lock.lock.entity_id,
                err,
            )
            return

        # Push subscription retry is handled by the config entry state
        # listener and connection transition handler — no need to retry here.

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
