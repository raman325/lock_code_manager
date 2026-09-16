"""
Shared behaviour for locks reached through an MQTT bridge.

Zigbee2MQTT and zwave-js-ui speak entirely different protocols to entirely
different radios, but they reach them the same way: a bridge that Home
Assistant's MQTT integration already talks to, addressed by topics the bridge
published in its own discovery payload. That shared transport is what makes
their hardening the same problem, and this class is where the answers live.

What it hosts is the set of decisions that are about the transport rather
than about the protocol:

* the operational preamble every public operation runs first, so a lock whose
  MQTT, bridge, or entity is down fails as a disconnect instead of as a
  ten-second timeout;
* the sequential per-slot read, including the distinction between a slot the
  lock described and a slot nothing came back for -- a read where every slot
  failed at the transport is not data and must not be reported as any;
* the entity availability check, which defers to the lock entity's own state
  rather than re-deriving the bridge's internals;
* the policy a resubscribe attempt follows -- keep, refuse, defer, or run it
  in the background -- which is about how a sync caller reaches an async
  subscribe, not about what is being subscribed to;
* the poll cadences, which are set by what a read costs here: one round trip
  per slot, on either bridge, with the hard refresh doing the drift detection
  a push provider's suppressed poll otherwise would.

What it deliberately does NOT host is anything whose semantics differ between
the two: the subscriptions themselves (one bridge has a per-node value tree,
the other a per-device topic, and zwave-js-ui also runs an api transport with
its own lifetime), the payload projections, and the api client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import timedelta
import time
from typing import ClassVar, NoReturn, final

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.core import callback

from ..domain.credentials import User, user_from_slot
from ..domain.exceptions import LockDisconnected
from ..domain.models import SlotCredential
from ..domain.read_health import (
    SILENT_READS_TO_CLASSIFY,
    UNANSWERED_PROBE_INTERVAL,
    ReadHealth,
    async_record_read_health,
    read_health,
)
from ._base import BaseLock
from .const import LOGGER


@dataclass(repr=False, eq=False)
class BaseMqttLock(BaseLock):
    """Base class for a lock addressed through an MQTT bridge."""

    # What one slot's read may cost on this transport. `_async_read_slots`
    # walks the slots one at a time -- deliberately, so the bridge and the
    # lock's firmware answer each read before the next goes out -- so the
    # deadline is this times the slots a call walks. Above zwave-js-ui's own
    # 60s API bound, which must always be the one to claim a silent slot.
    per_exchange_budget: ClassVar[float | None] = 70.0

    # When a lock that does not answer reads was last asked again, on the
    # monotonic clock; ``None`` until it has been asked on this instance.
    _last_unanswered_probe: float | None = field(init=False, default=None)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MQTT_DOMAIN

    @property
    def usercode_scan_interval(self) -> timedelta:
        """
        Return scan interval for usercodes.

        Inert while ``supports_push`` is true: the coordinator leaves its
        update interval unset for a push provider, so nothing schedules a poll
        at this cadence and drift is caught by the hard refresh instead. This
        is the cadence a lock running without push actually polls at, and it
        is spaced well out because a read here costs a round trip per slot
        rather than one for the whole lock -- see ``_async_read_slots``.
        """
        return timedelta(minutes=5)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """
        Return interval for hard refresh.

        The only recurring read a push provider makes, so it is also what
        notices a subscription that has drifted off the topic discovery now
        points at.
        """
        return timedelta(hours=1)

    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """
        Perform hard refresh and return all codes.

        There is no cached layer here for a hard refresh to go behind: an
        ordinary read already puts a request on the bridge and waits for the
        lock's own answer, so the two are the same operation.
        """
        # No cache to project from: a scoped refresh re-reads every managed slot
        # plus ``slots``, so the coordinator's replace still names them all.
        return await self.async_get_usercodes(
            None if slots is None else self.managed_slots | frozenset(slots)
        )

    async def async_is_device_available(self) -> bool:
        """
        Return whether the lock entity reports an operational state.

        Deferring to the entity's own availability rather than re-deriving the
        bridge's internals -- both bridges publish a status topic the entity
        already follows. A missing state row reads the same as an explicit
        ``unavailable``: neither is an entity that can answer a command.
        """
        state = self.hass.states.get(self.lock.entity_id)
        return not (state is None or state.state == "unavailable")

    def _raise_not_connected(self) -> NoReturn:
        """
        Raise the most specific reason this lock cannot be addressed.

        Overridden by a provider that can tell a transport which may come
        back from a lock that was never addressable at all -- an entity
        published by some other bridge, say -- so the error names the
        misconfiguration rather than implying it is temporary.
        """
        raise LockDisconnected("Lock not connected")

    async def _async_ensure_operational(self, *, require_device: bool = True) -> None:
        """
        Refuse to address a lock whose transport, bridge, or entity is down.

        The order is what makes the error useful: MQTT being off explains a
        missing bridge, and a missing bridge explains an unavailable entity,
        so the outermost cause is the one reported.

        ``require_device`` is off for a write on a bridge that will queue it
        for a sleeping device. Where a write is instead a round trip the
        device itself has to answer, leaving it on makes that fail up front
        as a disconnect instead of ten seconds later as a timeout.

        Two of the three checks repeat what ``_execute_rate_limited`` asked a
        moment earlier, and the repetition is load-bearing rather than
        leftover: the unmanaged-code sweep and slot allocation call the
        provider's operations directly, without that wrapper, so a helper
        trimmed to what the wrapper does not cover would leave those two paths
        addressing a lock that is not there.
        """
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")
        if not await self.async_is_integration_connected():
            self._raise_not_connected()
        if require_device and not await self.async_is_device_available():
            raise LockDisconnected("Device not available")

    @final
    @callback
    def _schedule_push_subscription(
        self,
        topic: str | None,
        ensure: Callable[[], Awaitable[None]],
        topic_label: str,
    ) -> None:
        """
        Bring a push subscription up in the background, from a sync caller.

        The whole policy for a resubscribe attempt, which both bridges reach
        the same three ways: the reconnect transition, the coordinator's
        first load, and a poll noticing the topic moved.

        A topic that cannot be resolved right now leaves a working
        subscription alone -- discovery data going transiently missing is not
        a lock that moved -- but with nothing to fall back on there is no
        push channel at all, and that is a disconnect the caller must hear
        about.

        ``ensure`` is the provider's own idempotent subscribe, run in a task
        because ``setup_push_subscription`` is synchronous. Nothing it raises
        can reach a caller from there, so everything it raises is logged --
        a lost connection quietly, since the reconnect path is already
        handling it, and anything else loudly. A disabled MQTT integration
        arrives that way too, from the ensure's own gate, which is why there
        is no second check for it here.

        ``topic_label`` names what could not be resolved: the bridges address
        different things -- one a device topic, the other a node's whole
        value tree -- and the message is what tells the reader which.
        """
        if topic is None:
            if self._push_unsubs:
                return
            raise LockDisconnected(
                f"Cannot subscribe to push updates for {self.lock.entity_id} - "
                f"no {topic_label}"
            )

        async def _subscribe_or_log() -> None:
            """Run the provider's subscribe, logging whatever it raises."""
            try:
                await ensure()
            except LockDisconnected as err:
                LOGGER.debug(
                    "Lock %s: push subscription deferred (disconnected): %s",
                    self.lock.entity_id,
                    err,
                )
            except Exception:
                LOGGER.exception(
                    "Lock %s: MQTT subscribe failed unexpectedly",
                    self.lock.entity_id,
                )

        self.hass.async_create_task(_subscribe_or_log())

    async def _async_read_slots(
        self,
        code_slots: Collection[int],
        read_slot: Callable[[int], Awaitable[SlotCredential | None]],
        *,
        transport_failure: str,
    ) -> list[User]:
        """
        Read the named slots one at a time, refusing to invent a successful poll.

        One request per index, in order, so the bridge and the lock's
        firmware answer each read before the next goes out. A parallel gather
        with per-slot timeouts can fail an entire refresh and leave the
        coordinator with no data, which makes sync skip every slot.

        ``read_slot`` returns None when the lock said nothing at all and a
        credential for anything it described, however unreadable. Both reach
        the coordinator as unreadable -- calling either empty would tell sync
        the code is gone and storm reprogramming once the lock answers again
        -- but only the silences decide whether the read was worth anything.

        What the silences decide depends on whether the lock has ever
        answered (``domain/read_health.py``):

        - **Never seen to answer.** Some locks cannot: a Zigbee2MQTT converter
          that exposes the PIN as write-only, firmware with PIN Set but no
          PIN Get. Their writes land, so silence is the answer rather than an
          outage. After ``SILENT_READS_TO_CLASSIFY`` silences in a row the
          read stops -- a lock that times out each slot would otherwise cost
          minutes per read, and allocation walks up to every slot it has --
          and the lock is asked something it can always answer
          (``_async_device_responds``). If it answers, it is recorded as not
          answering code reads. If not, it is simply out of reach, which is
          not a verdict about the lock, so the read fails as a disconnect. A read naming fewer
          slots asks again until it has heard that many silences, so an entry
          with one user classifies its lock as surely as one with ten.
        - **Recorded as not answering.** Nothing is asked except, at most once
          per ``UNANSWERED_PROBE_INTERVAL``, the first slot, in case the lock
          has started to answer. Every slot reads unreadable, which sync
          judges by the last PIN it wrote.
        - **Seen to answer.** Every slot failing at the transport is an
          outage and raises, but only for a read of two slots or more. Asking
          about one and hearing nothing is a single lost reply, routine on a
          lossy mesh (issue #1397 had a node dropping about half), and raising
          there would trip the connectivity breaker for an entry with one user
          while an entry with two on the same lock polled on untroubled. One
          silent slot among answered ones stays merely unreadable, and one
          silent poll is absorbed by the coordinator's breaker, which takes
          three in a row before it suspends anything.

        Every slot reading unreadable because the lock withholds its codes is
        different again: those are answers, so the lock counts as answering,
        and the poll counts as a success.

        Nothing about a transport that is genuinely gone rests on this
        signal. Every public operation runs ``_async_ensure_operational``
        first, which fails the read outright when the MQTT integration is
        down, the bridge's entry has gone, or the lock entity is
        unavailable -- and on both bridges that entity's availability follows
        the bridge's own status topic. Writes fail on their own path whatever
        the slot count.
        """
        if not code_slots:
            return []

        ordered = sorted(code_slots)
        health = read_health(self.hass, self.lock.entity_id)
        reads: dict[int, SlotCredential | None] = {}
        if health is ReadHealth.UNANSWERED:
            now = time.monotonic()
            if (
                self._last_unanswered_probe is None
                or now - self._last_unanswered_probe >= UNANSWERED_PROBE_INTERVAL
            ):
                self._last_unanswered_probe = now
                reads[ordered[0]] = await read_slot(ordered[0])
                if reads[ordered[0]] is not None:
                    health = self._note_answered()

        silences = 0
        if health is not ReadHealth.UNANSWERED:
            pending = [slot for slot in ordered if slot not in reads]
            while pending:
                slot = pending.pop(0)
                state = await read_slot(slot)
                if state is not None or slot not in reads:
                    reads[slot] = state
                if state is not None:
                    silences = 0
                    if health is None:
                        health = self._note_answered()
                    continue
                if health is not None:
                    continue
                silences += 1
                if silences >= SILENT_READS_TO_CLASSIFY:
                    if not await self._async_device_responds():
                        raise LockDisconnected(
                            f"{self.lock.entity_id}: answered none of "
                            f"{silences} code reads, nor anything else"
                        )
                    async_record_read_health(
                        self.hass, self.lock.entity_id, ReadHealth.UNANSWERED
                    )
                    health = ReadHealth.UNANSWERED
                    break
                if not pending:
                    # Fewer slots than it takes to judge: ask them again.
                    pending = list(ordered)

        if (
            health is ReadHealth.ANSWERED
            and len(ordered) > 1
            and all(reads.get(slot) is None for slot in ordered)
        ):
            raise LockDisconnected(
                f"{self.lock.entity_id}: every one of the {len(ordered)} requested "
                f"slot reads {transport_failure}"
            )
        return [
            user_from_slot(
                slot_num,
                SlotCredential.unreadable()
                if (state := reads.get(slot_num)) is None
                else state,
            )
            for slot_num in ordered
        ]

    async def _async_device_responds(self) -> bool:
        """
        Return whether the lock answers something other than a code read.

        What separates a lock that cannot report its codes from one that is
        out of reach. The entity's availability is the default answer; a
        provider whose entity stays available while its device is gone
        should ask the device itself.
        """
        return await self.async_is_device_available()

    def _note_answered(self) -> ReadHealth:
        """Record that the lock answered a read, and return that it does."""
        async_record_read_health(self.hass, self.lock.entity_id, ReadHealth.ANSWERED)
        return ReadHealth.ANSWERED
