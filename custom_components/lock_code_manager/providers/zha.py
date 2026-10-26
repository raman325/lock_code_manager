"""
ZHA (Zigbee Home Automation) lock provider.

Communicates with Zigbee locks via the zigpy DoorLock cluster through ZHA.
Supports push updates via cluster listeners for operation events (lock/unlock
with user ID) and programming events (PIN code changes).  When the lock does
not support programming event notifications, falls back to drift detection
polling.
"""

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any, ClassVar, Literal, NamedTuple, override

from zigpy.zcl import foundation
from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.components.zha.helpers import (
    get_zha_gateway_proxy as _get_zha_gateway_proxy,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..const import MAX_SEARCHED_SLOT
from ..model.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    SlotCredential,
    User,
    WriteResult,
    user_from_slot,
)
from ..model.exceptions import (
    CodeRejectedError,
    LockCodeManagerError,
    LockDisconnected,
    LockOperationFailed,
)
from ._base import BaseLock
from ._util import credential_named_by_source, is_masked_code

_LOGGER = logging.getLogger(__name__)

# zigpy decodes general commands from its own table, never from a cluster's.
_DEFAULT_RESPONSE = foundation.GENERAL_COMMANDS[
    foundation.GeneralCommand.Default_Response
].schema


def _is_reply(result: Any, cluster: DoorLock, reply: foundation.ZCLCommandDef) -> bool:
    """
    Return whether ``result`` is ``reply`` as the lock's own cluster decodes it.

    zigpy decodes a cluster command from the receiving cluster's own table, and
    a quirk that redefines a reply -- under the stock name or another --
    compiles a schema class of its own, so the stock class does not recognize
    a quirked lock's reply.
    """
    decoded_with = cluster.client_commands.get(reply.id)
    return decoded_with is not None and isinstance(result, decoded_with.schema)


def _write_answer(
    result: Any,
    cluster: DoorLock,
    request: foundation.ZCLCommandDef,
    response: foundation.ZCLCommandDef,
    code_slot: int,
    lock_entity_id: str,
) -> Any:
    """
    Return ``result`` if it answers the set or clear, else raise.

    zigpy pairs a reply with its request by endpoint, cluster, direction and
    transaction sequence number, so any frame arriving on those can fill the
    request -- a Default Response to another command, the other write's
    response, an event -- and its status is about something else. Only
    ``response``, or a Default Response naming ``request``, answers the write.

    Any other reply, including the raw bytes of a command zigpy could not
    decode, leaves the write's outcome unknown: the lock may have refused
    it. That raises ``LockOperationFailed``, so the worker sends the write
    again rather than confirming a write that did not land.
    """
    if _is_reply(result, cluster, response) or (
        isinstance(result, _DEFAULT_RESPONSE) and result.command_id == request.id
    ):
        return result
    # The name only: a foreign frame can carry a code.
    raise LockOperationFailed(
        f"Lock {lock_entity_id}: {request.name} on slot {code_slot} was "
        f"answered by a reply to something else "
        f"({type(result).__name__}), so whether it landed is unknown"
    )


# A Default Response's status says how the lock fared carrying the command
# out, and these say it failed to, not that it refused the code: the write's
# outcome is unknown, so it is sent again. Every other non-zero status, and
# any non-zero status in the write's own response, is the lock's refusal.
_TRANSIENT_DEFAULT_STATUSES = frozenset(
    {
        foundation.Status.FAILURE,
        foundation.Status.HARDWARE_FAILURE,
        foundation.Status.SOFTWARE_FAILURE,
    }
)


def _raise_for_status(
    answer: Any,
    request: foundation.ZCLCommandDef,
    code_slot: int,
    lock_entity_id: str,
) -> None:
    """
    Raise if ``answer``, the write's own reply, says the write did not land.

    A quirk's response may name no status field, which leaves the write
    standing; any status other than 0 rejects it, None included, except a
    Default Response's transient failure, which fails the write so the worker
    sends it again.
    """
    status = getattr(answer, "status", 0)
    if status == 0:
        return
    if isinstance(answer, _DEFAULT_RESPONSE) and status in _TRANSIENT_DEFAULT_STATUSES:
        raise LockOperationFailed(
            f"Lock {lock_entity_id}: {request.name} on slot {code_slot} failed: "
            f"status {_status_text(status)}"
        )
    raise CodeRejectedError(
        code_slot=code_slot,
        lock_entity_id=lock_entity_id,
        reason=f"{request.name} rejected: status {_status_text(status)}",
    )


def _status_text(status: Any) -> str:
    """Name a reply's status for a person: zigpy formats its enums as a repr."""
    name = getattr(status, "name", None)
    return f"{name} ({int(status)})" if name else str(status)


# The operations that count as a use, and whether each locked the door: the
# ones Zigbee2MQTT counts (``_Z2M_LOCK_ACTIONS``). Every other operation is not
# a use: a refused Personal Identification Number, card or schedule left the
# door as it was, and an unknown or non-access user operation says nothing
# about it. The one-touch, key, automatic and scheduled operations did move
# it, but the Zigbee Cluster Library reports them, like the manual lock and
# unlock, only from the manual source (zigpy's ``ManualOperatitonEventMask``),
# which names no credential; Zigbee2MQTT does not count them either.
OPERATION_TO_LOCKED: dict[int, bool] = {
    DoorLock.OperationEvent.Lock: True,
    DoorLock.OperationEvent.Manual_Lock: True,
    DoorLock.OperationEvent.Unlock: False,
    DoorLock.OperationEvent.Manual_Unlock: False,
}

# A use counts as the credential its operation event source names. The keypad
# names a Personal Identification Number and the Radio Frequency
# Identification reader a card. A radio frequency command (``_RADIO_SOURCES``)
# names a Personal Identification Number only when it names the user whose
# code it carried (an unlock command sent with one); without a user it carried
# none. Every other source -- manual, indeterminate, or one outside the Door
# Lock cluster such as the vendor extension 4 that zigbee-herdsman-converters
# names "fingerprint" -- names no credential, so the use is not reported.
SOURCE_TO_CREDENTIAL_TYPE: dict[int, CredentialType] = {
    DoorLock.OperationEventSource.Keypad: CredentialType.PIN,
    DoorLock.OperationEventSource.RFID: CredentialType.RFID,
}


_RADIO_SOURCES = frozenset({DoorLock.OperationEventSource.RF})

# Every kind an operation event can report: the table's, and the Personal
# Identification Number a radio frequency command naming its user carries.
_REPORTABLE_CREDENTIAL_TYPES = frozenset(
    {CredentialType.PIN, *SOURCE_TO_CREDENTIAL_TYPE.values()}
)

# The event masks that say whether the lock notifies a code change.
_PROGRAMMING_EVENT_MASKS = (
    DoorLock.AttributeDefs.keypad_programming_event_mask,
    DoorLock.AttributeDefs.rf_programming_event_mask,
    DoorLock.AttributeDefs.rfid_programming_event_mask,
)
_RFID_USERS = DoorLock.AttributeDefs.num_of_rfid_users_supported


class _SetupAnswers(NamedTuple):
    """What the lock said during setup; ``reads_cards`` is None for no answer."""

    programming_events: bool
    reads_cards: bool | None


def _reads_cards(success: dict[Any, Any], failure: dict[Any, Any]) -> bool | None:
    """
    Return whether the lock's answer says it has a card reader, or None.

    Only an unsupported attribute says there is no reader. None when the
    lock gave no count, or a count of zero, which nothing here treats as a
    missing reader.
    """
    if failure.get(_RFID_USERS.id) == foundation.Status.UNSUPPORTED_ATTRIBUTE:
        return False
    # A zero is taken as the lock not saying, as the PIN count is in
    # `async_get_max_slot`. Listing a kind the lock cannot produce costs
    # nothing; dropping a real card use does not.
    if not success.get(_RFID_USERS.id):
        return None
    return True


# What one ZCL command to a sleepy lock can legitimately take before zigpy
# itself gives up. Each of zigpy's three attempts waits for the radio's send
# confirmation and then `APS_REPLY_TIMEOUT_EXTENDED` (28s) for the reply --
# every battery deadbolt is an end device -- and the confirmation wait is the
# radio library's: 8s on bellows, 30s on zigpy-znp, 60s on zigpy-deconz, 120s
# on zigpy-xbee. Three attempts on XBee is 3 x (120 + 28) = 444s, and zigpy's
# per-device concurrency gate (two in flight) can hold the command behind one
# more attempt before it is even sent, so the honest worst case is nearer
# 592s. This sits above that, so zigpy's own timeout is always the one to
# claim a flaky slot and the bound here only ever claims a command nothing
# answered at all. Slow to notice a wedged transport, then -- but being wrong
# about a flaky slot would suspend a working lock, and slow is still one
# exchange rather than a walk of the whole lock.
_WORST_CASE_ZCL_COMMAND = 720.0


@dataclass(repr=False, eq=False)
class ZHALock(BaseLock):
    """
    ZHA lock provider.

    Push updates come from zigpy cluster listeners:
    - ``programming_event_notification`` (0x21): PIN added/deleted/changed
    - ``operation_event_notification`` (0x20): lock/unlock with user ID

    If the lock does not support programming event notifications (detected via
    event mask attributes), falls back to hourly drift detection polling.
    """

    _door_lock_cluster: DoorLock | None = field(init=False, default=None)
    _supports_programming_events: bool | None = field(init=False, default=None)
    # Whether the lock has a Radio Frequency Identification reader, as it
    # answered during setup; None while it has not.
    _reads_cards: bool | None = field(init=False, default=None)

    # One ZCL command per slot, each of them slow; see _WORST_CASE_ZCL_COMMAND.
    per_exchange_budget: ClassVar[float | None] = _WORST_CASE_ZCL_COMMAND

    # -- Properties ----------------------------------------------------------

    @property
    @override
    def reported_credential_types(self) -> frozenset[CredentialType]:
        """
        Return every kind an operation event can report, less a reader it lacks.

        The lock is asked during setup whether it reads cards. A card is
        left out only when the lock said it has no reader: listing a kind the
        lock cannot produce costs nothing, and dropping a real card use would
        not.
        """
        if self._reads_cards is False:
            return _REPORTABLE_CREDENTIAL_TYPES - {CredentialType.RFID}
        return _REPORTABLE_CREDENTIAL_TYPES

    @property
    @override
    def domain(self) -> str:
        """Return integration domain."""
        return ZHA_DOMAIN

    @property
    @override
    def supports_push(self) -> bool:
        """
        Return whether this lock supports push-based updates.

        Always True — we subscribe to cluster events for operation
        notifications (lock/unlock with user ID).  Programming event support
        is checked separately to decide whether drift detection is needed.
        """
        return True

    @property
    @override
    def hard_refresh_interval(self) -> timedelta | None:
        """
        Return interval for drift detection.

        One hour if the lock lacks programming event notifications, otherwise
        None (push handles code changes).
        """
        if self._supports_programming_events is False:
            return timedelta(hours=1)
        return None

    # -- Setup ---------------------------------------------------------------

    @override
    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """
        Initialize provider state and ask the lock what it supports.

        Called on initial setup AND on integration reconnect, so we clear
        cached cluster references (which may be stale after a ZHA reload)
        and re-detect programming event support.  The coordinator reads
        ``hard_refresh_interval`` during init, so detection must complete
        before coordinator creation. Whether the lock reads cards is asked
        here too, so it is known before a use arrives; the worker's first
        read after setup re-publishes each event entity's kinds.
        """
        self.unsubscribe_push_updates()
        self._door_lock_cluster = None
        await super().async_setup(config_entry)
        answers = await self._async_read_setup_attributes()
        self._supports_programming_events = answers.programming_events
        # A reconnect the lock does not answer keeps what it said before.
        if answers.reads_cards is not None:
            self._reads_cards = answers.reads_cards
        if not self._supports_programming_events:
            _LOGGER.info(
                "Lock %s: programming event notifications not supported, "
                "enabling drift detection (1 hour interval)",
                self.lock.entity_id,
            )

    # -- Cluster access ------------------------------------------------------

    def _get_door_lock_cluster(self) -> DoorLock | None:
        """Return the DoorLock cluster for this device, caching the result."""
        if self._door_lock_cluster is not None:
            return self._door_lock_cluster

        gateway = self._get_gateway()
        if not gateway:
            return None

        entity_ref = gateway.get_entity_reference(self.lock.entity_id)
        if not entity_ref:
            _LOGGER.debug("Could not find entity reference for %s", self.lock.entity_id)
            return None

        device_proxy = entity_ref.entity_data.device_proxy
        if not device_proxy:
            _LOGGER.debug("Could not find device proxy for %s", self.lock.entity_id)
            return None

        zigpy_device = device_proxy.device.device
        if not zigpy_device:
            _LOGGER.debug("Could not find zigpy device for %s", self.lock.entity_id)
            return None

        for endpoint_id, endpoint in zigpy_device.endpoints.items():
            if endpoint_id == 0:
                continue
            for cluster in endpoint.in_clusters.values():
                if cluster.cluster_id == DoorLock.cluster_id:
                    self._door_lock_cluster = cluster
                    _LOGGER.debug(
                        "Found DoorLock cluster on endpoint %s for %s",
                        endpoint_id,
                        self.lock.entity_id,
                    )
                    return cluster

        _LOGGER.warning("Could not find DoorLock cluster for %s", self.lock.entity_id)
        return None

    @override
    def describe_link_health(self) -> str | None:
        """
        Report what the coordinator last heard from this lock.

        Zigbee offers nothing equivalent to Z-Wave's per-node failure
        counters -- zigpy tracks counters on the radio application, not per
        device -- so this reports the other honest signal: the quality of
        the most recent frame received *from* the lock, and how long ago
        that was. The wording says "last frame" rather than "signal
        strength" because that is literally what zigpy stores: ``lqi`` and
        ``rssi`` are overwritten from each received packet, so they describe
        one moment rather than an average.

        Staleness carries most of the weight here. A lock that has not been
        heard from in hours while Lock Code Manager is actively writing to
        it is the Zigbee equivalent of the unanswered-command count, and it
        stays meaningful even when the radio reports no signal metrics.

        Descriptive only: an unresolvable device yields no description
        rather than an error, because this runs while a slot is already
        suspending and the repair matters more than the detail.
        """
        cluster = self._get_door_lock_cluster()
        if cluster is None:
            return None
        device = cluster.endpoint.device
        parts = []
        if (lqi := device.lqi) is not None:
            parts.append(f"signal quality {lqi} of 255")
        if (rssi := device.rssi) is not None:
            parts.append(f"{rssi} dBm")
        if (last_seen := device.last_seen) is not None:
            age = dt_util.get_age(dt_util.utc_from_timestamp(last_seen))
            parts.append(f"last heard from {age} ago")
        if not parts:
            return None
        return (
            f"Zigbee link, from the last frame this lock sent: {', '.join(parts)}. "
            f"A weak signal, or a lock not heard from recently, points to a Zigbee "
            f"range or mesh problem rather than the lock refusing the code."
        )

    async def _get_connected_cluster(self) -> DoorLock:
        """Return a connected DoorLock cluster or raise LockDisconnected."""
        cluster = self._get_door_lock_cluster()
        if not cluster:
            raise LockDisconnected("DoorLock cluster not available")
        if not await self.async_is_integration_connected():
            raise LockDisconnected("Lock not connected")
        return cluster

    # -- Helpers -------------------------------------------------------------

    def _get_gateway(self) -> Any | None:
        """
        Return the ZHA gateway proxy, or None if unavailable.

        ``get_zha_gateway_proxy`` raises ``ValueError("No gateway object
        exists")`` whenever ZHA is not loaded -- setting up, in setup retry,
        or unloaded. That is the ordinary "not connected" answer, and this is
        asked on every connection check, so it is not logged: the callers
        already report a lock whose integration is not ready.
        """
        try:
            return _get_zha_gateway_proxy(self.hass)
        except KeyError, ValueError:
            return None

    # -- Connection ----------------------------------------------------------

    @override
    async def async_is_integration_connected(self) -> bool:
        """Return whether ZHA is loaded and the device is available."""
        gateway = self._get_gateway()
        if not gateway:
            return False
        entity_ref = gateway.get_entity_reference(self.lock.entity_id)
        if not entity_ref:
            return False
        device_proxy = entity_ref.entity_data.device_proxy
        if not device_proxy:
            return False
        return device_proxy.device.available

    # -- Credential primitives -----------------------------------------------

    @override
    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set a Personal Identification Number credential on a slot.

        Bypasses ``async_call_service`` because ZHA exposes its cluster
        operations as zigpy method calls rather than Home Assistant
        services. ``cluster.set_pin_code`` raises zigpy-level errors
        (``DeliveryError``, ``asyncio.TimeoutError``) on communication
        failure — these are routed to ``LockDisconnected`` so retries
        and reconnect handling apply, mirroring the OSError branch of
        the service-call wrapper.

        Only the write's own reply can reject it, and a reply that is about
        something else fails the write so sync sends it again
        (``_write_answer``). The write's own reply with no status field
        leaves it standing, and a Default Response's transient failure
        fails it so sync sends it again (``_raise_for_status``).

        ``user_id`` is ignored; slot-only providers address the credential
        by ``credential.slot``.
        """
        code_slot = credential.slot
        cluster = await self._get_connected_cluster()
        try:
            result = await cluster.set_pin_code(
                code_slot,
                DoorLock.UserStatus.Enabled,
                DoorLock.UserType.Unrestricted,
                pin,
            )
        except Exception as err:
            raise LockDisconnected(f"Failed to set PIN: {err}") from err
        _LOGGER.debug(
            "Lock %s slot %s set_pin_code: %s",
            self.lock.entity_id,
            code_slot,
            result,
        )
        request = DoorLock.ServerCommandDefs.set_pin_code
        answer = _write_answer(
            result,
            cluster,
            request,
            DoorLock.ClientCommandDefs.set_pin_code_response,
            code_slot,
            self.lock.entity_id,
        )
        _raise_for_status(answer, request, code_slot, self.lock.entity_id)
        self._push_credential_update(code_slot, SlotCredential.known(pin))
        return WriteResult.CONFIRMED

    @override
    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Clear a Personal Identification Number from a slot.

        See ``async_set_credential`` for why bare zigpy failures route to
        ``LockDisconnected`` instead of ``LockOperationFailed``, and for
        which reply's status counts.
        """
        code_slot = ref.slot
        cluster = await self._get_connected_cluster()
        try:
            result = await cluster.clear_pin_code(code_slot)
        except Exception as err:
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err
        _LOGGER.debug(
            "Lock %s slot %s clear_pin_code: %s",
            self.lock.entity_id,
            code_slot,
            result,
        )
        request = DoorLock.ServerCommandDefs.clear_pin_code
        answer = _write_answer(
            result,
            cluster,
            request,
            DoorLock.ClientCommandDefs.clear_pin_code_response,
            code_slot,
            self.lock.entity_id,
        )
        _raise_for_status(answer, request, code_slot, self.lock.entity_id)
        self._push_credential_update(code_slot, SlotCredential.empty())
        return True

    @override
    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Read Personal Identification Number codes one index at a time.

        Returns a user per slot via the one-slot-one-user projection. The
        cluster answers a single index per round trip, so ``slots`` bounds
        the work and must be honoured.

        ``empty`` means confirmed cleared, and ``AVAILABLE`` is the only
        status that says so. ``known`` means the lock is accepting this code,
        which only ``ENABLED`` says. Every other status is ``unreadable``:
        something is there and its value cannot be trusted -- except Not
        Supported, a lock saying it cannot tell, which is silent
        (``last_silent_slots``, ``last_cannot_tell_slots``). A read that
        failed -- an error, a reply that is not this slot's answer, zigpy
        giving up on the slot -- is not a reading at all, and fails the read:
        reported as unreadable, a clear the lock acknowledged would read as
        done while the code is still there.

        Both directions matter. Reporting a clear that did not happen makes
        sync reprogram a slot that already holds a code and tells allocation
        an occupied index is free; reporting a value the lock is not
        honouring makes a code that will not open the door read as in sync.
        """
        cluster = await self._get_connected_cluster()
        scope = self.managed_slots if slots is None else slots
        if not scope:
            return []

        slot_states: dict[int, SlotCredential] = {}
        cannot_tell: set[int] = set()
        for slot_num in scope:
            try:
                # zigpy bounds each attempt and gives up after three, raising
                # its own TimeoutError inside the ladder _WORST_CASE_ZCL_COMMAND
                # describes; this bound is strictly above that, so zigpy's word
                # about a slot always comes first and only a transport that
                # answers nothing at all ends here.
                async with asyncio.timeout(_WORST_CASE_ZCL_COMMAND) as exchange:
                    result = await cluster.get_pin_code(slot_num)
                _LOGGER.debug(
                    "Lock %s slot %s get_pin_code: %s",
                    self.lock.entity_id,
                    slot_num,
                    result,
                )
                parsed = self._parse_pin_response(result, slot_num, cluster)
            except LockDisconnected:
                raise
            except TimeoutError:
                if exchange.expired():
                    # Not zigpy giving up on one flaky slot -- that is the
                    # branch below -- but a command nothing ever answered: the
                    # transport is wedged, and waiting out the rest of the
                    # walk would only say so later.
                    raise LockDisconnected(
                        f"Lock {self.lock.entity_id}: no answer for slot {slot_num} "
                        f"within {_WORST_CASE_ZCL_COMMAND:.0f}s"
                    ) from None
                raise LockOperationFailed(
                    f"Lock {self.lock.entity_id}: slot {slot_num} timed out on the mesh"
                ) from None
            except LockCodeManagerError:
                raise
            except Exception as err:
                raise LockOperationFailed(
                    f"Lock {self.lock.entity_id}: failed to read slot {slot_num}: {err}"
                ) from err

            if parsed is None:
                raise LockOperationFailed(
                    f"Lock {self.lock.entity_id}: get_pin_code reply is not slot "
                    f"{slot_num}'s answer"
                )

            user_status, pin_code = parsed
            # Available is the only status leaving this index free to write
            # to, Enabled the only one whose code the lock says it accepts.
            # Everything else holds something untrustworthy: claiming its
            # value would read as in sync while the code does not open the
            # door, and claiming it empty would hand it to allocation.
            #
            # An Enabled slot whose code comes back masked is untrustworthy
            # in the same way: a lock withholding its codes answers with an
            # asterisk per digit, which can never equal the configured PIN,
            # so confirming it makes sync reprogram the slot on every pass
            # until it is held.
            #
            # Not Supported (0xFF) is a lock that cannot report its users
            # (#1581): no code held, nor none. Silent, as Zigbee2MQTT's
            # ``not_supported_255`` is, and the core takes it from there.
            if user_status == DoorLock.UserStatus.Not_Supported:
                cannot_tell.add(slot_num)
                slot_states[slot_num] = SlotCredential.unreadable()
            elif user_status == DoorLock.UserStatus.Available:
                slot_states[slot_num] = SlotCredential.empty()
            elif (
                user_status == DoorLock.UserStatus.Enabled
                and pin_code
                and not is_masked_code(pin_code)
            ):
                slot_states[slot_num] = SlotCredential.known(pin_code)
            else:
                slot_states[slot_num] = SlotCredential.unreadable()
        self.last_silent_slots = self.last_cannot_tell_slots = frozenset(cannot_tell)
        return [user_from_slot(slot, state) for slot, state in slot_states.items()]

    @override
    async def async_get_max_slot(self) -> int | None:
        """
        Read how many Personal Identification Number users the lock supports.

        The Zigbee Cluster Library defines this
        (``num_of_pin_users_supported``), so the answer comes from the lock
        rather than from a guess. A lock that will not report it has no
        opinion, and the caller decides how far to look instead.
        """
        try:
            cluster = await self._get_connected_cluster()
            result = await cluster.read_attributes(
                [DoorLock.AttributeDefs.num_of_pin_users_supported.id],
                allow_cache=True,
                only_cache=False,
            )
            supported = (result[0] or {}).get(
                DoorLock.AttributeDefs.num_of_pin_users_supported.id
            )
        except Exception:
            _LOGGER.debug(
                "Lock %s: could not read the supported user count",
                self.lock.entity_id,
                exc_info=True,
            )
            return None

        # Zero is what a lock reports when it will not say, not a lock with
        # nowhere to write. The attribute is 16-bit, so a lock can also claim
        # a range far past anything addressable through this cluster -- and
        # this provider spends a round trip per index, so believing 65535
        # would mean tens of thousands of them before a refusal.
        if not supported:
            return None
        return min(int(supported), MAX_SEARCHED_SLOT)

    @override
    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """
        Re-read from the lock; there is no cache to invalidate.

        A lock read one slot at a time has no cache to project from, so a
        scoped refresh re-reads every managed slot plus ``slots``: the
        coordinator replaces its data with the answer, which must still name
        them all.
        """
        return await self.async_get_usercodes(
            None if slots is None else self.managed_slots | frozenset(slots)
        )

    # -- Response parsing ----------------------------------------------------

    @staticmethod
    def _parse_pin_response(
        result: Any, slot_num: int, cluster: DoorLock
    ) -> tuple[int, str] | None:
        """
        Extract (user_status, pin_code) from slot ``slot_num``'s get_pin_code reply.

        ``None`` for a reply that is not recognizably that slot's answer: one
        that is not a Get PIN Code Response as ``cluster`` decodes it
        (``_is_reply``), or that names another user. zigpy pairs a reply with
        its request by endpoint, cluster, direction and transaction sequence
        number, not by the user it names, so either can fill this slot's
        read. Reporting such a reply as ``Available`` would answer "this slot
        is free" from a reply that was never about it.
        """
        if not _is_reply(
            result, cluster, DoorLock.ClientCommandDefs.get_pin_code_response
        ):
            return None
        # A quirk's redefinition of the reply may name its fields otherwise.
        if getattr(result, "user_id", None) != slot_num or not hasattr(
            result, "user_status"
        ):
            return None
        pin = getattr(result, "code", "") or ""
        if isinstance(pin, bytes):
            pin = pin.decode("utf-8", errors="ignore")
        return result.user_status, str(pin)

    # -- Push updates --------------------------------------------------------

    @callback
    @override
    def setup_push_subscription(self) -> None:
        """Subscribe to DoorLock cluster events."""
        if self._push_unsubs:
            return

        cluster = self._get_door_lock_cluster()
        if not cluster:
            raise LockDisconnected(
                "DoorLock cluster not available for push subscription"
            )

        cluster.add_listener(self)
        self._register_push_unsub(lambda: cluster.remove_listener(self))
        _LOGGER.debug(
            "Lock %s: subscribed to DoorLock cluster events",
            self.lock.entity_id,
        )

    @callback
    @override
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from DoorLock cluster events."""
        if not self._push_unsubs:
            return
        self._clear_push_unsubs()
        _LOGGER.debug(
            "Lock %s: unsubscribed from DoorLock cluster events",
            self.lock.entity_id,
        )

    # -- Cluster listener callbacks ------------------------------------------

    def cluster_command(
        self,
        tsn: int,
        command_id: int,
        args: Any,
    ) -> None:
        """Handle incoming cluster commands from the lock (zigpy listener)."""
        if command_id == DoorLock.ClientCommandDefs.programming_event_notification.id:
            self._handle_programming_event(args)
        elif command_id == DoorLock.ClientCommandDefs.operation_event_notification.id:
            self._handle_operation_event(args)

    def _handle_programming_event(self, args: Any) -> None:
        """
        Handle programming event (PIN added/deleted/changed).

        Triggers a coordinator refresh to pick up the new code state.
        """
        try:
            event_code = (
                args.program_event_code
                if hasattr(args, "program_event_code")
                else args[1]
            )
            user_id = args.user_id if hasattr(args, "user_id") else args[2]
        except AttributeError, IndexError, TypeError:
            _LOGGER.warning(
                "Lock %s: could not parse programming event, "
                "triggering refresh as safety net: %s",
                self.lock.entity_id,
                args,
            )
            # Refresh anyway — we know something changed on the lock
            if self.coordinator:
                self._create_background_task(
                    self.coordinator.async_request_refresh(),
                    f"Refresh {self.lock.entity_id} after unparseable programming event",
                )
            return

        _LOGGER.debug(
            "Lock %s: programming event code=%s user_id=%s",
            self.lock.entity_id,
            event_code,
            user_id,
        )
        if self.coordinator:
            self._create_background_task(
                self.coordinator.async_request_refresh(),
                f"Refresh {self.lock.entity_id} after programming event",
            )

    def _handle_operation_event(self, args: Any) -> None:
        """
        Handle operation event (lock/unlock with user ID).

        Fires a code slot event so automations can react to lock usage.
        """
        try:
            source = (
                args.operation_event_source
                if hasattr(args, "operation_event_source")
                else args[0]
            )
            event_code = (
                args.operation_event_code
                if hasattr(args, "operation_event_code")
                else args[1]
            )
            user_id = args.user_id if hasattr(args, "user_id") else args[2]
        except AttributeError, IndexError, TypeError:
            _LOGGER.warning(
                "Lock %s: could not parse operation event: %s",
                self.lock.entity_id,
                args,
            )
            return

        _LOGGER.debug(
            "Lock %s: operation event source=%s code=%s user_id=%s",
            self.lock.entity_id,
            source,
            event_code,
            user_id,
        )

        if (to_locked := OPERATION_TO_LOCKED.get(event_code)) is None:
            _LOGGER.debug(
                "Lock %s: operation event code %s is not a lock or unlock "
                "that counts as a use, so the use is not reported",
                self.lock.entity_id,
                event_code,
            )
            return

        if (
            credential_type := credential_named_by_source(
                source,
                user_id,
                names=SOURCE_TO_CREDENTIAL_TYPE,
                radio=_RADIO_SOURCES,
            )
        ) is None:
            _LOGGER.debug(
                "Lock %s: operation event source %s names no credential, so "
                "the use is not reported",
                self.lock.entity_id,
                source,
            )
            return

        self.async_fire_code_slot_event(
            code_slot=user_id if user_id > 0 else None,
            to_locked=to_locked,
            credential_type=credential_type,
        )

    # -- Programming event support detection ---------------------------------

    async def _async_read_setup_attributes(self) -> _SetupAnswers:
        """
        Ask the lock what setup needs to know, in one read.

        Whether it sends programming event notifications when codes change,
        from its event mask attributes, and whether it reads cards, from its
        count of Radio Frequency Identification users. One read, so the
        second question costs setup no further exchange. zigpy answers each
        attribute on its own, so one the lock refuses says nothing about the
        others.
        """
        cluster = self._get_door_lock_cluster()
        if not cluster:
            return _SetupAnswers(programming_events=False, reads_cards=None)
        try:
            success, failure = await cluster.read_attributes(
                [attr.id for attr in (*_PROGRAMMING_EVENT_MASKS, _RFID_USERS)],
                allow_cache=False,
                only_cache=False,
            )
        except Exception:
            _LOGGER.debug(
                "Lock %s: could not read its programming event masks or card "
                "user count",
                self.lock.entity_id,
                exc_info=True,
            )
            return _SetupAnswers(programming_events=False, reads_cards=None)
        return _SetupAnswers(
            programming_events=self._sends_programming_events(success),
            reads_cards=_reads_cards(success, failure),
        )

    def _sends_programming_events(self, success: dict[Any, Any]) -> bool:
        """Return whether any event mask the lock answered with is set."""
        for attr in _PROGRAMMING_EVENT_MASKS:
            value = success.get(attr.id, success.get(attr.name))
            if value is not None and value != 0:
                _LOGGER.debug(
                    "Lock %s: supports programming events (%s [0x%04x]=0x%04x)",
                    self.lock.entity_id,
                    attr.name,
                    attr.id,
                    value,
                )
                return True

        _LOGGER.debug(
            "Lock %s: no programming event mask attributes found, "
            "will use drift detection fallback",
            self.lock.entity_id,
        )
        return False
