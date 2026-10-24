"""
A lock provider, as the worker sees it: a fresh read, a write, and pushes.

``ProviderPort`` gives the worker those from an existing ``BaseLock`` by
calling the provider's own hooks. Pacing, turn-taking and checking each write
by reading it back are the worker's; the duplicate pre-check is this port's
(``_duplicate_of``). ``async_internal_is_reachable`` is the one base wrapper
it calls, to retry a deferred setup and for the connection check.

The provider's own lifecycle is kept as it is: setup, setup deferred while
its integration loads, and resubscribing when that integration reloads or
reconnects. In place of the old coordinator the provider gets a small
stand-in that hands what it pushes to the worker, says which slots want a
PIN, and turns its refresh requests into reads. The adapter also keeps the
provider's cadence: a poll, and the connection check that drives the
provider's resubscribing. That check only drives the provider's lifecycle;
it never decides what a failed sync costs (the worker's answers do).
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
import contextlib
from datetime import datetime, timedelta
from functools import partial
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from ..model.credentials import CredentialAddress, CredentialType, SlotCredential
from ..model.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockBusy,
    LockCodeManagerError,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationBlocked,
    LockOperationError,
    LockOperationUnsupported,
)
from ..sync.slot import Holds, Reading, Seen
from ..sync.worker import ErrorKind, LockError, LockWorker

if TYPE_CHECKING:
    from ..providers._base import BaseLock

_LOGGER = logging.getLogger(__name__)


def reading(credential: SlotCredential) -> Reading:
    """Return what ``credential`` says about its slot."""
    if credential.is_empty:
        return Seen.EMPTY
    if (pin := credential.readable_pin) is None:
        return Seen.MASKED
    return Holds(pin)


def lock_error(
    err: LockCodeManagerProviderError, *, clearing: bool
) -> LockError | None:
    """
    Return what a provider's error on a write means to the worker; None for a bug.

    A refusal is the lock's answer about the slot. Any other failure is no
    answer: providers wrap an integration or cloud outage as a failed
    operation, and a slot is still charged when the lock answers anything
    else in its sync, such as the read-back.
    """
    if isinstance(err, CodeRejectedError):
        # A refused clear holds at once; a refused PIN turns the slot off.
        kind = ErrorKind.SLOT_REFUSED if clearing else ErrorKind.VALUE_REFUSED
    elif isinstance(err, LockOperationUnsupported):
        kind = ErrorKind.SLOT_REFUSED
    elif isinstance(err, (LockOperationError, LockBusy)):
        kind = ErrorKind.NO_ANSWER
    else:
        return None
    cause = str(err) if isinstance(err, LockOperationBlocked) else ""
    return LockError(kind, message=str(err), cause=cause)


class ProviderPort:
    """``LockPort`` for an existing provider, and its stand-in coordinator."""

    def __init__(
        self,
        provider: BaseLock,
        name_for: Callable[[int], str | None] = lambda slot: None,
        polling: Callable[[], bool] = lambda: True,
        reports: dict[str, bool] | None = None,
        on_reports: Callable[[], None] = lambda: None,
    ) -> None:
        """
        Wrap ``provider``.

        ``name_for`` names the user a slot's PIN is written for; ``polling``
        says whether the lock is polled ("Disable polling" spares the drift
        check). ``reports`` is shared by every port for the run: lock's
        entity registry id -> whether a read of it has described a slot (see
        ``read``), so a device re-paired under the same entity id starts over;
        ``on_reports`` is called when a read of this lock first does.
        """
        self.provider = provider
        self.name_for = name_for
        self.polling = polling
        # The worker that owns the lock: set once it is built.
        self.worker: LockWorker[CredentialAddress, ProviderPort] | None = None
        # The credential a write is in the provider for: its own echo is not
        # the lock.
        self._writing: CredentialAddress | None = None
        # When each credential was last written, on the event loop's clock: a
        # provider whose reads lag its writes reads it as silent until then.
        self._written_at: dict[CredentialAddress, float] = {}
        # Slots given up: clearing one releases it -- nothing untagged is
        # adopted, and a native-user provider deletes the user it tagged.
        self.releasing: set[int] = set()
        # Of those, the ones the store showed a credential Lock Code Manager
        # wrote when the release began: the release's own send forgets it.
        self._written: set[int] = set()
        # What the lock last said of any slot, by read or by push: codes
        # nobody here manages, and the last word on a slot the worker has let
        # go. The worker's own view of a slot it tracks comes first.
        self.others: dict[int, Reading] = {}
        # Some read has said something about a slot: silence is now no answer.
        self._answered = False
        # Every lock read this run, by registry id -> whether a read described
        # a slot of it. Kept past this port: a one-off read builds a port per
        # read.
        self.reports: dict[str, bool] = {} if reports is None else reports
        self.on_reports = on_reports
        # Slots the lock last answered it cannot tell what they hold.
        self.not_reported: set[int] = set()
        self._poll: asyncio.Task[None] | None = None
        self._poll_unsubs: list[Callable[[], None]] = []
        # Unloaded: nothing may start a poll or arm its timer again.
        self._closed = False
        # This port set the provider up, and owns its lifecycle.
        self._managed = False
        # Its setup raised what no provider handler expects: nothing retries
        # that short of a reload, so nothing can be sent to the lock.
        self.setup_crashed = False
        self._unsubs: list[Callable[[], None]] = []

    @property
    def managed(self) -> bool:
        """Return whether this port set the provider up and owns its lifecycle."""
        return self._managed

    # LockPort ----------------------------------------------------------------

    async def _timed[T](
        self, op: str, slots: Any, call: Callable[[], Awaitable[T]]
    ) -> T:
        """Run one provider call, logging its start and how long it took."""
        entity_id = self.provider.lock.entity_id
        _LOGGER.debug("%s slot %s: provider %s start", entity_id, slots, op)
        start = time.monotonic()
        outcome = "ok"
        try:
            return await call()
        except BaseException as err:
            kind = getattr(err, "kind", None)
            outcome = kind.value if isinstance(kind, ErrorKind) else type(err).__name__
            raise
        finally:
            _LOGGER.debug(
                "%s slot %s: provider %s %s in %.0f ms",
                entity_id,
                slots,
                op,
                outcome,
                (time.monotonic() - start) * 1000,
            )

    async def read(
        self, addresses: Collection[CredentialAddress]
    ) -> dict[CredentialAddress, Reading]:
        """
        Ask the lock about ``addresses`` now (see ``_read``), timed for the logs.

        One read per credential type. A type whose read fails is left out,
        as silent, so a lock that cannot read one type still answers for the
        rest; the read fails only when every type's does.
        """
        by_type: dict[CredentialType, list[int]] = {}
        for address in addresses:
            self._writable(address)
            by_type.setdefault(address.credential_type, []).append(address.user_ref)
        readings: dict[CredentialAddress, Reading] = {}
        failed: list[LockError] = []
        for credential_type, numbers in by_type.items():
            if credential_type is not CredentialType.PIN:
                op = f"read ({credential_type} credentials)"
            elif self.provider.fresh_read_is_hard_refresh:
                op = "read (hard refresh)"
            else:
                op = "read (get usercodes)"
            where = numbers[0] if len(numbers) == 1 else tuple(numbers)
            try:
                read = await self._timed(
                    op, where, partial(self._read, numbers, credential_type)
                )
            except LockError as err:
                failed.append(err)
                continue
            readings.update(
                {
                    CredentialAddress(slot, credential_type): seen
                    for slot, seen in read.items()
                }
            )
        if len(failed) == len(by_type):
            raise failed[0]
        return readings

    async def _read(
        self, slots: Collection[int], credential_type: CredentialType
    ) -> dict[int, Reading]:
        """
        Ask the lock about ``credential_type`` in ``slots`` now.

        Bounded by the provider's own budget for a walk of that many slots.
        A slot the lock said nothing about is left out (silent); a read where
        it said nothing about any of them, from a lock that has answered
        reads before, is no answer -- unless, of some, it answered that it
        cannot tell.

        A slot it answered it cannot tell of is silent too (#1581), unless a
        read has described some slot of the lock this run: a lock that can
        report its codes and will not report this one holds a code there.

        The PIN view is kept from PIN reads alone: the codes nobody here
        manages (``others``) and the slots the lock cannot tell of
        (``not_reported``), which the lock card shows. Capacity follows the
        slot for a PIN only; the provider places any other type.
        """
        provider = self.provider
        pin = credential_type is CredentialType.PIN
        if self._managed and not await self._set_up():
            raise LockError(
                ErrorKind.NO_ANSWER, sent=False, message="provider not set up"
            )
        await self._available()
        fetch = (
            provider.async_hard_refresh_codes
            if pin and provider.fresh_read_is_hard_refresh
            else partial(provider.async_get_slot_credentials, credential_type)
        )
        provider.last_silent_slots = provider.last_foreign_slots = frozenset()
        provider.last_cannot_tell_slots = frozenset()
        # A slot past the lock's capacity is never asked about: no answer
        # would come, and the lock would be taken for gone (#1398). Its write
        # is refused unsent, which holds it.
        if pin:
            slots = [slot for slot in slots if await self._within_capacity(slot)]
        if not slots:
            # Nothing was asked: not the lock answering, or a probe of a slot
            # past capacity would end an outage's backoff.
            raise LockError(
                ErrorKind.SLOT_REFUSED, sent=False, message="past the lock's capacity"
            )
        try:
            async with asyncio.timeout(provider._operation_budget(len(slots))):
                codes = await fetch(slots)
        except TimeoutError as err:
            raise LockError(ErrorKind.NO_ANSWER, message="read timed out") from err
        except LockCodeManagerProviderError as err:
            if lock_error(err, clearing=False) is None:
                raise
            # A read that failed said nothing about any slot: no answer.
            raise LockError(ErrorKind.NO_ANSWER, message=str(err)) from err
        silent = provider.last_silent_slots & set(slots)
        cannot_tell = provider.last_cannot_tell_slots & silent
        if self._answered and silent >= set(slots) and not cannot_tell:
            raise LockError(ErrorKind.NO_ANSWER, message="the lock said nothing")
        lock = provider.lock.id
        # Any slot asked about and not silent was described, named in the
        # answer or not: a provider projecting only the slots an entry manages
        # (Z-Wave JS's hard refresh) leaves an empty one it was asked about
        # out, and that is no lock that cannot report. Said of PINs alone:
        # the slots a lock cannot tell of are PIN slots.
        if pin and not self.reports.get(lock) and set(slots) - silent:
            self.reports[lock] = True
            self.on_reports()
        if self.reports.setdefault(lock, False):
            silent -= cannot_tell
        if pin:
            self.not_reported.difference_update(slots)
            self.not_reported |= cannot_tell
        # The lock answered, but of a credential written too lately to show
        # it yet: left out (silent), so the write is trusted until a later read.
        silent |= self._lagging(slots, credential_type)
        # Someone else's code at a slot's index is not the slot's: a slot
        # turned off is clear, and one wanting a PIN writes it, which adopts
        # the user (#1695). It is still a code nobody here manages.
        foreign = provider.last_foreign_slots
        readings = {
            slot: Seen.EMPTY if slot in foreign else reading(codes[slot])
            for slot in slots
            if slot in codes and slot not in silent
        }
        if pin:
            heard = {
                slot: reading(credential)
                for slot, credential in codes.items()
                if slot not in silent
            }
            if provider.per_exchange_budget is None:
                # A provider that reads the whole lock in one call tells of
                # every code on it: one it no longer tells of is gone (deleted
                # at the keypad, say). What it said nothing about stays.
                self.others = {
                    **{
                        slot: seen
                        for slot, seen in self.others.items()
                        if slot in silent
                    },
                    **heard,
                }
            else:
                # One that walks the slots asked for says nothing of the rest.
                self.others.update(heard)
        # About the lock, whichever credential type's read answered.
        self._answered = self._answered or bool(readings)
        return readings

    def _duplicate_of(self, address: CredentialAddress, value: str) -> int | None:
        """
        Return another slot holding or wanting ``value`` as the same type; None if none.

        Duplicates are per type: a password may be the same string as a PIN.
        A tracked slot counts by what it wants (one on its way to another
        value, or out, does not); any other slot by what the lock said of
        it, which is known for PINs alone.
        """
        slot, credential_type = address
        tracked = (
            {}
            if self.worker is None
            else {
                other.user_ref: current
                for other, current in self.worker.slots.items()
                if other.credential_type is credential_type
            }
        )
        if credential_type is CredentialType.PIN:
            for other, held in self.others.items():
                if other != slot and other not in tracked and held == Holds(value):
                    return other
        return next(
            (
                other
                for other, current in tracked.items()
                if other != slot and current.want == value and not current.release
            ),
            None,
        )

    def release(self, slot: int, *, written: bool) -> None:
        """
        Make ``slot``'s next clear a release.

        ``written``: the store shows a credential Lock Code Manager wrote
        there. Taken now: the worker forgets what it wrote as it sends the
        clear. The PIN's clear sends the release, which removes every type;
        another type's is a clear of that type.
        """
        self.releasing.add(slot)
        if written:
            self._written.add(slot)
        else:
            self._written.discard(slot)

    async def write(self, address: CredentialAddress, value: str | None) -> bool | None:
        """Put ``value`` at ``address`` or clear it (see ``_write``), timed for logs."""
        self._writable(address)
        op = "clear" if value is None else "set"
        return await self._timed(op, address, lambda: self._write(address, value))

    def _writable(self, address: CredentialAddress) -> None:
        """Refuse, unsent, a credential type the provider cannot write."""
        if address.credential_type not in self.provider.writable_credential_types:
            raise LockError(
                ErrorKind.SLOT_REFUSED,
                sent=False,
                message=(
                    "Lock Code Manager does not write "
                    f"{address.credential_type} credentials to "
                    f"{self.provider.lock.entity_id}"
                ),
            )

    async def _write(
        self, address: CredentialAddress, value: str | None
    ) -> bool | None:
        """
        Put ``value`` at ``address``, or clear it for None.

        Nothing is sent before the provider is set up, or for a PIN in a
        slot past the lock's capacity (#1398). A slot's release goes with
        its PIN's clear (``release``), every PIN slot being kept.
        """
        provider = self.provider
        slot, credential_type = address
        pin = credential_type is CredentialType.PIN
        if not await self._set_up():
            raise LockError(
                ErrorKind.NO_ANSWER, sent=False, message="provider not set up"
            )
        await self._available()
        self._writing = address
        if value is None:
            _LOGGER.debug(
                "Clearing %s on %s slot %s (source=%s)",
                "usercode" if pin else credential_type,
                provider.lock.entity_id,
                slot,
                "sync",
            )
        elif pin:
            # Masked: the ``deobfuscate_log`` action decodes it from here.
            _LOGGER.debug(
                "Setting usercode on %s slot %s (pin=%s, source=%s)",
                provider.lock.entity_id,
                slot,
                provider.mask_pin(value, slot),
                "sync",
            )
        else:
            _LOGGER.debug(
                "Setting %s on %s slot %s (value=%s, source=%s)",
                credential_type,
                provider.lock.entity_id,
                slot,
                provider.mask_pin(value, slot),
                "sync",
            )
        try:
            if value is None and pin and slot in self.releasing:
                await provider.async_release_managed_slot(
                    slot, written=slot in self._written
                )
                # It removes every type at once: each is as lately written.
                now = provider.hass.loop.time()
                self._written_at.update(
                    {
                        CredentialAddress(slot, written_type): now
                        for written_type in provider.writable_credential_types
                    }
                )
            elif value is None:
                # A clear that found nothing says nothing about what is there.
                return await provider.async_clear_slot_credential(slot, credential_type)
            else:
                if pin:
                    await self._capacity(slot)
                if (other := self._duplicate_of(address, value)) is not None:
                    # Two users sharing a credential: whoever presents it
                    # opens the door as the other, and turning one off leaves
                    # it working. Refused unsent: not the lock answering.
                    duplicate = DuplicateCodeError(
                        code_slot=slot,
                        lock_entity_id=provider.lock.entity_id,
                        conflicting_slot=other,
                        conflicting_slot_managed=provider.is_slot_managed(other),
                    )
                    raise LockError(
                        ErrorKind.VALUE_REFUSED, sent=False, message=str(duplicate)
                    ) from duplicate
                await provider.async_set_slot_credential(
                    slot,
                    credential_type,
                    value,
                    name=self.name_for(slot),
                    source="sync",
                )
        except LockCodeManagerProviderError as err:
            if (error := lock_error(err, clearing=value is None)) is None:
                raise
            raise error from err
        finally:
            self._writing = None
            # It may have landed, whatever came back.
            self._written_at[address] = provider.hass.loop.time()
        return None

    async def _set_up(self) -> bool:
        """
        Return whether the provider is set up, asking it first if not.

        Asking is what retries a deferred setup; a provider with no
        connection check (Z-Wave JS) is asked nowhere else. Reads are gated
        too once this port set the provider up: a lock that answers reads
        while every write is refused unsent would charge each slot's sync
        (the read-back answers) and hold it. A provider this port never set
        up (a one-off read's) is only read.
        """
        provider = self.provider
        if not provider.provider_setup_succeeded:
            with contextlib.suppress(LockCodeManagerError):
                await provider.async_internal_is_reachable()
        return provider.provider_setup_succeeded

    def _lagging(
        self, slots: Collection[int], credential_type: CredentialType
    ) -> set[int]:
        """Return the slots whose ``credential_type`` was written more lately than reads catch up."""
        if not (lag := self.provider.read_lag):
            return set()
        now = self.provider.hass.loop.time()
        return {
            slot
            for slot in slots
            if (
                written := self._written_at.get(
                    CredentialAddress(slot, credential_type)
                )
            )
            is not None
            and now - written < lag
        }

    async def _available(self) -> None:
        """
        Refuse, unsent, an exchange with a device its provider says is gone.

        For providers that ask for it: Z-Wave queues a dead node's commands
        and delivers them whenever it comes back (#139). No answer, so the
        lock backs off; only the worker's answers decide what is charged.
        """
        provider = self.provider
        if not provider.check_available_before_exchange:
            return
        try:
            available = await provider.async_internal_is_reachable()
        except LockCodeManagerError as err:
            raise LockError(ErrorKind.NO_ANSWER, sent=False, message=str(err)) from err
        if not available:
            raise LockError(
                ErrorKind.NO_ANSWER, sent=False, message="device not available"
            )

    async def _within_capacity(self, slot: int) -> bool:
        """Return whether ``slot`` is within the lock's capacity, as far as known."""
        try:
            await self.provider._assert_slot_within_capacity(slot)
        except LockOperationUnsupported:
            return False
        except Exception:
            # An unknown capacity limits nothing.
            _LOGGER.debug(
                "Capacity of %s unknown", self.provider.lock.entity_id, exc_info=True
            )
        return True

    async def _capacity(self, slot: int) -> None:
        """Refuse, unsent, a slot past the lock's capacity."""
        try:
            await self.provider._assert_slot_within_capacity(slot)
        except LockOperationUnsupported as err:
            raise LockError(
                ErrorKind.SLOT_REFUSED, sent=False, message=str(err)
            ) from err

    # Lifecycle ---------------------------------------------------------------

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set the provider up with the stand-in coordinator; start the cadence."""
        provider = self.provider
        self._managed = True
        try:
            await provider.async_setup_internal(config_entry, _Coordinator(self))
        except Exception:
            self.setup_crashed = True
            raise
        finally:
            # Even if setup raised: the poll reads the lock, and the
            # connection check is what retries a deferred setup.
            self._arm_poll()
            if interval := provider.connection_check_interval:
                self._unsubs.append(
                    async_track_time_interval(
                        provider.hass,
                        self._check_connection,
                        interval,
                        cancel_on_shutdown=True,
                    )
                )

    async def async_unload(self, remove_permanently: bool) -> None:
        """Stop the cadence and unload the provider."""
        self._closed = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for unsub in self._poll_unsubs:
            unsub()
        self._poll_unsubs.clear()
        if self._poll is not None:
            self._poll.cancel()
            await asyncio.gather(self._poll, return_exceptions=True)
        await self.provider.async_unload(remove_permanently)

    # What the provider pushes and asks for -------------------------------------

    def pushed(self, address: CredentialAddress, credential: SlotCredential) -> None:
        """Hand the worker what the lock reported on its own about ``address``."""
        if self.worker is None or address == self._writing:
            # A provider echoing the write it is making is not the lock.
            return
        pin = address.credential_type is CredentialType.PIN
        if pin:
            self.not_reported.discard(address.user_ref)
        if address not in self.worker.slots:
            if pin:
                self.others[address.user_ref] = reading(credential)
            return
        self.worker.push(address, reading(credential))

    def poll(self) -> None:
        """Read every slot the worker tracks, unless a poll is still out."""
        if self.worker is None or not self.worker.started:
            return
        if self._poll is None or self._poll.done():
            self._poll = self.provider.hass.async_create_background_task(
                self._read_tracked(), f"Poll {self.provider.lock.entity_id}"
            )

    def wants(self, address: CredentialAddress) -> bool:
        """Return whether ``address`` should hold a value."""
        current = None if self.worker is None else self.worker.slots.get(address)
        return current is not None and current.want is not None

    async def _read_tracked(self) -> None:
        assert self.worker is not None
        try:
            await self.worker.read()
        except LockError as err:
            # The worker has counted it; nothing else to do.
            _LOGGER.debug("Poll of %s failed: %s", self.provider.lock.entity_id, err)

    def _cadence(self) -> tuple[timedelta | None, timedelta | None]:
        """
        Return the scan interval (a lock that does not push) and the drift check's.

        Every read is fresh, so both are plain reads; only the scan gives way
        to "Disable polling".
        """
        provider = self.provider
        scan = None if provider.supports_push else provider.usercode_scan_interval
        return scan, provider.hard_refresh_interval

    def _arm_poll(self) -> None:
        """(Re)start the poll at the provider's cadence as it stands now."""
        for unsub in self._poll_unsubs:
            unsub()
        self._poll_unsubs.clear()
        if self._closed:
            return
        scan, drift = self._cadence()
        for interval, action in ((scan, self._on_scan), (drift, self._on_poll)):
            if interval:
                self._poll_unsubs.append(
                    async_track_time_interval(
                        self.provider.hass, action, interval, cancel_on_shutdown=True
                    )
                )

    @callback
    def _on_scan(self, now: datetime) -> None:
        if self.polling():
            self.poll()

    @callback
    def _on_poll(self, now: datetime) -> None:
        self.poll()

    async def _check_connection(self, now: datetime) -> None:
        """Let the provider see its integration come and go, to resubscribe."""
        try:
            await self.provider.async_internal_is_reachable()
        except LockCodeManagerError as err:
            _LOGGER.debug(
                "Connection check failed for %s: %s",
                self.provider.lock.entity_id,
                err,
            )


class _Coordinator:
    """What a provider asks of the old coordinator, answered for the core."""

    last_update_success = True
    last_exception: Exception | None = None

    def __init__(self, port: ProviderPort) -> None:
        """Answer for ``port``."""
        self._port = port

    def push_update(self, updates: dict[int, SlotCredential]) -> None:
        """Hand each pushed slot's PIN to the worker."""
        for slot, credential in updates.items():
            self._port.pushed(CredentialAddress(slot, CredentialType.PIN), credential)

    def observe_push(
        self, address: CredentialAddress, observed: SlotCredential
    ) -> None:
        """Hand a push about one credential to the worker."""
        self._port.pushed(address, observed)

    def desired_credential(
        self, address: CredentialAddress, *, owner: Any = None
    ) -> SlotCredential:
        """Say whether a value is wanted at ``address`` (only that it is, not which)."""
        if self._port.wants(address):
            return SlotCredential.unreadable()
        return SlotCredential.empty()

    async def async_request_refresh(self) -> None:
        """Read every tracked slot (after a reconnect, or a lock's event)."""
        if (worker := self._port.worker) is not None and not worker.reachable:
            worker.wake()  # backing off: probe now instead of waiting it out
            return
        self._port.poll()

    async def async_refresh(self) -> None:
        """Do nothing: the worker's own first read comes first."""

    async def async_in_turn[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """
        Run ``call`` against the lock in its worker's turn; return its result.

        For an exchange a provider starts on its own, such as an event's
        follow-up read: the worker stays the one owner of every exchange, so
        nothing reaches the lock once it has stopped. The provider's own error
        comes back as it was raised; a worker that has stopped, or is backing
        off, answers ``LockDisconnected`` without asking the lock.
        """
        worker = self._port.worker
        # Built before the provider is set up with this stand-in.
        assert worker is not None
        raised: list[LockCodeManagerProviderError] = []

        async def _call() -> T:
            try:
                return await call()
            except LockCodeManagerProviderError as err:
                raised.append(err)
                if (error := lock_error(err, clearing=False)) is None:
                    raise  # the provider's bug: the worker logs it
                raise error from err

        try:
            return await worker.turn(_call)
        except LockError as err:
            if raised:
                raise raised[0] from None
            raise LockDisconnected(str(err) or "the lock did not answer") from err

    def async_set_update_error(self, err: Exception) -> None:
        """Do nothing: the worker counts its own failures."""

    def note_push_capability(self) -> None:
        """
        Re-arm the poll once a bridged provider has learned it can push.

        Push data going missing for a while is not a lock that stopped
        pushing: chasing it would thrash the cadence with the broker.
        """
        if self._port.provider.supports_push:
            self._port._arm_poll()


type PortWorker = LockWorker[CredentialAddress, ProviderPort]
