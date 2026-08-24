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
  rather than re-deriving the bridge's internals.

What it deliberately does NOT host is anything whose semantics differ between
the two: subscription lifecycles (one bridge has a per-node value tree, the
other a per-device topic, and zwave-js-ui also runs an api transport with its
own lifetime), the payload projections, the api client, and the poll cadences
-- whose intervals happen to coincide today for reasons that have nothing to
do with each other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import NoReturn

from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled

from ..domain.credentials import User, user_from_slot
from ..domain.exceptions import LockDisconnected
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import entity_state_is_available


@dataclass(repr=False, eq=False)
class BaseMqttLock(BaseLock):
    """Base class for a lock addressed through an MQTT bridge."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MQTT_DOMAIN

    async def async_is_device_available(self) -> bool:
        """Return whether the lock entity reports an operational state."""
        return entity_state_is_available(self.hass, self.lock.entity_id)

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
        """
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")
        if not await self.async_is_integration_connected():
            self._raise_not_connected()
        if require_device and not await self.async_is_device_available():
            raise LockDisconnected("Device not available")

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
        slot reading unreadable, and only the first raises. An all-unreadable
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
        """
        if not code_slots:
            return []

        reads = {slot_num: await read_slot(slot_num) for slot_num in sorted(code_slots)}
        if all(state is None for state in reads.values()):
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
