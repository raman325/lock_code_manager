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
from .credentials import CredentialAddress, CredentialType, pin_address
from .exceptions import LockCodeManagerError
from .models import SlotCredential
from .queries import get_entry_config
from .resilience import CircuitBreaker
from .util import per_lock_issue_id

if TYPE_CHECKING:
    from ..providers import BaseLock

_LOGGER = logging.getLogger(__name__)


def _checked(address: CredentialAddress) -> CredentialAddress:
    """
    Return ``address`` normalized, rejecting types the coordinator cannot serve.

    The coordinator manages Personal Identification Number credentials only,
    so another type is a programming error rather than a missing entry.
    Failing here keeps a future second credential type from silently reading
    and writing the PIN's storage.

    ``user_ref`` is coerced for the same reason ``_normalize_keys`` coerces:
    a slot number reaches entities as either ``int`` or ``str`` (see
    ``EntryConfig.has_slot``), and an uncoerced ``"1"`` would miss every
    ``int``-keyed entry -- ``is_verified`` would report True for a slot whose
    optimistic write is still unconfirmed.
    """
    if address.credential_type != CredentialType.PIN:
        raise ValueError(
            f"Only PIN credentials are addressable today, got {address.credential_type}"
        )
    return CredentialAddress(int(address.user_ref), address.credential_type)


class LockUsercodeUpdateCoordinator(
    DataUpdateCoordinator[dict[CredentialAddress, SlotCredential]]
):
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
        self.data: dict[CredentialAddress, SlotCredential] = {}
        # Slots awaiting confirmation, kept in lockstep with ``data``. A slot is
        # unverified only while an optimistic (ambiguous-but-treated-as-completed)
        # write awaits confirmation; every other source -- genuine push events,
        # polls, hard refreshes, and authoritative writes -- is verified.
        #
        # A set rather than a per-slot flag, because "verified" has no
        # representation of its own: membership is the whole state. Storing it as
        # ``dict[int, bool]`` admitted a third, redundant value -- an explicit
        # ``True`` that read identically to absence -- and every writer had to
        # remember which of the two to use. See the Phase 2 push-as-commit spec.
        self._unverified: set[CredentialAddress] = set()
        self._config_entry = config_entry
        self._lock_breaker = CircuitBreaker(
            BACKOFF_FAILURE_THRESHOLD,
            backoff_initial=timedelta(seconds=BACKOFF_INITIAL_SECONDS),
            backoff_max=timedelta(seconds=BACKOFF_MAX_SECONDS),
        )
        self._original_update_interval: timedelta | None = update_interval
        # Whether the lock has ever been reached successfully. A lock that has
        # never been reached is "not ready yet" (e.g. its integration is still
        # starting up after a Home Assistant restart), not "offline" -- so it
        # must not raise the lock_offline repair during the startup window.
        self._reached_once = False

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

    def desired_credential(self, address: CredentialAddress) -> SlotCredential:
        """
        Return the credential LCM wants at an address.

        Disabled slots and enabled-but-blank slots map to
        ``SlotCredential.empty()``; an enabled slot with a configured PIN
        maps to ``SlotCredential.known(pin)``.
        """
        slot_data = get_entry_config(self._config_entry).slot(
            _checked(address).user_ref
        )
        if not slot_data.get(CONF_ENABLED):
            return SlotCredential.empty()
        pin = slot_data.get(CONF_PIN)
        if not pin:
            return SlotCredential.empty()
        return SlotCredential.known(pin)

    @staticmethod
    def _normalize_keys(
        data: dict[Any, SlotCredential],
    ) -> dict[CredentialAddress, SlotCredential]:
        """
        Lift a provider's slot-keyed read into the address keyspace.

        Providers report what they found on the device, keyed by device slot,
        and every credential they report today is a Personal Identification
        Number. Raises ValueError/TypeError if a key cannot be cast to int.
        """
        return {pin_address(int(k)): v for k, v in data.items()}

    def _apply_read(
        self, observed: dict[CredentialAddress, SlotCredential]
    ) -> dict[CredentialAddress, SlotCredential]:
        """
        Resolve a genuine read (poll or hard refresh) against pending writes.

        A read is the dropped-push backstop for the verified-credential
        lifecycle: for a slot with an outstanding optimistic write, observing
        the slot present confirms our write -- keep the believed value and mark
        it verified. The one exception (mirroring ``BaseLock._confirm_slot``) is
        a *readable* observation of a different code: that is an external change
        racing our write, so take the observation rather than masking it with
        the believed value -- otherwise a drift refresh, whose whole purpose is
        to surface out-of-band changes, would silently overwrite one. Observing
        the slot still absent means the write has not landed yet, so keep waiting
        (stay unverified, pending intact). Slots with no pending write are
        genuine observations and are marked verified. See the Phase 2
        push-as-commit spec.
        """
        out: dict[CredentialAddress, SlotCredential] = {}
        for address, cred in observed.items():
            pending = self._lock._pending_writes.get(address)
            if pending is not None and cred.is_present:
                pin, _deadline = pending
                del self._lock._pending_writes[address]
                if cred.is_readable and cred.readable_pin != pin:
                    out[address] = cred
                else:
                    out[address] = SlotCredential.known(pin)
                self._unverified.discard(address)
            elif pending is not None:
                out[address] = cred
                self._unverified.add(address)
            else:
                out[address] = cred
                self._unverified.discard(address)
        # Keep the unverified set in lockstep with the read.
        self._unverified &= out.keys()
        return out

    def is_verified(self, address: CredentialAddress) -> bool:
        """
        Return whether the address's credential is a confirmed observation.

        Unlisted addresses are verified: an address is only unverified while
        an optimistic write awaits confirmation (push event or hard refresh).
        """
        return _checked(address) not in self._unverified

    @callback
    def mark_verified(self, address: CredentialAddress) -> None:
        """
        Drop an address from the unverified set.

        Called when a write is confirmed by the lock (an authoritative
        ``WriteResult.CONFIRMED``), so an address left unverified by a prior
        optimistic write on the same address cannot strand it.
        """
        self._unverified.discard(_checked(address))

    def credential(self, address: CredentialAddress) -> SlotCredential | None:
        """
        Return the credential the lock reports at an address, or ``None``.

        ``None`` means the lock has not reported anything for this address --
        distinct from ``SlotCredential.empty()``, which is a positive
        observation that the slot holds no code.
        """
        return self.data.get(_checked(address))

    def has_credential(self, address: CredentialAddress) -> bool:
        """Return whether the lock has reported anything at an address."""
        return _checked(address) in self.data

    def credentials_by_slot(
        self, credential_type: CredentialType = CredentialType.PIN
    ) -> dict[int, SlotCredential]:
        """
        Project the stored credentials of one type into a slot-keyed view.

        The websocket payload and the diagnostics output are contracts with
        things outside this integration -- the dashboard card reads one, and
        people paste the other into issue reports -- so both keep their
        slot-keyed shape while the storage underneath moves to addresses.
        Lossless while Personal Identification Number is the only type
        stored; when a second type arrives, those boundaries choose which
        type they are projecting instead of silently flattening both.
        """
        return {
            address.user_ref: credential
            for address, credential in self.data.items()
            if address.credential_type is credential_type
        }

    async def async_confirm_pending_writes(self) -> None:
        """
        Actively read the lock back to confirm outstanding optimistic writes.

        An ambiguous write (``WriteResult.OPTIMISTIC``) gets no confirming push
        on some stacks: node-zwave-js, for one, emits no ``credential
        added/modified`` event when its own post-write verification fails on a
        lock that reports codes back masked -- the event and the ``ERROR_UNKNOWN``
        result are mutually exclusive. Waiting for the hourly drift refresh would
        let the breaker suspend a slot whose code actually landed (~3 attempts in
        the 5-minute window, long before the hourly backstop). So the seam calls
        this immediately after recording an optimistic write.

        This is the order-independent confirmation path: it does not depend on
        receiving any event. A hard read observes the slot present-but-masked
        (LCM projects masked codes to ``unreadable`` rather than repeating the
        driver's ``userCode == codeData`` check) and ``_apply_read`` confirms it;
        a genuinely-absent slot stays pending and re-syncs on the next tick.

        A failed read is non-fatal and does not apply backoff: the slot stays
        pending and the sync tick reconciles it within the TTL.
        """
        if not self._lock._pending_writes:
            return
        try:
            new_data = self._apply_read(
                self._normalize_keys(
                    await self._lock.async_internal_hard_refresh_codes()
                )
            )
        except LockCodeManagerError as err:
            _LOGGER.debug(
                "On-demand confirmation read failed for %s: %s; leaving pending "
                "writes for the sync tick to reconcile",
                self._lock.lock.entity_id,
                err,
            )
            return
        except Exception:
            # The confirmation read is a best-effort backstop, never fatal: it
            # must not escape into the set seam and suspend the slot. The pending
            # write stays recorded and the sync tick reconciles it via the TTL.
            _LOGGER.exception(
                "Unexpected error during on-demand confirmation read for %s; "
                "leaving pending writes for the sync tick to reconcile",
                self._lock.lock.entity_id,
            )
            return
        # _apply_read already cleared pending + updated the unverified set in
        # place, so the confirmation takes effect even when the data is
        # unchanged; only the listener notification is gated on a real delta.
        if new_data != self.data:
            self.async_set_updated_data(new_data)

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

        # Record the pushed slots regardless of whether the value changed: an
        # optimistic re-push of the same value still flips the slot to
        # unverified.
        if optimistic:
            self._unverified |= normalized.keys()
        else:
            self._unverified -= normalized.keys()
        # Keep the unverified set in lockstep with data.
        self._unverified &= new_data.keys()

        if new_data == self.data:
            # Verification-only change: the sync layer reads ``is_verified``
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

        # Only a lock that was reached at least once can go "offline". A lock
        # that has never been reached is still coming up (e.g. its integration
        # is mid-startup after a HA restart, surfacing transient "not connected"
        # errors); raising lock_offline there produces a repair that is created
        # and then auto-cleared the moment the integration finishes loading.
        if (
            self._reached_once
            and self._lock_breaker.failure_count == POLL_FAILURE_ALERT_THRESHOLD
        ):
            async_create_issue(
                self.hass,
                DOMAIN,
                per_lock_issue_id("lock_offline", self._lock.lock.entity_id),
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
        # A successful reach proves the lock is (now) reachable; from here on a
        # later drop is a genuine outage that may raise lock_offline.
        self._reached_once = True
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
            per_lock_issue_id("lock_offline", self._lock.lock.entity_id),
        )

    async def async_get_usercodes(self) -> dict[CredentialAddress, SlotCredential]:
        """Fetch usercodes from the provider, normalize slot keys, and apply backoff handling."""
        try:
            data = await self._lock.async_internal_get_usercodes()
        except LockCodeManagerError as err:
            self._apply_backoff()
            # Don't swallow into {}: DataUpdateCoordinator records any return as
            # a success, so an empty return fakes a "recovered" while the lock
            # is still unreachable (#1268).
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

        # A successful hard refresh is a genuine reach -- mark it so a later
        # outage can raise lock_offline even if the lock's only successful
        # contact was via drift detection rather than a poll/push.
        self._reached_once = True

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
            await self._lock.async_internal_is_reachable()
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
