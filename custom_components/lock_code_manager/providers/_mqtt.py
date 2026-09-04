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
from typing import ClassVar, NoReturn, final

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.core import callback

from ..domain.credentials import User, user_from_slot
from ..domain.exceptions import LockDisconnected
from ..domain.models import SlotCredential
from ._base import BaseLock
from .const import LOGGER


@dataclass(repr=False, eq=False)
class BaseMqttLock(BaseLock):
    """Base class for a lock addressed through an MQTT bridge."""

    # What one slot's read costs on this transport. `_async_read_slots` walks
    # the managed slots one at a time -- deliberately, so the bridge and the
    # lock's firmware answer each read before the next goes out -- so the
    # stall budget is this times the number of slots, not a flat figure.
    _per_slot_read_budget: ClassVar[float] = 60.0

    @property
    def operation_timeout_seconds(self) -> float:
        """Scale with the slot walk rather than reporting a slow lock as dead."""
        return max(
            super().operation_timeout_seconds,
            self._per_slot_read_budget * max(len(self.managed_slots), 1),
        )

    # Whether any slot read on this instance has ever come back with something
    # the lock said, rather than silence. Latched on, never cleared: what it
    # records is a property of the bridge and the lock's firmware, and neither
    # stops being able to answer reads because a poll went badly.
    _reads_have_succeeded: bool = field(init=False, default=False)

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
        return await self.async_get_usercodes()

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

        Every slot failing at the transport is a different thing from every
        slot reading unreadable, and only the first can raise. An all-unreadable
        return is a successful poll as far as the coordinator is concerned:
        it resets the connectivity breaker, un-suspends the slots, and lets
        them re-fail on the next tick -- an oscillation on the order of
        seconds, flipping every slot in and out of sync. A lock configured to
        withhold its codes answers every request and is perfectly healthy, so
        its reads are data and must not count towards this.

        One silent slot among answered ones stays merely unreadable. A lock
        that answers most requests and drops one is a weak link rather than a
        lost transport, and the caller's ``transport_failure`` phrase names
        what silence means on its own bridge.

        Silence is only evidence of an outage against a transport that has
        been shown capable of the opposite, so two conditions both have to
        hold before this raises.

        Two slots is the smallest read it can judge. Asking about one and
        hearing nothing is a single lost reply, and on a lossy mesh that is
        routine -- issue #1397 had a node dropping roughly half its
        responses. Raising there would trip the connectivity breaker on
        ordinary noise for an entry with one user, while an entry with two on
        the same lock, losing replies at the same rate, polled on untroubled.
        How many people a household has must not decide whether its lock is
        reported unreachable.

        This instance must also have read something successfully at least
        once. Some bridges cannot answer a read at all and never could: a
        Zigbee2MQTT converter that exposes the PIN as write-only, a node whose
        firmware implements User Code Set without Get. Those locks worked --
        every poll came back all-unreadable and every write landed -- until an
        all-silent rule that did not ask whether reading was ever possible
        started declaring them gone, tripping the breaker and suspending the
        writes along with the reads that were never going to work. Until the
        lock proves it can be read, silence is the answer rather than the
        absence of one, and a lock that can never prove it is left exactly as
        it was before this rule existed.

        One silent poll is not by itself an outage, and does not need to be
        one here: raising records a single connectivity failure, and the
        coordinator's breaker takes three in a row before it suspends
        anything. A #1397-class link that loses both replies of one poll is
        absorbed there, by the mechanism that already exists for it.

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

        reads = {slot_num: await read_slot(slot_num) for slot_num in sorted(code_slots)}
        if any(state is not None for state in reads.values()):
            self._reads_have_succeeded = True
        elif len(reads) > 1 and self._reads_have_succeeded:
            raise LockDisconnected(
                f"{self.lock.entity_id}: every one of the {len(reads)} requested "
                f"slot reads {transport_failure}"
            )
        return [
            user_from_slot(
                slot_num, SlotCredential.unreadable() if state is None else state
            )
            for slot_num, state in reads.items()
        ]
