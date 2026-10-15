"""
A lock provider, as the worker sees it: a fresh read, a write, and pushes.

``ProviderPort`` gives the worker those from an existing ``BaseLock`` by
calling the provider's own hooks -- never its ``async_internal_*`` wrappers,
so none of the old core runs: no rate limiting, turn-taking, duplicate
pre-check, pending writes or confirmations. The worker does all of that.

The provider's own lifecycle is kept as it is: setup, setup deferred while
its integration loads, and resubscribing when that integration reloads or
reconnects. In place of the old coordinator the provider gets a small
stand-in that hands what it pushes to the worker, says which slots want a
PIN, and turns its refresh requests into reads. The adapter also keeps the
provider's cadence: a poll, and the connection check that drives the
provider's resubscribing. That check only drives the provider's lifecycle;
it never decides what a failed sync costs (the worker's answers do).

The design: docs/superpowers/specs/2026-10-14-core-registry-adapter.md.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
import contextlib
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from ..domain.credentials import CredentialAddress
from ..domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockBusy,
    LockCodeManagerError,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
    LockOperationUnsupported,
)
from ..domain.models import SlotCredential
from .slot import Holds, Reading, Seen
from .worker import ErrorKind, LockError, LockWorker

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
    elif isinstance(err, (LockOperationFailed, LockDisconnected, LockBusy)):
        kind = ErrorKind.NO_ANSWER
    else:
        return None
    return LockError(kind, message=str(err))


class ProviderPort:
    """``LockPort`` for an existing provider, and its stand-in coordinator."""

    def __init__(
        self,
        provider: BaseLock,
        name_for: Callable[[int], str | None] = lambda slot: None,
        polling: Callable[[], bool] = lambda: True,
    ) -> None:
        """
        Wrap ``provider``.

        ``name_for`` names the user a slot's PIN is written for; ``polling``
        says whether the lock is polled ("Disable polling" spares the drift
        check).
        """
        self.provider = provider
        self.name_for = name_for
        self.polling = polling
        # The worker that owns the lock: set once it is built.
        self.worker: LockWorker[ProviderPort] | None = None
        # The slot a write is in the provider for: its own echo is not the lock.
        self._writing: int | None = None
        # When each slot was last written, on the event loop's clock: a
        # provider whose reads lag its writes reads it as silent until then.
        self._written_at: dict[int, float] = {}
        # Slots given up: clearing one releases it -- nothing untagged is
        # adopted, and a native-user provider deletes the user it tagged.
        self.releasing: set[int] = set()
        # What the lock last said of any slot, by read or by push: codes
        # nobody here manages, and the last word on a slot the worker has let
        # go. The worker's own view of a slot it tracks comes first.
        self.others: dict[int, Reading] = {}
        # Some read has said something about a slot: silence is now no answer.
        self._answered = False
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

    async def read(self, slots: Collection[int]) -> dict[int, Reading]:
        """
        Ask the lock about ``slots`` now.

        Bounded by the provider's own budget for a walk of that many slots.
        A slot the lock said nothing about is left out (silent); a read where
        it said nothing about any of them, from a lock that has answered
        reads before, is no answer.
        """
        provider = self.provider
        if self._managed and not await self._set_up():
            raise LockError(
                ErrorKind.NO_ANSWER, sent=False, message="provider not set up"
            )
        await self._available()
        fetch = (
            provider.async_hard_refresh_codes
            if provider.fresh_read_is_hard_refresh
            else provider.async_get_usercodes
        )
        provider.last_silent_slots = frozenset()
        # A slot past the lock's capacity is never asked about: no answer
        # would come, and the lock would be taken for gone (#1398). Its write
        # is refused unsent, which holds it.
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
        if self._answered and silent >= set(slots):
            raise LockError(ErrorKind.NO_ANSWER, message="the lock said nothing")
        # The lock answered, but of a slot written too lately to show it yet:
        # left out (silent), so the write is trusted until a later read.
        silent |= self._lagging(slots)
        readings = {
            slot: reading(codes[slot])
            for slot in slots
            if slot in codes and slot not in silent
        }
        heard = {
            slot: reading(credential)
            for slot, credential in codes.items()
            if slot not in silent
        }
        if provider.per_exchange_budget is None:
            # A provider that reads the whole lock in one call tells of every
            # code on it: one it no longer tells of is gone (deleted at the
            # keypad, say). What it said nothing about stays as it was.
            self.others = {
                **{slot: seen for slot, seen in self.others.items() if slot in silent},
                **heard,
            }
        else:
            # One that walks the slots asked for says nothing of the rest.
            self.others.update(heard)
        self._answered = self._answered or bool(readings)
        return readings

    def _duplicate_of(self, slot: int, pin: str) -> int | None:
        """
        Return another slot ``pin`` is on, or is wanted on; None if none.

        A tracked slot counts by what it wants (one on its way to another
        PIN, or out, does not); any other slot by what the lock said of it.
        """
        tracked = {} if self.worker is None else self.worker.slots
        for other, held in self.others.items():
            if other != slot and other not in tracked and held == Holds(pin):
                return other
        return next(
            (
                other
                for other, current in tracked.items()
                if other != slot and current.want == pin and not current.release
            ),
            None,
        )

    async def write(self, slot: int, pin: str | None) -> bool | None:
        """
        Put ``pin`` in ``slot``, or clear it for None.

        Nothing is sent before the provider is set up, or for a slot past
        the lock's capacity (#1398).
        """
        provider = self.provider
        if not await self._set_up():
            raise LockError(
                ErrorKind.NO_ANSWER, sent=False, message="provider not set up"
            )
        await self._available()
        self._writing = slot
        if pin is None:
            _LOGGER.debug(
                "Clearing usercode on %s slot %s (source=%s)",
                provider.lock.entity_id,
                slot,
                "sync",
            )
        else:
            # Masked: the ``deobfuscate_log`` action decodes it from here.
            _LOGGER.debug(
                "Setting usercode on %s slot %s (pin=%s, source=%s)",
                provider.lock.entity_id,
                slot,
                provider.mask_pin(pin, slot),
                "sync",
            )
        try:
            if pin is None and slot in self.releasing:
                await provider.async_release_managed_slot(slot)
            elif pin is None:
                # A clear that found nothing says nothing about what is there.
                return await provider.async_clear_usercode(slot)
            else:
                await self._capacity(slot)
                if (other := self._duplicate_of(slot, pin)) is not None:
                    # Two users sharing a PIN: whoever types it opens the door
                    # as the other, and turning one off leaves it working.
                    # Refused unsent: not the lock answering.
                    duplicate = DuplicateCodeError(
                        code_slot=slot,
                        lock_entity_id=provider.lock.entity_id,
                        conflicting_slot=other,
                        conflicting_slot_managed=provider.is_slot_managed(other),
                    )
                    raise LockError(
                        ErrorKind.VALUE_REFUSED, sent=False, message=str(duplicate)
                    ) from duplicate
                await provider.async_set_usercode(
                    slot, pin, self.name_for(slot), source="sync"
                )
        except LockCodeManagerProviderError as err:
            if (error := lock_error(err, clearing=pin is None)) is None:
                raise
            raise error from err
        finally:
            self._writing = None
            # It may have landed, whatever came back.
            self._written_at[slot] = provider.hass.loop.time()
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

    def _lagging(self, slots: Collection[int]) -> set[int]:
        """Return the slots written more lately than the provider's reads catch up."""
        if not (lag := self.provider.read_lag):
            return set()
        now = self.provider.hass.loop.time()
        return {
            slot
            for slot in slots
            if (written := self._written_at.get(slot)) is not None
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

    def pushed(self, slot: int, credential: SlotCredential) -> None:
        """Hand the worker what the lock reported on its own about ``slot``."""
        if self.worker is None or slot == self._writing:
            # A provider echoing the write it is making is not the lock.
            return
        if slot not in self.worker.slots:
            self.others[slot] = reading(credential)
            return
        self.worker.push(slot, reading(credential))

    def poll(self) -> None:
        """Read every slot the worker tracks, unless a poll is still out."""
        if self.worker is None or not self.worker.started:
            return
        if self._poll is None or self._poll.done():
            self._poll = self.provider.hass.async_create_background_task(
                self._read_tracked(), f"Poll {self.provider.lock.entity_id}"
            )

    def wants_pin(self, slot: int) -> bool:
        """Return whether ``slot`` should hold a PIN."""
        current = None if self.worker is None else self.worker.slots.get(slot)
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
        """Hand each pushed slot to the worker."""
        for slot, credential in updates.items():
            self._port.pushed(slot, credential)

    def observe_push(
        self, address: CredentialAddress, observed: SlotCredential
    ) -> None:
        """Hand a push about one slot to the worker."""
        self._port.pushed(address.user_ref, observed)

    def desired_credential(
        self, address: CredentialAddress, *, owner: Any = None
    ) -> SlotCredential:
        """Say whether a PIN is wanted at ``address`` (only that it is, not which)."""
        if self._port.wants_pin(address.user_ref):
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
