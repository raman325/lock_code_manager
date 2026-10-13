"""
A stateful Matter door lock, faked where Home Assistant's client meets the server.

The fake answers the Door Lock cluster commands Home Assistant's
``lock_helpers`` and the provider send for one node, at the client's
``send_command`` -- the websocket's place -- so ``MatterClient.send_device_command``
and its ``dataclass_to_dict`` encoding, Home Assistant's ``lock_helpers`` and the
provider all run for real above it. Node events go out through the client's own
``_handle_event_message``, so subscribers are matched by the client's filters.

The lock's side follows the Matter SDK's door-lock server: what it stores, what
it answers, and the ``LockUserChange`` events it logs. Every modeled behavior
cites the line it follows. References, read at:

- connectedhomeip ``v1.4.2.0`` (``06523c22640ceb8b89f9a11ff2325a4481a178a3``):
  ``DLS`` = src/app/clusters/door-lock-server/door-lock-server.cpp.
- matter-python-client 1.4.0 (the version Home Assistant 2026.9.4 pins):
  ``MS_CLIENT`` = matter_server/client/client.py, ``MS_ERR`` =
  matter_server/common/errors.py.
- Home Assistant 2026.9.4: ``HA_LH`` = homeassistant/components/matter/lock_helpers.py.

- matterjs-server ``b9e19f73`` (the server matter-python-client 1.4.0 is
  published from): ``MJS_WS`` = packages/ws-controller/src/server/
  WebSocketControllerHandler.ts, ``MJS_CMD`` = packages/ws-controller/src/
  controller/ControllerCommandHandler.ts, ``MJS_TYPES`` = packages/
  ws-controller/src/types/WebSocketMessageTypes.ts.
- matter.js ``36927903``: ``MJS_SRE`` = packages/types/src/common/
  StatusResponseError.ts.

How a device's status reaches Home Assistant: the server answers a failed
command with ``error_code`` (a ``ServerError``'s own code, else 0) and the
error's message as ``details`` (MJS_WS:862-870), and the client raises the
class registered for the code (``MS_ERR``: 0 is ``UnknownError``). A device's
Interaction Model status is a matter.js ``StatusResponseError``, whose message
is the status in words and its code, with the cluster code when there is one:
``Invalid command (code 133)``, ``Failure (code 1; cluster code 3)``
(MJS_SRE:62-85).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from functools import partial
import inspect
import time
from typing import Any, Literal, Self
from unittest.mock import DEFAULT, MagicMock

from chip.clusters import Objects as clusters
from matter_server.client.client import MatterClient
from matter_server.client.models.node import MatterNode
from matter_server.common.errors import MatterError, NodeNotResolving, UnknownError
from matter_server.common.helpers.util import dataclass_from_dict
from matter_server.common.models import APICommand, EventType, MatterNodeEvent
import pytest

from tests.common import async_run_until_quiet, track_fault, untrack_fault

DoorLock = clusters.DoorLock
_DL = DoorLock.id
_PIN = DoorLock.Enums.CredentialTypeEnum.kPin
_ENABLED = DoorLock.Enums.UserStatusEnum.kOccupiedEnabled

# The fabric Home Assistant's controller is on, and another admin's (a vendor
# app, the lock's own keypad programming): ``creatorFabricIndex`` tells them
# apart, and only the creating fabric may modify a record (DLS:2047-2061, 793-800).
HA_FABRIC = 1
OTHER_FABRIC = 2
# The node id the SDK reports as the source of a remote change (DLS:3364-3380):
# the server's controller, which matterjs-server always gives this id
# (packages/ws-controller/src/controller/ControllerNode.ts:78). Home
# Assistant's fabric index is 1 when it commissioned the lock first.
_HA_NODE_ID = 112233
# Door Lock event priorities (matter.js packages/model/src/standard/elements/
# door-lock-cluster.element.ts:181, 216): LockOperation is critical,
# LockUserChange info -- the Interaction Model's 2 and 1.
_EVENT_PRIORITY = {
    DoorLock.Events.LockOperation.event_id: 2,
    DoorLock.Events.LockUserChange.event_id: 1,
}

# appclusters, 5.2.4.34 (DLS:455): DOOR_LOCK_MAX_USER_NAME_SIZE, the SDK's
# default; a vendor can build with another.
MAX_USER_NAME = 10
# ClearUser's "all users" index (DLS:621, 641).
CLEAR_ALL = 0xFFFE

# Door Lock attributes the fake reads off the node: what the lock reports it holds.
_ATTR_FEATURE_MAP = DoorLock.Attributes.FeatureMap
_ATTR_USERS = DoorLock.Attributes.NumberOfTotalUsersSupported
_ATTR_PIN_USERS = DoorLock.Attributes.NumberOfPINUsersSupported
_ATTR_PER_USER = DoorLock.Attributes.NumberOfCredentialsSupportedPerUser
_ATTR_MIN_PIN = DoorLock.Attributes.MinPINCodeLength
_ATTR_MAX_PIN = DoorLock.Attributes.MaxPINCodeLength


class ImStatus(IntEnum):
    """Interaction Model statuses a Door Lock command can fail with."""

    FAILURE = 0x01
    INVALID_COMMAND = 0x85
    UNSUPPORTED_COMMAND = 0x81
    BUSY = 0x9C
    UNSUPPORTED_CLUSTER = 0xC3


DlStatus = DoorLock.Enums.DlStatus


def im_error(status: int, cluster_code: int | None = None) -> UnknownError:
    """
    Return what the client raises for a command the lock answered ``status``.

    A status matter.js has no name for is an ``UnknownStatusResponseError``,
    whose message carries no code (MJS_SRE:50-56).
    """
    if (words := _IM_WORDS.get(status)) is None:
        return UnknownError("Unknown status response")
    code = f"code {status}"
    if cluster_code is not None:
        code += f"; cluster code {cluster_code}"
    return UnknownError(f"{words} ({code})")


# matter.js's ``Status`` names as its errors spell them: decamelized,
# capitalized (MJS_SRE:72-74).
_IM_WORDS = {
    ImStatus.FAILURE: "Failure",
    ImStatus.INVALID_COMMAND: "Invalid command",
    ImStatus.UNSUPPORTED_COMMAND: "Unsupported command",
    ImStatus.BUSY: "Busy",
    ImStatus.UNSUPPORTED_CLUSTER: "Unsupported cluster",
}


class FaultKind(StrEnum):
    """How an exchange with the lock goes wrong; see the knobs on the fake."""

    IGNORE = "ignore"
    REFUSE = "refuse"
    FAIL = "fail"
    HANG = "hang"
    SLOW = "slow"


# The commands the fake answers, by the name ``send_device_command`` sends.
MatterCommand = Literal[
    "GetUser",
    "SetUser",
    "ClearUser",
    "SetCredential",
    "GetCredentialStatus",
    "ClearCredential",
]
# Only a command that stores or removes something can be ignored or refused.
_WRITES = frozenset({"SetUser", "ClearUser", "SetCredential", "ClearCredential"})
_WRITE_ONLY_FAULTS = frozenset({FaultKind.IGNORE, FaultKind.REFUSE})


@dataclass(eq=False)
class NodeFault:
    """
    One way the lock misbehaves on its commands, until cleared.

    Named and shaped like ``tests.common.LockFault`` and the Z-Wave fake's
    ``NodeFault`` (``times``, ``hits``, ``held``, ``async_wait_held``, context
    manager). ``command`` is the Door Lock command it meets (None: any);
    ``index`` the user or credential index it addresses (None: any).
    """

    lock: FakeMatterLock
    kind: FaultKind
    command: MatterCommand | None
    index: int | None
    times: int | None = None
    seconds: float = 0.0
    error: MatterError | None = None
    status: int | None = None
    hits: int = 0
    held: int = 0
    _released: asyncio.Event = field(default_factory=asyncio.Event)

    def __enter__(self) -> Self:
        """Return the fault, to clear on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Clear the fault."""
        self.clear()

    def matches(self, command: str, index: int | None) -> bool:
        """Return whether a ``command`` addressing ``index`` meets this fault."""
        return (self.command is None or self.command == command) and (
            self.index is None or self.index == index
        )

    def clear(self) -> None:
        """Stop matching commands, and let any command held go."""
        if self in self.lock.faults:
            self.lock.faults.remove(self)
        untrack_fault(self)
        self._released.set()

    async def async_wait_held(self) -> None:
        """Run the loop until quiet, then fail unless this holds a command now."""
        await async_run_until_quiet()
        if not self.held:
            pytest.fail(
                f"no {self.command or 'command'} held by the {self.kind} fault "
                f"(matched {self.hits})"
            )

    async def async_before(self) -> None:
        """Act out what happens before the lock answers."""
        if self.kind is FaultKind.FAIL:
            raise self.error or NodeNotResolving(
                f"Node {self.lock.node.node_id} is not resolving"
            )
        if self.kind is FaultKind.HANG:
            self.held += 1
            try:
                await self._released.wait()
            finally:
                self.held -= 1
        if self.kind is FaultKind.SLOW:
            self.held += 1
            try:
                with suppress(TimeoutError):
                    async with asyncio.timeout(self.seconds):
                        await self._released.wait()
            finally:
                self.held -= 1


@dataclass
class LockUser:
    """A user record as the SDK stores it (``EmberAfPluginDoorLockUserInfo``)."""

    name: str = ""
    unique_id: int | None = None
    status: int = _ENABLED
    user_type: int = DoorLock.Enums.UserTypeEnum.kUnrestrictedUser
    credential_rule: int = DoorLock.Enums.CredentialRuleEnum.kSingle
    credentials: list[tuple[int, int]] = field(default_factory=list)
    created_by: int | None = HA_FABRIC
    modified_by: int | None = HA_FABRIC


@dataclass
class LockCredential:
    """A credential record (``EmberAfPluginDoorLockCredentialInfo``)."""

    data: bytes
    created_by: int | None = HA_FABRIC
    modified_by: int | None = HA_FABRIC


@dataclass
class Exchange:
    """One command the lock answered, for tests to inspect."""

    command: str
    index: int | None
    payload: dict[str, Any]
    # When the lock saw it, on the clock the provider paces its turns by.
    # Looked up per call: the bare function, bound here, escapes freezegun.
    at: float = field(default_factory=lambda: time.monotonic())  # noqa: PLW0108


class FakeMatterLock:
    """
    A Matter door lock node behind the server's ``device_command`` API.

    Install it on the ``matter_client`` fixture; it answers this node's Door
    Lock commands at ``send_command`` and passes everything else on to what
    the fixture answered before. Its capacities are the node's own Door Lock
    attributes, read at install: ``NumberOfTotalUsersSupported`` user
    indices, ``NumberOfPINUsersSupported`` PIN credential indices,
    ``NumberOfCredentialsSupportedPerUser`` credentials a user may hold, and
    ``Min``/``MaxPINCodeLength``.

    The lock's table is ``users`` (by user index) and ``credentials`` (by
    (credential type, index)); ``holds(index)`` is the PIN at a credential
    index. Records Home Assistant wrote carry ``HA_FABRIC``; ``program``
    writes as another admin or the keypad would. ``max_user_name`` is the
    longest name the lock stores: ten characters is the SDK's default, and a
    longer one is refused as InvalidCommand (DLS:454-462).

    Faults (``ignore``, ``refuse``, ``fail``, ``hang``, ``slow``) act on the
    Door Lock commands; see ``NodeFault``. ``commands`` records every command
    answered, in order.
    """

    def __init__(
        self,
        client: MagicMock,
        node: MatterNode,
        *,
        max_user_name: int = MAX_USER_NAME,
    ) -> None:
        """Wrap ``node``'s Door Lock endpoint, and install on ``client``."""
        self.client = client
        self.node = node
        self.max_user_name = max_user_name
        self.endpoint_id = next(
            endpoint.endpoint_id
            for endpoint in node.endpoints.values()
            if endpoint.has_cluster(DoorLock)
        )
        endpoint = node.endpoints[self.endpoint_id]
        self.feature_map: int = endpoint.get_attribute_value(None, _ATTR_FEATURE_MAP)
        self.max_users: int = endpoint.get_attribute_value(None, _ATTR_USERS)
        self.pin_slots: int = endpoint.get_attribute_value(None, _ATTR_PIN_USERS)
        self.per_user: int = endpoint.get_attribute_value(None, _ATTR_PER_USER)
        self.pin_lengths: tuple[int, int] = (
            endpoint.get_attribute_value(None, _ATTR_MIN_PIN),
            endpoint.get_attribute_value(None, _ATTR_MAX_PIN),
        )
        self.users: dict[int, LockUser] = {}
        self.credentials: dict[tuple[int, int], LockCredential] = {}
        self.faults: list[NodeFault] = []
        self.commands: list[Exchange] = []
        self._event_number = 0
        self._in_flight: dict[tuple[str, str], asyncio.Future[Any]] = {}
        # Events logged while a command is being answered, held for after it.
        self._answering = 0
        self._queued_events: list[MatterNodeEvent] = []

        # The client's own encoding and event dispatch run above the fake;
        # the autospec mock would otherwise swallow both.
        client._subscribers = {}
        client.logger = MagicMock()
        client.subscribe_events.side_effect = partial(
            MatterClient.subscribe_events, client
        )
        client.send_device_command.side_effect = partial(
            MatterClient.send_device_command, client
        )
        self._previous = client.send_command.side_effect
        client.send_command.side_effect = self._async_dispatch

    # ------------------------------------------------------------------
    # The lock, seen from the test
    # ------------------------------------------------------------------

    @property
    def supports_users(self) -> bool:
        """Return whether the lock advertises the USR feature (DLS:419)."""
        return bool(self.feature_map & DoorLock.Bitmaps.Feature.kUser)

    def holds(self, index: int) -> str | None:
        """Return the PIN at credential ``index``, or None if it holds none."""
        credential = self.credentials.get((_PIN, index))
        return None if credential is None else credential.data.decode()

    @property
    def codes(self) -> dict[int, str]:
        """Return every PIN the lock holds, by credential index."""
        return {
            index: credential.data.decode()
            for (kind, index), credential in self.credentials.items()
            if kind == _PIN
        }

    def owner(self, index: int) -> int | None:
        """Return the user index holding PIN credential ``index``, if any."""
        return next(
            (
                user_index
                for user_index, user in self.users.items()
                if (_PIN, index) in user.credentials
            ),
            None,
        )

    def sent(self, command: MatterCommand) -> list[Exchange]:
        """Return each ``command`` the lock answered, in order."""
        return [exchange for exchange in self.commands if exchange.command == command]

    def program(
        self,
        user_index: int,
        pin: str | None,
        *,
        name: str = "",
        credential_index: int | None = None,
        status: int = _ENABLED,
    ) -> None:
        """
        Change a user and its PIN as another admin or the keypad does, and announce it.

        A change not made over Matter is logged as a ``LockUserChange`` with no
        fabric or source node, and what it writes carries no fabric index
        (DLS:563-571: they are reported only for ``kMatterIM`` changes); a
        record Home Assistant created keeps its creator. The PIN moves to this
        user from any other holding it, and is logged as an add or a modify of
        the credential, by whether it existed. A PIN of None removes the user
        and every credential it holds.
        """
        index = user_index if credential_index is None else credential_index
        if pin is None:
            user = self.users.pop(user_index, None)
            for credential in user.credentials if user else ():
                self.credentials.pop(credential, None)
            self._log_user_change(
                DoorLock.Enums.LockDataTypeEnum.kUserIndex,
                DoorLock.Enums.DataOperationTypeEnum.kClear,
                user_index,
                user_index,
                source=DoorLock.Enums.OperationSourceEnum.kKeypad,
            )
            return
        existed = (_PIN, index) in self.credentials
        for other_index, other in self.users.items():
            if other_index != user_index and (_PIN, index) in other.credentials:
                other.credentials.remove((_PIN, index))
        user = self.users.setdefault(
            user_index, LockUser(name=name, created_by=None, modified_by=None)
        )
        if name:
            user.name = name
        user.status = status
        user.modified_by = None
        if (_PIN, index) not in user.credentials:
            user.credentials.append((_PIN, index))
        previous = self.credentials.get((_PIN, index))
        self.credentials[(_PIN, index)] = LockCredential(
            pin.encode(),
            created_by=previous.created_by if previous else None,
            modified_by=None,
        )
        self._log_user_change(
            DoorLock.Enums.LockDataTypeEnum.kPin,
            DoorLock.Enums.DataOperationTypeEnum.kModify
            if existed
            else DoorLock.Enums.DataOperationTypeEnum.kAdd,
            user_index,
            index,
            source=DoorLock.Enums.OperationSourceEnum.kKeypad,
        )

    def keypad_unlock(
        self, credential_index: int, *, user_index: int | None = None
    ) -> None:
        """
        Unlock with the PIN at ``credential_index``, as the lock logs it.

        A ``LockOperation`` event: unlocked, from the keypad, naming the user
        and the credential used (appclusters 5.2.11.2). ``user_index`` defaults
        to the user holding the credential.
        """
        self._log_event(
            DoorLock.Events.LockOperation,
            {
                "lockOperationType": DoorLock.Enums.LockOperationTypeEnum.kUnlock,
                "operationSource": DoorLock.Enums.OperationSourceEnum.kKeypad,
                "userIndex": self.owner(credential_index)
                if user_index is None
                else user_index,
                "fabricIndex": None,
                "sourceNode": None,
                "credentials": [
                    {"credentialType": _PIN, "credentialIndex": credential_index}
                ],
            },
        )

    # ------------------------------------------------------------------
    # Faults
    # ------------------------------------------------------------------

    def _add_fault(
        self,
        kind: FaultKind,
        command: MatterCommand | None,
        index: int | None,
        **kwargs: Any,
    ) -> NodeFault:
        """Install a fault, refusing one that cannot happen to its command."""
        if kind in _WRITE_ONLY_FAULTS and command not in _WRITES:
            raise ValueError(f"a {kind} fault needs a command that writes")
        fault = NodeFault(self, kind, command, index, **kwargs)
        self.faults.append(fault)
        track_fault(fault)
        return fault

    def ignore(
        self, command: MatterCommand, index: int | None = None, **kw: Any
    ) -> NodeFault:
        """Answer a write as done, and change nothing."""
        return self._add_fault(FaultKind.IGNORE, command, index, **kw)

    def refuse(
        self,
        command: MatterCommand,
        index: int | None = None,
        *,
        status: int | None = None,
        **kw: Any,
    ) -> NodeFault:
        """
        Refuse a write, changing nothing.

        ``SetCredential`` answers in band with ``status`` (``DlStatus``,
        ``kFailure`` by default); the other writes fail with Interaction Model
        ``status`` (``ImStatus.FAILURE`` by default).
        """
        return self._add_fault(FaultKind.REFUSE, command, index, status=status, **kw)

    def fail(
        self,
        command: MatterCommand | None = None,
        index: int | None = None,
        *,
        error: MatterError | None = None,
        **kw: Any,
    ) -> NodeFault:
        """
        Never answer: the command raises ``error``.

        The default is what the server raises for a node it cannot reach,
        ``NodeNotResolving`` (error code 4, with the message MJS_TYPES:118-119
        gives it).
        """
        return self._add_fault(FaultKind.FAIL, command, index, error=error, **kw)

    def hang(
        self, command: MatterCommand | None = None, index: int | None = None, **kw: Any
    ) -> NodeFault:
        """Hold the command until the fault is cleared; then answer it."""
        return self._add_fault(FaultKind.HANG, command, index, **kw)

    def slow(
        self,
        command: MatterCommand | None = None,
        index: int | None = None,
        *,
        seconds: float,
        **kw: Any,
    ) -> NodeFault:
        """Make the command take ``seconds`` on the clock, then answer it."""
        return self._add_fault(FaultKind.SLOW, command, index, seconds=seconds, **kw)

    def clear_faults(self) -> None:
        """Clear every fault installed on this lock, releasing what they hold."""
        for fault in list(self.faults):
            fault.clear()

    async def _async_meet(self, command: str, index: int | None) -> NodeFault | None:
        """Return the fault a command meets, after acting out what it does first."""
        fault = next(
            (
                f
                for f in self.faults
                if not (f.times is not None and f.hits >= f.times)
                and f.matches(command, index)
            ),
            None,
        )
        if fault is None:
            return None
        fault.hits += 1
        await fault.async_before()
        return fault

    # ------------------------------------------------------------------
    # The server's device_command, answered for this node
    # ------------------------------------------------------------------

    async def _async_dispatch(self, command: str, *args: Any, **kwargs: Any) -> Any:
        if (
            command != APICommand.DEVICE_COMMAND
            or kwargs.get("node_id") != self.node.node_id
            or kwargs.get("cluster_id") != _DL
        ):
            if self._previous is None:
                # The mock answers with its return_value, as before the fake.
                return DEFAULT
            result = self._previous(command, *args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        if kwargs.get("endpoint_id") != self.endpoint_id:
            # The server sends the command to an endpoint with no Door Lock
            # cluster, which answers UnsupportedCluster.
            raise im_error(ImStatus.UNSUPPORTED_CLUSTER)
        name: str = kwargs["command_name"]
        payload: dict[str, Any] = kwargs["payload"]
        # The server sends a command identical to one still in flight to the
        # lock only once, and answers both with its result (MJS_CMD:1303-1340).
        key = (name, repr(payload))
        if (in_flight := self._in_flight.get(key)) is None:
            in_flight = asyncio.ensure_future(self._async_answer(name, payload))
            self._in_flight[key] = in_flight
            in_flight.add_done_callback(lambda _: self._in_flight.pop(key, None))
        try:
            return await asyncio.shield(in_flight)
        finally:
            # What the command logged goes out after its answer.
            asyncio.get_running_loop().call_soon(self._flush_events)

    async def _async_answer(self, name: str, payload: dict[str, Any]) -> Any:
        """Answer one Door Lock command, meeting any fault installed for it."""
        self._answering += 1
        try:
            return await self._async_answer_unqueued(name, payload)
        finally:
            self._answering -= 1

    async def _async_answer_unqueued(self, name: str, payload: dict[str, Any]) -> Any:
        handler: Callable[[dict[str, Any]], Any] | None = getattr(
            self, f"_cmd_{name}", None
        )
        if handler is None:
            raise NotImplementedError(f"the fake lock does not model {name}")
        index = _addressed_index(payload)
        self.commands.append(Exchange(name, index, payload))
        fault = await self._async_meet(name, index)
        if fault is not None and fault.kind is FaultKind.REFUSE:
            if name == "SetCredential":
                return _set_credential_response(
                    fault.status if fault.status is not None else DlStatus.kFailure,
                    None,
                    self._next_free_credential(_cred_index(payload)),
                )
            raise im_error(fault.status or ImStatus.FAILURE)
        if fault is not None and fault.kind is FaultKind.IGNORE:
            if name == "SetCredential":
                return _set_credential_response(
                    DlStatus.kSuccess,
                    None,
                    self._next_free_credential(_cred_index(payload)),
                )
            return None
        return await handler(payload)

    # Users (DLS:411-662) ----------------------------------------------------

    async def _cmd_GetUser(self, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        """
        Answer GetUser (DLS:510-590).

        An index past the lock's users is InvalidCommand. The answer names the
        next occupied user after it, if any; an empty index carries no
        user-specific fields (DLS:555-582), and fabric indices are reported
        only for records written over Matter (DLS:563-571).
        """
        self._require_users()
        index: int = payload["userIndex"]
        if not 1 <= index <= self.max_users:
            raise im_error(ImStatus.INVALID_COMMAND)
        response: dict[str, Any] = {
            "userIndex": index,
            "userName": None,
            "userUniqueID": None,
            "userStatus": None,
            "userType": None,
            "credentialRule": None,
            "credentials": None,
            "creatorFabricIndex": None,
            "lastModifiedFabricIndex": None,
            "nextUserIndex": next((i for i in sorted(self.users) if i > index), None),
        }
        if (user := self.users.get(index)) is not None:
            response |= {
                "userName": user.name,
                "userUniqueID": user.unique_id,
                "userStatus": user.status,
                "userType": user.user_type,
                "credentialRule": user.credential_rule,
                "credentials": [
                    {"credentialType": kind, "credentialIndex": i}
                    for kind, i in user.credentials
                ],
                "creatorFabricIndex": user.created_by,
                "lastModifiedFabricIndex": user.modified_by,
            }
        return response

    async def _cmd_SetUser(self, payload: dict[str, Any]) -> None:  # noqa: N802
        """
        Answer SetUser (DLS:411-508), creating (DLS:1948-2019) or modifying (DLS:2021-2091).

        Checked first: the index is in range, a name fits ten characters, and
        the status is one a Set may write (DLS:448-483) -- else InvalidCommand.
        Adding to an occupied index is a Failure carrying the cluster-specific
        Occupied (DLS:1964-1967). Modifying an empty
        index is InvalidCommand (DLS:2041-2045), as is renaming a record
        another fabric created (DLS:2047-2061); fields left null keep their
        values. Either change is logged as a ``LockUserChange`` (DLS:2015, 2087).
        """
        self._require_users()
        index: int = payload["userIndex"]
        name: str | None = payload.get("userName")
        status = payload.get("userStatus")
        if (
            not 1 <= index <= self.max_users
            or (name is not None and len(name) > self.max_user_name)
            or status
            not in (None, _ENABLED, DoorLock.Enums.UserStatusEnum.kOccupiedDisabled)
        ):
            raise im_error(ImStatus.INVALID_COMMAND)
        operation = payload["operationType"]
        existing = self.users.get(index)
        if operation == DoorLock.Enums.DataOperationTypeEnum.kAdd:
            if existing is not None:
                raise im_error(ImStatus.FAILURE, DlStatus.kOccupied)
            self.users[index] = LockUser(
                name=name or "",
                unique_id=payload.get("userUniqueID"),
                status=_ENABLED if status is None else status,
                user_type=_or(
                    payload.get("userType"),
                    DoorLock.Enums.UserTypeEnum.kUnrestrictedUser,
                ),
                credential_rule=_or(
                    payload.get("credentialRule"),
                    DoorLock.Enums.CredentialRuleEnum.kSingle,
                ),
            )
        elif operation == DoorLock.Enums.DataOperationTypeEnum.kModify:
            if existing is None:
                raise im_error(ImStatus.INVALID_COMMAND)
            if existing.created_by != HA_FABRIC and (
                name is not None or payload.get("userUniqueID") is not None
            ):
                raise im_error(ImStatus.INVALID_COMMAND)
            existing.name = _or(name, existing.name)
            existing.unique_id = _or(payload.get("userUniqueID"), existing.unique_id)
            existing.status = _or(status, existing.status)
            existing.user_type = _or(payload.get("userType"), existing.user_type)
            existing.credential_rule = _or(
                payload.get("credentialRule"), existing.credential_rule
            )
            existing.modified_by = HA_FABRIC
        else:
            raise im_error(ImStatus.INVALID_COMMAND)
        self._log_user_change(
            DoorLock.Enums.LockDataTypeEnum.kUserIndex, operation, index, index
        )

    async def _cmd_ClearUser(self, payload: dict[str, Any]) -> None:  # noqa: N802
        """
        Answer ClearUser (DLS:592-662, 2093-2151): the user, its credentials and schedules.

        ``CLEAR_ALL`` clears every user and logs one event for all of them.
        """
        self._require_users()
        index: int = payload["userIndex"]
        if index == CLEAR_ALL:
            for user_index in list(self.users):
                self._clear_user(user_index, announce=False)
            self._log_user_change(
                DoorLock.Enums.LockDataTypeEnum.kUserIndex,
                DoorLock.Enums.DataOperationTypeEnum.kClear,
                CLEAR_ALL,
                CLEAR_ALL,
            )
            return
        if not 1 <= index <= self.max_users:
            raise im_error(ImStatus.INVALID_COMMAND)
        self._clear_user(index, announce=True)

    def _clear_user(self, index: int, *, announce: bool) -> None:
        """
        Remove a user and every credential it holds (DLS:2105-2151).

        The SDK does not check that the index was occupied, so clearing an
        empty one is announced like any other.
        """
        user = self.users.pop(index, None)
        for credential in user.credentials if user else ():
            self.credentials.pop(credential, None)
        if announce:
            self._log_user_change(
                DoorLock.Enums.LockDataTypeEnum.kUserIndex,
                DoorLock.Enums.DataOperationTypeEnum.kClear,
                index,
                index,
            )

    # Credentials (DLS:664-1044) ---------------------------------------------

    async def _cmd_SetCredential(self, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        """
        Answer SetCredential (DLS:664-856) in band, with a ``DlStatus``.

        In order: the type must be supported (DLS:678-685); the index in
        range, else InvalidField (DLS:692-700); the data within the PIN
        lengths, else InvalidField (DLS:703-709); no other slot of the type
        may hold the same data, else Duplicate (DLS:712-740). Adding over an
        occupied index is Occupied (DLS:2489-2494). With no user index a user
        is created at the first free index (DLS:2200-2247); with one, the
        credential joins that user, which must exist and be this fabric's,
        else InvalidField (DLS:2249-2316), and stay within the per-user limit,
        else ResourceExhausted (DLS:2346-2350). Modifying needs an occupied
        index this fabric created (DLS:800-818). Success is logged as a
        ``LockUserChange`` (DLS:2573, 2659).
        """
        kind = payload["credential"]["credentialType"]
        index = payload["credential"]["credentialIndex"]
        data = _as_bytes(payload["credentialData"])
        user_index = payload.get("userIndex")
        next_free = self._next_free_credential(index)
        if not self._supports_type(kind):
            raise im_error(ImStatus.UNSUPPORTED_COMMAND)
        if not 1 <= index <= self.pin_slots:
            return _set_credential_response(DlStatus.kInvalidField, None, next_free)
        low, high = self.pin_lengths
        if not low <= len(data) <= high:
            return _set_credential_response(DlStatus.kInvalidField, None, next_free)
        if any(
            other_kind == kind and other_index != index and credential.data == data
            for (other_kind, other_index), credential in self.credentials.items()
        ):
            return _set_credential_response(DlStatus.kDuplicate, None, next_free)
        existing = self.credentials.get((kind, index))
        operation = payload["operationType"]
        if operation == DoorLock.Enums.DataOperationTypeEnum.kAdd:
            if existing is not None:
                return _set_credential_response(DlStatus.kOccupied, None, next_free)
            created_user: int | None = None
            if user_index is None:
                created_user = next(
                    (i for i in range(1, self.max_users + 1) if i not in self.users),
                    None,
                )
                if created_user is None:
                    return _set_credential_response(DlStatus.kOccupied, None, next_free)
                self.users[created_user] = LockUser(
                    status=_or(payload.get("userStatus"), _ENABLED),
                    user_type=_or(
                        payload.get("userType"),
                        DoorLock.Enums.UserTypeEnum.kUnrestrictedUser,
                    ),
                    credentials=[(kind, index)],
                )
                self._log_user_change(
                    DoorLock.Enums.LockDataTypeEnum.kUserIndex,
                    DoorLock.Enums.DataOperationTypeEnum.kAdd,
                    created_user,
                    created_user,
                )
            else:
                user = self.users.get(user_index)
                if (
                    payload.get("userStatus") is not None
                    or payload.get("userType") is not None
                    or user is None
                    or user.created_by != HA_FABRIC
                ):
                    return _set_credential_response(
                        DlStatus.kInvalidField, None, next_free
                    )
                if len(user.credentials) + 1 > self.per_user:
                    return _set_credential_response(
                        DlStatus.kResourceExhausted, None, next_free
                    )
                user.credentials.append((kind, index))
                user.modified_by = HA_FABRIC
            self.credentials[(kind, index)] = LockCredential(data)
            self._log_user_change(
                DoorLock.Enums.LockDataTypeEnum.kPin,
                DoorLock.Enums.DataOperationTypeEnum.kAdd,
                created_user or user_index or 0,
                index,
            )
            return _set_credential_response(
                DlStatus.kSuccess, created_user, self._next_free_credential(index)
            )
        if operation == DoorLock.Enums.DataOperationTypeEnum.kModify:
            if (
                existing is None
                or existing.created_by != HA_FABRIC
                or user_index is None
                or (kind, index)
                not in self.users.get(user_index, LockUser()).credentials
            ):
                return _set_credential_response(DlStatus.kInvalidField, None, next_free)
            existing.data = data
            existing.modified_by = HA_FABRIC
            self._log_user_change(
                DoorLock.Enums.LockDataTypeEnum.kPin,
                DoorLock.Enums.DataOperationTypeEnum.kModify,
                user_index,
                index,
            )
            return _set_credential_response(DlStatus.kSuccess, None, next_free)
        return _set_credential_response(DlStatus.kInvalidField, None, next_free)

    async def _cmd_GetCredentialStatus(self, payload: dict[str, Any]) -> dict[str, Any]:  # noqa: N802
        """
        Answer GetCredentialStatus (DLS:858-944): whether the index is in use, by whom.

        An index out of range answers that nothing is there (DLS:902-907);
        the next occupied index after it is named either way (DLS:880-895).
        """
        kind = payload["credential"]["credentialType"]
        index = payload["credential"]["credentialIndex"]
        if not self._supports_type(kind):
            raise im_error(ImStatus.UNSUPPORTED_COMMAND)
        credential = self.credentials.get((kind, index))
        return {
            "credentialExists": credential is not None,
            "userIndex": self.owner(index) if credential is not None else None,
            "creatorFabricIndex": credential.created_by if credential else None,
            "lastModifiedFabricIndex": credential.modified_by if credential else None,
            "nextCredentialIndex": next(
                (
                    i
                    for (other_kind, i) in sorted(self.credentials)
                    if other_kind == kind and i > index
                ),
                None,
            ),
            "credentialData": None,
        }

    async def _cmd_ClearCredential(self, payload: dict[str, Any]) -> None:  # noqa: N802
        """
        Answer ClearCredential (DLS:993-1044, 2986-3140).

        An unsupported type or an index out of range is InvalidCommand
        (DLS:1023-1030, 2996-3002); clearing every credential (null, or
        ``CLEAR_ALL``) is not modeled. An empty index clears nothing and
        succeeds (DLS:3005-3021). Clearing a
        user's only credential clears the user, and is logged as the user's
        removal (DLS:3043-3053); otherwise the credential leaves the user, and
        is logged as itself (DLS:3135).
        """
        credential = payload.get("credential")
        if credential is None or credential["credentialIndex"] == CLEAR_ALL:
            raise NotImplementedError(
                "the fake lock does not model clearing every credential"
            )
        kind = credential["credentialType"]
        index = credential["credentialIndex"]
        if not self._supports_type(kind) or not 1 <= index <= self.pin_slots:
            raise im_error(ImStatus.INVALID_COMMAND)
        if (kind, index) not in self.credentials:
            return
        owner = self.owner(index)
        if owner is not None and len(self.users[owner].credentials) == 1:
            self._clear_user(owner, announce=True)
            return
        self.credentials.pop((kind, index))
        if owner is not None:
            self.users[owner].credentials.remove((kind, index))
            self.users[owner].modified_by = HA_FABRIC
        self._log_user_change(
            DoorLock.Enums.LockDataTypeEnum.kPin,
            DoorLock.Enums.DataOperationTypeEnum.kClear,
            owner or 0,
            index,
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _log_user_change(
        self,
        data_type: int,
        operation: int,
        user_index: int,
        data_index: int,
        *,
        source: int = DoorLock.Enums.OperationSourceEnum.kRemote,
    ) -> None:
        """
        Log a ``LockUserChange`` the way the SDK fills one in (DLS:3364-3380).

        A remote change names Home Assistant's fabric and node; a zero index is
        sent as null. A local one (keypad, another admin's app) names neither.
        """
        remote = source == DoorLock.Enums.OperationSourceEnum.kRemote
        self._log_event(
            DoorLock.Events.LockUserChange,
            {
                "lockDataType": data_type,
                "dataOperationType": operation,
                "operationSource": source,
                "userIndex": user_index or None,
                "fabricIndex": HA_FABRIC if remote else None,
                "sourceNode": _HA_NODE_ID if remote else None,
                "dataIndex": data_index or None,
            },
        )

    def _log_event(self, event: type, data: dict[str, Any]) -> None:
        """
        Deliver a Door Lock event through the client, as the server forwards it.

        What the client does with a ``node_event`` message (MS_CLIENT:932-944):
        parse it into a ``MatterNodeEvent`` and signal it to the subscribers
        its own filters match (MS_CLIENT:960-979).
        """
        self._event_number += 1
        # A separate message from the server, after the command's own answer:
        # held while a command is answered, and scheduled either way, so a
        # caller has its response before any subscriber runs.
        node_event = dataclass_from_dict(
            MatterNodeEvent,
            {
                "node_id": self.node.node_id,
                "endpoint_id": self.endpoint_id,
                "cluster_id": _DL,
                "event_id": event.event_id,
                "event_number": self._event_number,
                "priority": _EVENT_PRIORITY[event.event_id],
                # A lock that keeps time stamps the event in epoch
                # milliseconds, which the server forwards as type 1
                # (MJS_WS:413-426).
                "timestamp": int(time.time() * 1000),
                "timestamp_type": 1,
                "data": data,
            },
        )
        self._queued_events.append(node_event)
        if not self._answering:
            asyncio.get_running_loop().call_soon(self._flush_events)

    def _flush_events(self) -> None:
        """Signal every queued event to the subscribers the client's filters match."""
        if self._answering:
            return
        events, self._queued_events = self._queued_events, []
        for node_event in events:
            MatterClient._signal_event(
                self.client,
                EventType.NODE_EVENT,
                data=node_event,
                node_id=node_event.node_id,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _supports_type(self, kind: int) -> bool:
        """Return whether the lock takes credentials of ``kind`` (DLS:2728-2750)."""
        return kind == _PIN and bool(
            self.feature_map & DoorLock.Bitmaps.Feature.kPinCredential
        )

    def _require_users(self) -> None:
        """Refuse a user command, UnsupportedCommand, on a lock without the USR feature."""
        if not self.supports_users:
            raise im_error(ImStatus.UNSUPPORTED_COMMAND)

    def _next_free_credential(self, after: int) -> int | None:
        """Return the next PIN index after ``after`` that is free (DLS:694-696)."""
        return next(
            (
                i
                for i in range(after + 1, self.pin_slots + 1)
                if (_PIN, i) not in self.credentials
            ),
            None,
        )


def _addressed_index(payload: dict[str, Any]) -> int | None:
    """Return the user or credential index a command addresses."""
    if (credential := payload.get("credential")) is not None:
        return credential["credentialIndex"]
    return payload.get("userIndex")


def _cred_index(payload: dict[str, Any]) -> int:
    return payload["credential"]["credentialIndex"]


def _set_credential_response(
    status: int, user_index: int | None, next_index: int | None
) -> dict[str, Any]:
    """Return a ``SetCredentialResponse`` (DLS:2711-2726)."""
    return {
        "status": status,
        "userIndex": user_index,
        "nextCredentialIndex": next_index,
    }


def _as_bytes(data: Any) -> bytes:
    """Return credential data as bytes; ``dataclass_to_dict`` leaves them bytes."""
    return data if isinstance(data, bytes) else str(data).encode()


def _or(value: Any, default: Any) -> Any:
    """Return ``value``, or ``default`` for a field left null."""
    return default if value is None else value
