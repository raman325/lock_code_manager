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

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
import contextlib
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from ..domain.credentials import CredentialAddress
from ..domain.exceptions import (
    CodeRejectedError,
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
    from ..domain.coordinator import LockUsercodeUpdateCoordinator
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
    ) -> None:
        """Wrap ``provider``; ``name_for`` names the user a slot's PIN is written for."""
        self.provider = provider
        self.name_for = name_for
        # The worker that owns the lock: set once it is built.
        self.worker: LockWorker | None = None
        # The slot a write is in the provider for: its own echo is not the lock.
        self._writing: int | None = None
        # Some read has said something about a slot: silence is now no answer.
        self._answered = False
        self._poll: asyncio.Task[None] | None = None
        self._poll_unsub: Callable[[], None] | None = None
        # Unloaded: nothing may start a poll or arm its timer again.
        self._closed = False
        # This port set the provider up, and owns its lifecycle.
        self._managed = False
        self._unsubs: list[Callable[[], None]] = []

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
        fetch = (
            provider.async_hard_refresh_codes
            if provider.fresh_read_is_hard_refresh
            else provider.async_get_usercodes
        )
        provider.last_silent_slots = frozenset()
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
        readings = {
            slot: reading(codes[slot])
            for slot in slots
            if slot in codes and slot not in silent
        }
        self._answered = self._answered or bool(readings)
        return readings

    async def write(self, slot: int, pin: str | None) -> None:
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
        self._writing = slot
        try:
            if pin is None:
                await provider.async_clear_usercode(slot)
            else:
                await self._capacity(slot)
                await provider.async_set_usercode(
                    slot, pin, self.name_for(slot), source="sync"
                )
        except LockCodeManagerProviderError as err:
            if (error := lock_error(err, clearing=pin is None)) is None:
                raise
            raise error from err
        finally:
            self._writing = None

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
        await provider.async_setup_internal(
            config_entry,
            # The old coordinator's type until the swap deletes it.
            cast("LockUsercodeUpdateCoordinator", _Coordinator(self)),
        )
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
        if self._poll_unsub is not None:
            self._poll_unsub()
            self._poll_unsub = None
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

    def _poll_interval(self) -> timedelta | None:
        """Every read is fresh, so polling and drift checks are one cadence."""
        provider = self.provider
        intervals = [provider.hard_refresh_interval]
        if not provider.supports_push:
            intervals.append(provider.usercode_scan_interval)
        return min((i for i in intervals if i is not None), default=None)

    def _arm_poll(self) -> None:
        """(Re)start the poll at the provider's cadence as it stands now."""
        if self._poll_unsub is not None:
            self._poll_unsub()
            self._poll_unsub = None
        if not self._closed and (interval := self._poll_interval()):
            self._poll_unsub = async_track_time_interval(
                self.provider.hass, self._on_poll, interval, cancel_on_shutdown=True
            )

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
        self.data: dict[CredentialAddress, SlotCredential] = {}

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

    def credentials_by_slot(self) -> dict[int, SlotCredential]:
        """Return nothing: the duplicate pre-check this served is not the core's."""
        return {}

    async def async_request_refresh(self) -> None:
        """Read every tracked slot (after a reconnect, or a lock's event)."""
        self._port.poll()

    async def async_refresh(self) -> None:
        """Do nothing: the worker's own first read comes first."""

    def async_set_update_error(self, err: Exception) -> None:
        """Do nothing: the worker counts its own failures."""

    def record_write(self, *args: Any, **kwargs: Any) -> None:
        """Do nothing: the worker records what it wrote."""

    def drop_pending(self, address: CredentialAddress) -> None:
        """Do nothing: the core keeps no pending writes."""

    def note_push_capability(self) -> None:
        """Re-arm the poll: a bridged provider has learned it can push."""
        self._port._arm_poll()
