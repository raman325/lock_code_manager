"""
Manages the slot->code mapping for a single lock.

Stores ALL slots (managed and unmanaged). See ARCHITECTURE.md for the full data flow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from homeassistant.const import CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
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
    CONFIRM_READ_INTERVAL,
    DOMAIN,
    PENDING_WRITE_TTL,
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


class PendingWrite(NamedTuple):
    """
    A write the lock has not yet been seen to hold.

    ``believed`` says whether ``data`` carries the written PIN on the strength
    of the write alone (an optimistic write pushes its value before anything
    confirms it). Either way the address is unverified until a read or push
    shows the slot present, and given up on at ``deadline``.
    """

    pin: str
    written_at: float
    believed: bool

    @property
    def deadline(self) -> float:
        """Monotonic time after which an absent read means the write did not land."""
        return self.written_at + PENDING_WRITE_TTL


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
        #
        # A bridged provider may not know the answer yet: it derives push
        # support from discovery data that arrives on the broker's schedule,
        # so a lock constructed before that lands looks like a poll lock.
        # ``note_push_capability`` is the one place that answer is revisited.
        self._is_push = lock.supports_push
        update_interval = None if self._is_push else lock.usercode_scan_interval
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {lock.lock.entity_id}",
            update_method=self.async_get_usercodes,
            update_interval=update_interval,
            config_entry=config_entry,
        )
        self.data: dict[CredentialAddress, SlotCredential] = {}
        # Writes the lock has not yet been seen to hold, by address. This is the
        # whole of "unverified": an address is verified exactly when nothing is
        # pending against it. Confirmed by a read or push showing the slot
        # present; given up on by the first read past the deadline that still
        # does not -- which is also the only way a pending write ends without
        # confirmation, so nothing outside a completed read ever has to guess.
        # Keyed by CredentialAddress, NOT by device slot: one slot can hold a
        # credential of each type, and a slot-keyed map would let two addresses
        # sharing a slot consume each other's entry.
        self._pending: dict[CredentialAddress, PendingWrite] = {}
        # Addresses whose pending write failed -- not seen by the deadline, or
        # displaced by a different code -- awaiting the sync tick's one charge
        # to the slot breaker. Consumed by ``take_failed_write``.
        self._failed_writes: set[CredentialAddress] = set()
        # The single confirmation look for this lock: a task for the immediate
        # first look, then a timer while anything stays pending. One per
        # coordinator, not per write: N pending slots on one lock are one read
        # apart, not N.
        self._confirm_task: asyncio.Task[None] | None = None
        self._confirm_unsub: Callable[[], None] | None = None
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

        For an address with a write pending, observing the slot present
        confirms the write: keep the written value, verified. The one
        exception (mirroring ``observe_push``) is a *readable* observation of
        a different code: the slot holds something else, so the write did not
        take -- take the observation, otherwise a drift refresh, whose whole
        purpose is to surface out-of-band changes, would silently overwrite
        one, and count the write as failed. Observing the slot still absent
        before the deadline means the write has not landed yet: keep waiting.
        Absent at or past the deadline means it is not going to: give the
        write up, take the observation, and count it failed. Either failure
        leaves the address for the sync tick to charge once. Addresses with
        nothing pending are the lock's word as read.
        """
        now = time.monotonic()
        out: dict[CredentialAddress, SlotCredential] = {}
        for address, cred in observed.items():
            pending = self._pending.get(address)
            if pending is None:
                out[address] = cred
            elif cred.is_present:
                del self._pending[address]
                if cred.is_readable and cred.readable_pin != pending.pin:
                    self._failed_writes.add(address)
                    out[address] = cred
                else:
                    out[address] = SlotCredential.known(pending.pin)
            elif now >= pending.deadline:
                del self._pending[address]
                self._failed_writes.add(address)
                out[address] = cred
            elif pending.believed:
                # Still waiting, and the write was believed: keep showing it
                # rather than flickering to the read and back on confirmation.
                out[address] = SlotCredential.known(pending.pin)
            else:
                out[address] = cred
        return out

    def is_verified(self, address: CredentialAddress) -> bool:
        """
        Return whether the address's credential is the lock's word.

        False exactly while a write is pending against the address: an
        optimistic write anywhere, or a confirmed write on a polled lock,
        until a read or push shows the slot present -- or the deadline passes
        and the write is given up on.
        """
        return _checked(address) not in self._pending

    def pending_write(self, address: CredentialAddress) -> PendingWrite | None:
        """Return the write pending against ``address``, if any."""
        return self._pending.get(_checked(address))

    def has_pending_write(self, address: CredentialAddress) -> bool:
        """Return whether a write is pending against ``address``."""
        return _checked(address) in self._pending

    @callback
    def record_write(
        self, address: CredentialAddress, pin: str, *, believed: bool
    ) -> None:
        """
        Record a write the lock has not yet been seen to hold, and go look.

        ``believed`` pushes the written value into ``data`` on the strength of
        the write alone -- what an optimistic write does, so the code sensor
        shows the PIN the driver most likely stored. A polled lock's confirmed
        write is recorded without it: the cloud accepting the write is not
        the lock reporting it, and the read that follows is what will say.

        Starts the confirmation look if one is not already under way. The
        first look is an immediate task, so a lock that already reports the
        write settles on the next sync tick; later looks are a timer,
        CONFIRM_READ_INTERVAL apart, until nothing is pending. A newer write
        to the same address replaces the record, so the read confirms the PIN
        actually sent last.
        """
        if self._shutdown_requested:
            # A write returning after unload has no one left to confirm it
            # for; recording it would start a look against a torn-down lock.
            return
        checked = _checked(address)
        self._pending[checked] = PendingWrite(pin, time.monotonic(), believed)
        self._failed_writes.discard(checked)
        if believed:
            new_data = {**self.data, checked: SlotCredential.known(pin)}
            if new_data != self.data:
                self.async_set_updated_data(new_data)
        self._start_look()

    @callback
    def drop_pending(self, address: CredentialAddress) -> None:
        """
        Forget a pending write without judging it.

        For a write that is no longer wanted -- the slot was cleared, or the
        desired PIN changed while it was outstanding. Not a failure, so it
        leaves nothing for the tick to charge.
        """
        checked = _checked(address)
        self._pending.pop(checked, None)
        self._failed_writes.discard(checked)

    @callback
    def take_failed_write(self, address: CredentialAddress) -> bool:
        """
        Return, once, whether a pending write to ``address`` failed.

        Set when a read past the deadline still did not show the write, or
        when a read showed the slot holding a different code instead; cleared
        by this call. The sync tick charges the slot breaker exactly once per
        write the lock did not keep.
        """
        checked = _checked(address)
        if checked in self._failed_writes:
            self._failed_writes.discard(checked)
            return True
        return False

    @callback
    def observe_push(
        self, address: CredentialAddress, observed: SlotCredential
    ) -> None:
        """
        Resolve a push event for one address against any pending write.

        The push-side twin of ``_apply_read``, with one deliberate difference:
        a push is the lock speaking now, so an absent push ends the pending
        write and is taken as the lock's word rather than waited out -- and
        counts the write failed. A present push confirms the write (keeping
        the written value), unless a readable different code shows the slot
        holds something else, which counts it failed as well.
        """
        checked = _checked(address)
        pending = self._pending.pop(checked, None)
        failed = False
        if pending is None:
            value = observed
        elif observed.is_present and not (
            observed.is_readable and observed.readable_pin != pending.pin
        ):
            value = SlotCredential.known(pending.pin)
        else:
            self._failed_writes.add(checked)
            failed = True
            value = observed
        before = self.data
        self.push_update({checked.user_ref: value})
        if failed and self.data is before:
            # Same data, failed write: let the sync layer judge it now.
            self.async_update_listeners()

    @callback
    def _start_look(self) -> None:
        """
        Start the confirmation look now, unless one is already in flight.

        A timer waiting for the next look is pulled forward instead: the write
        that just returned gets its immediate look, and the chain stays one.
        """
        if self._confirm_task is not None:
            return
        if self._confirm_unsub is not None:
            self._confirm_unsub()
            self._confirm_unsub = None
        # Not eager: record_write is called from inside the provider's own
        # write path, and an eagerly started read would reenter the provider
        # before that write has returned.
        self._confirm_task = self.hass.async_create_task(
            self._async_confirmation_look(),
            f"{DOMAIN} confirmation read for {self._lock.lock.entity_id}",
            eager_start=False,
        )

    async def _async_confirmation_look(self) -> None:
        """Look once; hand off to the timer while anything is still pending."""
        try:
            await self.async_confirm_pending_writes()
        finally:
            self._confirm_task = None
        if self._pending:
            self._confirm_unsub = async_call_later(
                self.hass, CONFIRM_READ_INTERVAL, self._async_confirmation_timer
            )

    @callback
    def _async_confirmation_timer(self, _now: datetime) -> None:
        """Run the next look as the tracked task, so shutdown can cancel it too."""
        self._confirm_unsub = None
        self._start_look()

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
        Read the lock back to settle the writes pending against it.

        The order-independent confirmation path: it does not depend on a push
        arriving, which some stacks never send for an ambiguous write and a
        polled lock never sends at all. A polled lock is read through its
        ordinary read, scoped to the managed slots plus every pending address,
        so a write to a slot nothing manages is still asked about; a push
        provider is hard-refreshed, since its ordinary path is the cache the
        write may not have reached.

        A failed read is not the lock's word: it is non-fatal, touches no
        breaker, and the write stays pending for the next look. A read that
        fails once a pending write is past its deadline gives that write up
        all the same: the lock has had the time to live to be seen holding it,
        and was not.
        """
        if not self._pending:
            return
        try:
            if self._is_push:
                # Only the slots with a write pending need re-reading from the
                # device; on a lossy link that is what lets the read complete.
                raw = await self._lock.async_internal_hard_refresh_codes(
                    {address.user_ref for address in self._pending}
                )
            else:
                raw = await self._lock.async_internal_get_usercodes(
                    self._lock.managed_slots
                    | {address.user_ref for address in self._pending}
                )
            # Replace, as the poll and drift paths do: the read names every
            # managed and pending slot plus whatever else the lock holds, so a
            # slot it no longer reports is one the lock no longer has.
            failed_before = len(self._failed_writes)
            new_data = self._apply_read(self._normalize_keys(raw))
            # A completed read that does not name a pending address is the
            # lock not holding it: a poller's read is scoped to name every
            # pending slot, and a push provider's refresh re-reads the pending
            # slots from the device and then projects everything the lock
            # holds. So it is judged like an absent slot: waited for until
            # the deadline, then given up.
            self._fail_overdue(
                [address for address in self._pending if address not in new_data]
            )
        except LockCodeManagerError as err:
            self._give_up_overdue(err)
            return
        except Exception as err:
            _LOGGER.exception(
                "Unexpected error during confirmation read for %s",
                self._lock.lock.entity_id,
            )
            self._give_up_overdue(err)
            return
        if new_data != self.data:
            self.async_set_updated_data(new_data)
        elif len(self._failed_writes) > failed_before:
            # The data did not move, but a write just failed against it: a
            # slot left in sync must judge that now, not be charged for it by
            # some later, unrelated sync.
            self.async_update_listeners()

    @callback
    def _fail_overdue(
        self, addresses: Iterable[CredentialAddress]
    ) -> list[CredentialAddress]:
        """Fail every given pending write that is past its deadline; return them."""
        now = time.monotonic()
        overdue = [
            address for address in addresses if now >= self._pending[address].deadline
        ]
        for address in overdue:
            del self._pending[address]
            self._failed_writes.add(address)
        return overdue

    @callback
    def _give_up_overdue(self, err: BaseException) -> None:
        """
        After a failed read, give up every pending write past its deadline.

        Said at info with the cause when anything is given up, so the sync
        tick's later warning about an unconfirmed write has a reason on
        record; a failure that leaves everything still waiting is routine.
        """
        overdue = self._fail_overdue(list(self._pending))
        if overdue:
            self.async_update_listeners()
            _LOGGER.info(
                "%s could not be read back before the deadline (%s); giving up "
                "on the writes to slots %s",
                self._lock.lock.entity_id,
                err,
                [address.user_ref for address in overdue],
            )
        else:
            _LOGGER.debug(
                "Confirmation read failed for %s, will retry: %s",
                self._lock.lock.entity_id,
                err,
            )

    @callback
    def push_update(self, updates: dict[int, SlotCredential]) -> None:
        """
        Push one or more slot updates, as the lock's word, and notify entities.

        Whether an address is verified is not a property of the value pushed
        but of whether a write is pending against it; see ``record_write`` and
        ``observe_push`` for the paths that touch that.
        """
        if not updates:
            return

        new_data = {**self.data, **self._normalize_keys(updates)}
        if new_data == self.data:
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
        elif self._is_push and not self._reached_once:
            # A push provider polls on no timer, so the recovery probe above
            # can never start from a cold start: tripping the breaker takes
            # repeated polls and nothing is scheduling them. One failed first
            # load would strand every entity unavailable until a reload, which
            # re-runs the same first load into the same wall. Poll on the base
            # backoff cadence until the coordinator is seeded once -- a lock
            # that was merely asleep at startup (a FLiRS battery lock, say)
            # then recovers on its own. Second-guessing a poll provider's own
            # cadence is not this arm's business, hence the interval check.
            # ``_reset_backoff`` restores the push cadence on first success,
            # and the breaker's escalating delay takes over above once it
            # trips.
            retry_interval = timedelta(seconds=BACKOFF_INITIAL_SECONDS)
            if self.update_interval != retry_interval:
                self.update_interval = retry_interval
                _LOGGER.debug(
                    "Initial load for %s has not succeeded yet; retrying every "
                    "%ds until the lock answers",
                    self._lock.lock.entity_id,
                    BACKOFF_INITIAL_SECONDS,
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

    @callback
    def note_push_capability(self) -> None:
        """
        Re-read whether the lock pushes, once its provider setup has succeeded.

        A bridged provider derives push support from discovery data arriving
        on the broker's schedule, so a lock whose setup was deferred was built
        before the answer existed and kept the poll cadence chosen for a lock
        that has none -- polling a pushing lock forever, one api round trip
        per slot every five minutes.

        Only the gaining direction, and only once: discovery data going
        transiently missing is not a lock that stopped pushing, and the
        cadence would thrash with it. The live interval is left alone while a
        probe arm owns it (breaker backoff, or the cold-start retry); both
        restore ``_original_update_interval``, which has just been updated.
        """
        if self._is_push or not self._lock.supports_push:
            return
        self._is_push = True
        self._original_update_interval = None
        if not self._lock_breaker.tripped and self._reached_once:
            # Same narrowing the restore-on-recovery arm needs: the base
            # class annotates this as ``timedelta`` while treating None as
            # "do not poll", which is exactly the state being entered.
            self.update_interval = self._original_update_interval  # type: ignore[assignment]
        _LOGGER.debug(
            "Lock %s pushes after all; polling disabled",
            self._lock.lock.entity_id,
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
        # Nothing may be looked at again: a look already in flight is
        # cancelled, and an empty pending set arms no timer after it.
        self._pending.clear()
        self._failed_writes.clear()
        if self._confirm_task is not None:
            self._confirm_task.cancel()
            self._confirm_task = None
        if self._confirm_unsub:
            self._confirm_unsub()
            self._confirm_unsub = None
        if self._drift_unsub:
            self._drift_unsub()
            self._drift_unsub = None
        if self._connection_unsub:
            self._connection_unsub()
            self._connection_unsub = None
        await super().async_shutdown()
