"""
A stateful Z-Wave JS lock node, faked where Home Assistant's client meets the server.

The fake answers the server commands Home Assistant's ``lock_helpers`` and
the provider send for one node, and raises the node events the server
forwards, so zwave-js-server-python, Home Assistant's zwave_js integration and
the provider all run for real above it. It keeps two things apart, as the
driver does: what the lock holds (``FakeZWaveLock.holds``) and what the
driver's value database caches about it. Cached reads answer from the cache;
only an exchange with the lock (a write, a fresh read, a report the lock
sends) changes it, and only the way the driver's code changes it.

Every modeled behavior is traced through the layers and cited below by file
and line. References, read at:

- node-zwave-js ``a0cc3b1c0`` (15.31.1+26), packages:
  ``AC`` = zwave-js/src/lib/node/feature-apis/AccessControl.ts,
  ``UCC`` = cc/src/cc/UserCodeCC.ts, ``U3CC`` = cc/src/cc/UserCredentialCC.ts,
  ``U3CH`` = zwave-js/src/lib/node/CCHandlers/UserCredentialCC.ts,
  ``NCH`` = zwave-js/src/lib/node/CCHandlers/NotificationCC.ts,
  ``DRV`` = zwave-js/src/lib/driver/Driver.ts,
  ``SM`` = zwave-js/src/lib/driver/StateMachineShared.ts,
  ``STAT`` = zwave-js/src/lib/driver/Statistics.ts,
  ``VDB`` = core/src/values/ValueDB.ts,
  ``VAL`` = zwave-js/src/lib/node/mixins/40_Values.ts,
  ``SUP`` = core/src/definitions/Supervision.ts,
  ``ERR`` = core/src/error/ZWaveError.ts,
  ``MOCK_UC`` / ``MOCK_U3C`` = zwave-js/src/lib/node/mockCCBehaviors/
  UserCode.ts / UserCredential.ts (the driver's own device models, for what
  a lock answers).
- zwave-js-server ``400fd94`` (3.11.0): ``SRV_AC`` =
  src/lib/endpoint/access_control/message_handler.ts, ``SRV_FWD`` =
  src/lib/forward.ts, ``SRV`` = src/lib/server.ts, ``SRV_JSON`` =
  src/util/stringify.ts.
- zwave-js-server-python 0.73.1 (the version Home Assistant 2026.9.4 pins):
  ``PY_AC`` = zwave_js_server/model/access_control.py, ``PY_CLIENT`` =
  zwave_js_server/client.py, ``PY_NODE`` = zwave_js_server/model/node/__init__.py.
- Home Assistant 2026.9.4: ``HA_LH`` = homeassistant/components/zwave_js/
  lock_helpers.py.

The driver is 15.x: User Code values are public, so a User Code report
raises value events and no unified credential event (UCC:245-270, VAL:186).
zwave-js/zwave-js#8930 (v16) changes that; the provider's report shim and
this fake both follow the released driver.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
import copy
from dataclasses import dataclass, field, replace
from enum import IntEnum, StrEnum
import time
from typing import Any, Literal, Self
from unittest.mock import MagicMock

from awesomeversion import AwesomeVersion, AwesomeVersionCompareException
import pytest
from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.access_control import (
    SetCredentialResult,
    SetUserResult,
    UserCredentialRule,
    UserCredentialType,
    UserCredentialUserType,
)
from zwave_js_server.const.command_class.lock import (
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.exceptions import FailedZWaveCommand
from zwave_js_server.model.node import Node

from tests.common import async_run_until_quiet, track_fault, untrack_fault
from tests.providers.zwave_access_control import DRIVER_EVENT_LABELS


class UserIDStatus(IntEnum):
    """User Code CC ``userIdStatus`` values (UCC ``UserIDStatus``)."""

    AVAILABLE = 0x00
    ENABLED = 0x01
    DISABLED = 0x02
    MESSAGING = 0x03
    PASSAGE_MODE = 0x04
    STATUS_NOT_AVAILABLE = 0xFE


class SupervisionStatus(IntEnum):
    """Supervision report statuses (SUP:5-10)."""

    NO_SUPPORT = 0x00
    WORKING = 0x01
    FAIL = 0x02
    SUCCESS = 0xFF


class ZWaveErrorCode(IntEnum):
    """The ``ZWaveErrorCodes`` the fake raises (ERR:34-39, 109)."""

    CONTROLLER_NODE_TIMEOUT = 201
    CONTROLLER_CALLBACK_NOK = 204
    ARGUMENT_INVALID = 322


class CredentialReportType(IntEnum):
    """User Credential CC Credential Report types the fake sends (U3CC enum)."""

    CREDENTIAL_ADDED = 0x00
    CREDENTIAL_MODIFIED = 0x01
    CREDENTIAL_DELETED = 0x02
    CREDENTIAL_UNCHANGED = 0x03
    RESPONSE_TO_GET = 0x04
    CREDENTIAL_ADD_REJECTED_LOCATION_OCCUPIED = 0x05
    CREDENTIAL_MODIFY_REJECTED_LOCATION_EMPTY = 0x06
    DUPLICATE_CREDENTIAL = 0x07
    MANUFACTURER_SECURITY_RULES = 0x08
    WRONG_USER_UNIQUE_IDENTIFIER = 0x09
    DUPLICATE_ADMIN_PIN_CODE = 0x0A


class UserReportType(IntEnum):
    """User Credential CC User Report types the fake sends (U3CC enum)."""

    USER_ADDED = 0x00
    USER_MODIFIED = 0x01
    USER_DELETED = 0x02
    USER_UNCHANGED = 0x03
    RESPONSE_TO_GET = 0x04
    USER_ADD_REJECTED_LOCATION_OCCUPIED = 0x05
    USER_MODIFY_REJECTED_LOCATION_EMPTY = 0x06


# AC:158-183 and AC:139-156: the driver's mapping of a device report to the
# result the access-control API returns. A report that never arrives maps to
# Error_Unknown.
_CREDENTIAL_RESULT: dict[CredentialReportType, SetCredentialResult] = {
    CredentialReportType.CREDENTIAL_ADDED: SetCredentialResult.OK,
    CredentialReportType.CREDENTIAL_MODIFIED: SetCredentialResult.OK,
    CredentialReportType.CREDENTIAL_DELETED: SetCredentialResult.OK,
    CredentialReportType.CREDENTIAL_UNCHANGED: SetCredentialResult.OK,
    CredentialReportType.CREDENTIAL_ADD_REJECTED_LOCATION_OCCUPIED: (
        SetCredentialResult.ERROR_ADD_REJECTED_LOCATION_OCCUPIED
    ),
    CredentialReportType.CREDENTIAL_MODIFY_REJECTED_LOCATION_EMPTY: (
        SetCredentialResult.ERROR_MODIFY_REJECTED_LOCATION_EMPTY
    ),
    CredentialReportType.DUPLICATE_CREDENTIAL: (
        SetCredentialResult.ERROR_DUPLICATE_CREDENTIAL
    ),
    CredentialReportType.MANUFACTURER_SECURITY_RULES: (
        SetCredentialResult.ERROR_MANUFACTURER_SECURITY_RULES
    ),
    CredentialReportType.WRONG_USER_UNIQUE_IDENTIFIER: (
        SetCredentialResult.ERROR_WRONG_USER_UNIQUE_IDENTIFIER
    ),
    CredentialReportType.DUPLICATE_ADMIN_PIN_CODE: (
        SetCredentialResult.ERROR_DUPLICATE_ADMIN_PIN_CODE
    ),
}
_USER_RESULT: dict[UserReportType, SetUserResult] = {
    UserReportType.USER_ADDED: SetUserResult.OK,
    UserReportType.USER_MODIFIED: SetUserResult.OK,
    UserReportType.USER_DELETED: SetUserResult.OK,
    UserReportType.USER_UNCHANGED: SetUserResult.OK,
    UserReportType.USER_ADD_REJECTED_LOCATION_OCCUPIED: (
        SetUserResult.ERROR_ADD_REJECTED_LOCATION_OCCUPIED
    ),
    UserReportType.USER_MODIFY_REJECTED_LOCATION_EMPTY: (
        SetUserResult.ERROR_MODIFY_REJECTED_LOCATION_EMPTY
    ),
}

USER_CREDENTIAL_CC_ID = 131
SUPERVISION_CC_ID = 108
_UC = CommandClass.USER_CODE.value
_U3C = USER_CREDENTIAL_CC_ID
_PIN = UserCredentialType.PIN_CODE

# A driver value ID, as (command class, property, property key).
ValueKey = tuple[int, str, int | None]

# The only values the fake keeps that the driver exposes to applications:
# every other User Code value and every User Credential value is
# ``internal`` (UCC ``UserCodeCCValues``, U3CC:91-300), and the driver raises
# no value event for an internal value (VAL:186).
_PUBLIC_PROPERTIES = frozenset({LOCK_USERCODE_STATUS_PROPERTY, LOCK_USERCODE_PROPERTY})

CodeDisplay = Literal["clear", "masked", "withheld"]
NodeOperation = Literal["set", "clear", "get", "user"]


def user_credential_lock_state(state: dict[str, Any]) -> dict[str, Any]:
    """
    Turn a User Code lock's node dump into a User Credential lock's.

    No node dump of a User Credential CC lock exists in Home Assistant core's
    zwave_js fixtures, zwave-js-server-python's tests, zwave-js-server or
    node-zwave-js (searched for command class 131 on 2026-10-12), so this
    derives one from the Schlage BE469 dump: the same node, its root endpoint
    advertising User Credential CC in place of User Code CC. Its User Code
    values go with it, and it gains none: every User Credential value is
    internal, and a server node dump carries no internal value.
    """
    state = copy.deepcopy(state)
    state["label"] = "User Credential lock"
    for endpoint in state["endpoints"]:
        endpoint["commandClasses"] = [
            cc for cc in endpoint["commandClasses"] if cc["id"] != _UC
        ]
        if endpoint["index"] == 0:
            endpoint["commandClasses"].append(
                {
                    "id": _U3C,
                    "name": "User Credential",
                    "version": 1,
                    "isSecure": True,
                }
            )
    state["values"] = [v for v in state["values"] if v["commandClass"] != _UC]
    return state


def supervised_lock_state(state: dict[str, Any]) -> dict[str, Any]:
    """
    Make a node dump advertise Supervision CC on its root endpoint.

    The driver supervises a Set only for a node that supports Supervision CC
    (DRV:8044-8051, ``SupervisionCC.mayUseSupervision``); the BE469 does not.
    """
    state = copy.deepcopy(state)
    root = next(e for e in state["endpoints"] if e["index"] == 0)
    root["commandClasses"].append(
        {
            "id": SUPERVISION_CC_ID,
            "name": "Supervision",
            "version": 1,
            "isSecure": False,
        }
    )
    return state


class FaultKind(StrEnum):
    """How an exchange with the lock goes wrong; see the knobs on the fake."""

    IGNORE = "ignore"
    REFUSE = "refuse"
    FAIL = "fail"
    HANG = "hang"
    DROP_REPLY = "drop_reply"
    SLOW = "slow"


# The User Credential values the fake caches per user and per credential.
_U3C_ENTRY_PROPERTIES = frozenset(
    {
        "userType",
        "userActive",
        "credentialRule",
        "userName",
        "credential",
        "credentialOwner",
    }
)

# Only a Set that stores or empties a slot can be ignored or refused.
_WRITE_ONLY_FAULTS = frozenset({FaultKind.IGNORE, FaultKind.REFUSE})


@dataclass(eq=False)
class NodeFault:
    """
    One way the lock misbehaves on its radio exchanges, until cleared.

    Named and shaped like ``tests.common.LockFault`` (same knob names,
    ``times``, ``hits``, ``held``, ``async_wait_held``, context manager), but
    acting one layer down: on an exchange between the driver and the lock,
    not on a provider call. ``operation`` is the exchange: ``set`` (a Set that
    stores a code), ``clear`` (a Set that empties a slot), ``get`` (any Get of
    a slot: a fresh read, a walk, the driver's verification read) or ``user``
    (a User Credential CC User Set); None matches all of them. ``slot`` is the
    slot (or user, for ``user``) the exchange addresses; None matches any.
    """

    lock: FakeZWaveLock
    kind: FaultKind
    operation: NodeOperation | None
    slot: int | None
    times: int | None = None
    seconds: float = 0.0
    error_code: int = ZWaveErrorCode.CONTROLLER_CALLBACK_NOK
    error_message: str = "Failed to send the command (Status NoAck)"
    hits: int = 0
    held: int = 0
    _released: asyncio.Event = field(default_factory=asyncio.Event)

    def __enter__(self) -> Self:
        """Return the fault, to clear on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Clear the fault."""
        self.clear()

    def matches(self, operation: NodeOperation, slot: int) -> bool:
        """Return whether an exchange of ``operation`` on ``slot`` meets this."""
        return (self.operation is None or self.operation == operation) and (
            self.slot is None or self.slot == slot
        )

    def clear(self) -> None:
        """Stop matching exchanges, and let any exchange held go."""
        if self in self.lock.faults:
            self.lock.faults.remove(self)
        untrack_fault(self)
        self._released.set()

    async def async_wait_held(self) -> None:
        """Run the loop until quiet, then fail unless this holds an exchange now."""
        await async_run_until_quiet()
        if not self.held:
            pytest.fail(
                f"no {self.operation or 'exchange'} held by the {self.kind} fault "
                f"(matched {self.hits})"
            )

    async def async_before(self) -> None:
        """Act out what happens before the lock sees the frame."""
        if self.kind is FaultKind.FAIL:
            # SM:101-112: a frame the lock never acknowledges fails the
            # transaction with Controller_CallbackNOK; DRV:6667-6669 counts it.
            self.lock.statistics["commandsDroppedTX"] += 1
            raise zwave_error(self.error_code, self.error_message)
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


def zwave_error(code: int, message: str) -> FailedZWaveCommand:
    """
    Return the exception the python client raises for a driver ``ZWaveError``.

    The driver appends ``(ZW0322)`` to every message (ERR:220-245); the server
    forwards code and message as ``zwaveErrorCode``/``zwaveErrorMessage``
    (SRV:283-308); the client raises ``FailedZWaveCommand`` (PY_CLIENT:496-501).
    """
    suffix = f" (ZW{code:04d})"
    if not message.endswith(suffix):
        message += suffix
    return FailedZWaveCommand("fake-node", code, message)


@dataclass
class LockUser:
    """A User Credential CC user as the lock stores it (MOCK_U3C ``UserState``)."""

    user_type: int = UserCredentialUserType.GENERAL
    active: bool = True
    credential_rule: int = UserCredentialRule.SINGLE
    user_name: str = ""


@dataclass
class _Exchange:
    """One radio exchange the fake acted out, for tests to inspect."""

    operation: NodeOperation
    slot: int
    code: str | None = None
    # When the lock saw it, on the clock the provider paces its turns by.
    # Looked up per call: the bare function, bound here, escapes freezegun.
    at: float = field(default_factory=lambda: time.monotonic())  # noqa: PLW0108


class FakeZWaveLock:
    """
    A lock node behind the driver's unified access-control API.

    Install it on the ``zwave_client`` fixture's ``async_send_command``; it
    answers that node's ``endpoint.access_control.*`` commands and the User
    Code ``getUsersCount`` CC API call, and passes every other command to the
    fixture's canned answers. Which command class the driver dispatches to is
    decided as the driver decides it (AC:269-288), from the node's command
    classes and the cached User Credential user count.

    User Code CC (the BE469 fixture):

    - ``supported_users`` is how many slots the lock has; ``interviewed``
      says whether the driver cached that count and every slot's report (an
      interview that finished while a battery lock slept leaves it uncached:
      issue #1298). Slots past the lock's capacity answer
      ``StatusNotAvailable`` (MOCK_UC ``respondToUserGet``).
    - ``code_display`` is what the lock puts in a report's code field for an
      occupied slot: the code, an asterisk per digit (``masked``), or nothing
      (``withheld``: zero padding, which the driver strips to "", UCC:1556).
    - ``supervision``: whether Sets are supervised comes from the dump
      (``supervised_lock_state``); on such a node this chooses what the lock
      reports for a Set it applies -- Success (the default), Working, Fail
      (and it applies nothing) or NoSupport (the driver resends unsupervised)
      -- and it is refused on a node without Supervision CC. A Set the lock
      did not apply is reported Fail; ``ignore`` makes it lie.
    - ``rejects_duplicates``: the lock refuses a code another slot holds, and
      announces it with Access Control notification 15 (``duplicate_parameters``
      as its event parameters, if any). User Code CC has no in-band
      rejection, so the driver can only answer ``Error_Unknown`` (AC:1186-1236).

    User Credential CC (``user_credential_lock_state``): ``max_users``,
    ``pin_slots``, ``max_user_name_length`` and ``max_credential_hash_length``
    are the capabilities; a hash length above 0 means the lock sends no
    credential data back (MOCK_U3C:1062-1068, CC:0083.01.0C.11.021).
    Duplicates are rejected in-band, as the specification requires
    (MOCK_U3C:1663-1683).

    ``codes`` is what the lock holds, by slot, when it differs from what the
    dump caches (a masking lock's dump caches asterisks).

    ``driver_version`` is the driver the client reports connecting to
    (``client.version``, PY_CLIENT). One before zwave-js/zwave-js#8927
    (5239ec3c6, released in 15.25.2), still common on add-on installs, is
    ``driver_before_15_25_2``: on User Code CC it took a supervised Set's
    outcome at its word: a success persisted nothing, so the cache kept the
    slot's old values, and a failure was answered Error_Unknown without
    reading the slot. The worker's read-back after a write that returns is what
    puts that cache right. A version that does not parse behaves as the
    current driver.

    Faults (``ignore``, ``refuse``, ``fail``, ``hang``, ``drop_reply``,
    ``slow``) act on radio exchanges; see ``NodeFault``.
    """

    def __init__(
        self,
        client: MagicMock,
        node: Node,
        *,
        supported_users: int = 30,
        interviewed: bool = True,
        code_display: CodeDisplay = "clear",
        supervision: SupervisionStatus | None = None,
        rejects_duplicates: bool = True,
        duplicate_parameters: dict[str, Any] | None = None,
        max_users: int = 30,
        pin_slots: int = 30,
        max_user_name_length: int = 16,
        max_credential_hash_length: int = 0,
        pin_lengths: tuple[int, int] = (4, 10),
        other_credential_types: tuple[int, ...] = (),
        codes: dict[int, str] | None = None,
        driver_version: str = "15.31.1",
    ) -> None:
        """Wrap ``node``, seed the driver cache from its dump, and install."""
        self.client = client
        self.node = node
        self.code_display: CodeDisplay = code_display
        # Remembered by the driver once a Set came back NoSupport (DRV:8058-8063).
        self._uc_supervision_unsupported = False
        self._supervision: SupervisionStatus | None = None
        if supervision is not None:
            self.supervision = supervision
        elif self._supports(SUPERVISION_CC_ID):
            self.supervision = SupervisionStatus.SUCCESS
        self.rejects_duplicates = rejects_duplicates
        self.driver_version = driver_version
        self.duplicate_parameters = duplicate_parameters
        self.max_user_name_length = max_user_name_length
        self.max_credential_hash_length = max_credential_hash_length
        self.pin_lengths = pin_lengths
        self.other_credential_types = other_credential_types
        # Every fault not yet cleared; a spent one matches nothing more but
        # may still hold the exchange it met last.
        self.faults: list[NodeFault] = []
        # Every server command the fake answered, and every radio exchange.
        self.commands: list[dict[str, Any]] = []
        self.exchanges: list[_Exchange] = []
        self.statistics: dict[str, int] = {
            "commandsTX": 0,
            "commandsRX": 0,
            "commandsDroppedRX": 0,
            "commandsDroppedTX": 0,
            "timeoutResponse": 0,
        }
        # The driver's value database for the two access-control command
        # classes, and which public value IDs already have metadata.
        self._values: dict[ValueKey, Any] = {}
        self._metadata: set[ValueKey] = set()
        # What the lock holds.
        self.capacity = supported_users
        self.uc_slots: dict[int, tuple[UserIDStatus, str]] = {}
        self.max_users = max_users
        self.pin_slots = pin_slots
        self.users: dict[int, LockUser] = {}
        self.credentials: dict[tuple[int, int], tuple[int, str]] = {}

        self._seed_user_code(interviewed, codes)
        self._seed_user_credential()
        self._previous = client.async_send_command.side_effect
        client.async_send_command.side_effect = self._async_dispatch

    # ------------------------------------------------------------------
    # The lock, seen from the test
    # ------------------------------------------------------------------

    @property
    def driver_version(self) -> str:
        """Return the driver version the client reports."""
        return self.client.version.driver_version

    @driver_version.setter
    def driver_version(self, version: str) -> None:
        """Put the lock behind another driver, as reconnecting to it would."""
        self.client.version = replace(self.client.version, driver_version=version)

    @property
    def driver_before_15_25_2(self) -> bool:
        """Return whether the driver predates zwave-js/zwave-js#8927."""
        try:
            return AwesomeVersion(self.driver_version) < AwesomeVersion("15.25.2")
        except AwesomeVersionCompareException:
            return False

    @property
    def supervision(self) -> SupervisionStatus | None:
        """Return the Supervision status the lock reports for a Set, if supervised."""
        return self._supervision

    @supervision.setter
    def supervision(self, status: SupervisionStatus) -> None:
        """
        Choose the outcome a supervised Set reports.

        Whether a Set is supervised at all is the dump's to say: the driver
        supervises only for a node advertising Supervision CC
        (``SupervisionCC.mayUseSupervision``), so an outcome on a node that
        does not is a test that cannot happen, and is refused -- as is None,
        which is no outcome.
        """
        if status is None:
            raise ValueError(
                "None is no Supervision outcome; whether Sets are supervised "
                "is decided by the dump"
            )
        if not self._supports(SUPERVISION_CC_ID):
            raise ValueError(
                "the node does not advertise Supervision CC, so its Sets are "
                "never supervised; use supervised_lock_state for the dump"
            )
        self._supervision = status

    def _supports(self, cc_id: int) -> bool:
        return any(cc.id == cc_id for cc in self.node.command_classes)

    @property
    def uses_user_credential_cc(self) -> bool:
        """
        Return whether the driver dispatches to User Credential CC (AC:269-288).

        Only when the node supports it, and, if it also supports User Code
        CC, does not report 0 User Credential users (an unknown count counts
        as not 0).
        """
        if not self._supports(_U3C):
            return False
        if not self._supports(_UC):
            return True
        return self._get(_U3C, "supportedUsers") != 0

    @property
    def uc_version(self) -> int:
        """Return the node's User Code CC version, as the interview found it."""
        return next(cc.version for cc in self.node.command_classes if cc.id == _UC)

    def holds(self, slot: int) -> str | None:
        """Return the PIN the lock holds at ``slot``, or None if it holds none."""
        if self.uses_user_credential_cc:
            entry = self.credentials.get((_PIN, slot))
            return None if entry is None else entry[1]
        status, code = self.uc_slots.get(slot, (UserIDStatus.AVAILABLE, ""))
        return None if status == UserIDStatus.AVAILABLE else code

    @property
    def codes(self) -> dict[int, str]:
        """Return every PIN the lock holds, by slot."""
        if self.uses_user_credential_cc:
            return {
                slot: data
                for (kind, slot), (_owner, data) in self.credentials.items()
                if kind == _PIN
            }
        return {
            slot: code
            for slot, (status, code) in self.uc_slots.items()
            if status != UserIDStatus.AVAILABLE
        }

    @property
    def sets(self) -> list[tuple[int, str]]:
        """Return each code-storing Set the lock received, as (slot, code)."""
        return [
            (exchange.slot, exchange.code or "")
            for exchange in self.exchanges
            if exchange.operation == "set"
        ]

    @property
    def clears(self) -> list[int]:
        """Return each slot-emptying Set the lock received."""
        return [e.slot for e in self.exchanges if e.operation == "clear"]

    def cached(self, cc_id: int, prop: str, key: int | None = None) -> Any:
        """Return a value from the driver's value database, or None."""
        return self._get(cc_id, prop, key)

    def program(self, slot: int, code: str | None) -> None:
        """
        Change a slot at the keypad (or from another controller), and report it.

        User Code CC: the lock sends an unsolicited User Code Report, which
        the driver persists like any report -- value events, no unified
        credential event on a 15.x driver (DRV:4279-4281, UCC:1586-1597).
        User Credential CC: the lock sends a Credential Report of type Added,
        Modified or Deleted, which the driver persists and turns into the
        matching credential event (U3CC:2874-3040, U3CH:56-108).
        """
        if self.uses_user_credential_cc:
            existing = self.credentials.get((_PIN, slot))
            if code is None:
                self.credentials.pop((_PIN, slot), None)
                owner = 0 if existing is None else existing[0]
                self._u3c_credential_report(
                    CredentialReportType.CREDENTIAL_DELETED, owner, slot, None
                )
                return
            owner = slot if existing is None else existing[0]
            if owner not in self.users:
                self.users[owner] = LockUser(user_name=f"User-{owner}")
                self._u3c_persist_user(owner, UserReportType.USER_ADDED)
                self._u3c_user_event(owner, UserReportType.USER_ADDED)
            self.credentials[(_PIN, slot)] = (owner, code)
            self._u3c_credential_report(
                CredentialReportType.CREDENTIAL_MODIFIED
                if existing
                else CredentialReportType.CREDENTIAL_ADDED,
                owner,
                slot,
                code,
            )
            return
        if code is None:
            self.uc_slots.pop(slot, None)
        else:
            self.uc_slots[slot] = (UserIDStatus.ENABLED, code)
        self.report(slot)

    def enroll(self, user_id: int, kind: int, slot: int, data: str) -> None:
        """
        Add a credential of ``kind`` to an existing user at the lock, and report it.

        An RFID tag or a fingerprint enrolled at the lock or in the vendor's
        app: the lock sends a Credential Report of type Added, which the
        driver persists and turns into a credential event (U3CC:2874-3040).
        """
        assert self.uses_user_credential_cc
        assert user_id in self.users
        assert kind in self.other_credential_types
        self.credentials[(kind, slot)] = (user_id, data)
        self._u3c_credential_report(
            CredentialReportType.CREDENTIAL_ADDED, user_id, slot, data, kind
        )

    def report(self, slot: int) -> None:
        """Send the lock's own User Code Report of ``slot``, unsolicited."""
        self._uc_persist_report(slot, *self._uc_shown(slot))

    def send_user_code_report(self, slot: int, status: int, code: str) -> None:
        """
        Deliver a User Code Report the lock sends, whatever it holds.

        For reports that disagree with the lock's memory -- a stale Available
        some locks send after a code lands (#863), or a code field left over
        after a clear (#819). Persisted the way the driver persists every
        report: status, then code, each raising a value event even when
        unchanged (UCC:245-270, VDB:239-259).
        """
        self._uc_persist_report(slot, UserIDStatus(status), code)

    def notify_access_control(
        self,
        event: AccessControlNotificationEvent,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """
        Deliver an Access Control (type 6) notification the lock sends.

        The driver emits a ``notification`` node event with the registry's
        labels (NCH:262-321; ``DRIVER_EVENT_LABELS``), forwarded as
        the schema-32+ endpoint event (SRV_FWD:563-574); Home Assistant turns
        it into a ``zwave_js_notification`` bus event. Parameters are omitted
        when the lock sends none, as JSON drops an undefined field.
        """
        args: dict[str, Any] = {
            "type": NotificationType.ACCESS_CONTROL,
            "event": event,
            "label": "Access Control",
            "eventLabel": DRIVER_EVENT_LABELS.get(event, event.name),
        }
        if parameters is not None:
            args["parameters"] = parameters
        self.notify(CommandClass.NOTIFICATION, args)

    def notify(self, command_class: CommandClass, args: dict[str, Any]) -> None:
        """
        Deliver a notification of any command class the lock sends.

        Besides the Notification command class, the driver raises
        ``notification`` for a Battery report of low battery, an Entry Control
        notification, a Multilevel Switch level change and a Powerlevel test
        report, each with its own ``args`` (zwave-js/src/lib/node/CCHandlers/,
        at the node-zwave-js commit above). The server
        forwards them all alike (SRV_FWD:563-574), and Home Assistant builds a
        differently shaped ``zwave_js_notification`` bus event for each.
        """
        self._emit(
            "notification",
            endpointIndex=0,
            ccId=command_class,
            args=args,
        )

    def keypad_unlock(self, user_id: int) -> None:
        """Deliver the notification a lock sends when a code unlocks it."""
        self.notify_access_control(
            AccessControlNotificationEvent.KEYPAD_UNLOCK_OPERATION,
            {"userId": user_id},
        )

    def mark_dead(self) -> None:
        """
        Have the driver mark the node dead, as after an unacknowledged frame.

        DRV:4672-4685 marks a listening node dead when it misses an ACK;
        the server forwards ``dead`` with the old status (SRV_FWD:408-413)
        and the client sets the status (PY_NODE ``handle_dead``).
        """
        self._emit("dead", oldStatus=self.node.status)

    def mark_alive(self) -> None:
        """Have the driver mark the node alive again (DRV:5821-5825)."""
        self._emit("alive", oldStatus=self.node.status)

    def emit_statistics(self, **overrides: Any) -> None:
        """
        Send the driver's ``statistics updated`` event with the current counters.

        The counters move on their own as exchanges happen; ``overrides``
        sets any of them (``rtt`` included) first. The driver throttles the
        event to one per 250 ms (STAT:29-43); the fake sends one per
        answered command instead, and one here.
        """
        self.statistics.update(overrides)
        self._emit("statistics updated", statistics=dict(self.statistics))

    def reinterview(self) -> None:
        """
        Re-interview the access-control command class, caching what the lock says.

        User Code CC: the users count (persisted, internal) and a report of
        every slot (value events). User Credential CC: its capabilities.
        """
        if self.uses_user_credential_cc:
            self._seed_user_credential()
        else:
            self._interview_user_code()

    def _interview_user_code(self) -> None:
        """Cache the users count (internal, UCC:1675) and a report of every slot."""
        self._set(_UC, "supportedUsers", None, self.capacity)
        for slot in range(1, self.capacity + 1):
            self._uc_persist_report(slot, *self._uc_shown(slot))

    # ------------------------------------------------------------------
    # Faults
    # ------------------------------------------------------------------

    def _add_fault(self, kind: FaultKind, operation, slot, **kwargs: Any) -> NodeFault:
        """
        Install a fault, refusing one that cannot happen to its exchange.

        As ``LockFault`` refuses them (``_WRITE_ONLY_FAULTS``): only a Set
        that stores or empties a slot can be ignored or refused, so a fault
        that would match nothing cannot pass a test for the wrong reason.
        """
        if kind in _WRITE_ONLY_FAULTS and operation not in ("set", "clear"):
            raise ValueError(f"a {kind} fault needs a set or clear operation")
        fault = NodeFault(self, kind, operation, slot, **kwargs)
        self.faults.append(fault)
        track_fault(fault)
        return fault

    def ignore(
        self, operation: Literal["set", "clear"], slot: int | None = None, **kw: Any
    ) -> NodeFault:
        """
        Acknowledge a Set without applying it.

        Unsupervised, only the driver's verification read can tell, and it
        sees the old slot. Supervised, the lock reports Success anyway, and
        the driver persists the code it wrote without reading it back
        (AC:1195-1199), so the cache claims what the lock does not hold.
        """
        return self._add_fault(FaultKind.IGNORE, operation, slot, **kw)

    def refuse(
        self, operation: Literal["set", "clear"], slot: int | None = None, **kw: Any
    ) -> NodeFault:
        """Refuse a Set: not applied, and reported Fail when supervised."""
        return self._add_fault(FaultKind.REFUSE, operation, slot, **kw)

    def fail(
        self,
        operation: NodeOperation | None = None,
        slot: int | None = None,
        *,
        error_code: int = ZWaveErrorCode.CONTROLLER_CALLBACK_NOK,
        error_message: str = "Failed to send the command (Status NoAck)",
        **kw: Any,
    ) -> NodeFault:
        """
        Never acknowledge the frame: the command fails with a driver error.

        The default is what an unreachable lock produces (SM:101-112); the
        server sends it as a ``zwave_error`` and the client raises
        ``FailedZWaveCommand`` carrying the code.
        """
        return self._add_fault(
            FaultKind.FAIL,
            operation,
            slot,
            error_code=error_code,
            error_message=error_message,
            **kw,
        )

    def hang(
        self, operation: NodeOperation | None = None, slot: int | None = None, **kw: Any
    ) -> NodeFault:
        """Hold the exchange until cleared, the command with it; then go on."""
        return self._add_fault(FaultKind.HANG, operation, slot, **kw)

    def drop_reply(
        self, operation: NodeOperation | None = None, slot: int | None = None, **kw: Any
    ) -> NodeFault:
        """
        Let the lock act on the frame, and lose what it sends back.

        A Get whose report times out resolves with nothing rather than an
        error (DRV:7959-7976): a fresh read answers "no credential", and a
        Set's verification read fails to confirm it, so the driver answers
        ``Error_Unknown`` (AC:1186-1194) -- the #1397 shape, where the code
        landed. On a Set, the Supervision report is lost too, which the
        driver treats as an unsupervised Set (DRV:7998-8002), and so is the
        verification read that follows. Counted in ``timeoutResponse``
        (DRV:7733-7743).
        """
        return self._add_fault(FaultKind.DROP_REPLY, operation, slot, **kw)

    def slow(
        self,
        operation: NodeOperation | None = None,
        slot: int | None = None,
        *,
        seconds: float,
        **kw: Any,
    ) -> NodeFault:
        """Make the exchange take ``seconds`` on the clock, then go on."""
        return self._add_fault(FaultKind.SLOW, operation, slot, seconds=seconds, **kw)

    def clear_faults(self) -> None:
        """
        Clear every fault installed on this lock, releasing what they hold.

        Spent faults included, as ``MockLCMLock.clear_faults`` does: one whose
        ``times`` ran out matches nothing more but may still hold the
        exchange it met last.
        """
        for fault in list(self.faults):
            fault.clear()

    async def _async_meet(
        self,
        operation: NodeOperation,
        slot: int,
        *,
        passes: frozenset[FaultKind] = frozenset(),
    ) -> NodeFault | None:
        """Return the fault an exchange meets; ``passes`` kinds wait for a later one."""
        fault = next(
            (
                f
                for f in self.faults
                if f.kind not in passes
                and not (f.times is not None and f.hits >= f.times)
                and f.matches(operation, slot)
            ),
            None,
        )
        if fault is None:
            return None
        fault.hits += 1
        await fault.async_before()
        return fault

    # ------------------------------------------------------------------
    # The driver's value database
    # ------------------------------------------------------------------

    def _get(self, cc_id: int, prop: str, key: int | None = None) -> Any:
        return self._values.get((cc_id, prop, key))

    def _set(self, cc_id: int, prop: str, key: int | None, value: Any) -> None:
        """
        Set a value as ``ValueDB.setValue`` does (VDB:219-267).

        A public value raises ``value added`` when new and ``value updated``
        otherwise -- even when the value is unchanged -- carrying the IDs the
        node adds (VAL:126-259); internal values raise nothing.
        """
        value_key = (cc_id, prop, key)
        existed = value_key in self._values
        previous = self._values.get(value_key)
        self._values[value_key] = value
        if prop not in _PUBLIC_PROPERTIES or cc_id != _UC:
            return
        args = self._value_args(prop, key)
        args["newValue"] = value
        if existed:
            args["prevValue"] = previous
        self._emit("value updated" if existed else "value added", args=args)

    def _remove(self, cc_id: int, prop: str, key: int | None) -> None:
        """Remove a value as ``ValueDB.removeValue`` does (VDB:272-294)."""
        value_key = (cc_id, prop, key)
        if value_key not in self._values:
            return
        previous = self._values.pop(value_key)
        if prop in _PUBLIC_PROPERTIES and cc_id == _UC:
            args = self._value_args(prop, key)
            args["prevValue"] = previous
            self._emit("value removed", args=args)

    def _value_args(self, prop: str, key: int | None) -> dict[str, Any]:
        return {
            "commandClass": _UC,
            "commandClassName": "User Code",
            "endpoint": 0,
            "property": prop,
            "propertyName": prop,
            "propertyKey": key,
            "propertyKeyName": str(key),
        }

    def _ensure_uc_metadata(self, slot: int) -> None:
        """
        Create a slot's metadata the first time a report persists it.

        ``setUserCodeMetadata`` runs before the values are set (UCC:197-243),
        raising ``metadata updated`` for each new value ID.
        """
        for prop, metadata in (
            (
                LOCK_USERCODE_STATUS_PROPERTY,
                {
                    "type": "number",
                    "readable": True,
                    "writeable": True,
                    "label": f"User ID status ({slot})",
                    "states": {"0": "Available", "1": "Enabled", "2": "Disabled"},
                    **_value_flags(LOCK_USERCODE_STATUS_PROPERTY),
                },
            ),
            (
                LOCK_USERCODE_PROPERTY,
                {
                    "type": "string",
                    "readable": True,
                    "writeable": True,
                    "minLength": 4,
                    "maxLength": 10,
                    "label": f"User Code ({slot})",
                    **_value_flags(LOCK_USERCODE_PROPERTY),
                },
            ),
        ):
            if (_UC, prop, slot) in self._metadata:
                continue
            self._metadata.add((_UC, prop, slot))
            args = self._value_args(prop, slot)
            args["metadata"] = metadata
            self._emit("metadata updated", args=args)

    def _remove_uc_metadata(self, slot: int) -> None:
        """
        Remove a slot's metadata as ``removeMetadata`` does (UCC:255-261, VDB:411-451).

        Each removal raises ``metadata updated``; the node replaces the
        now-missing metadata with what the value defines -- nothing for these
        dynamic values, so the default "any" (VAL:95-121, 139-141) -- and the
        client recreates the value from it, with no value (PY_NODE
        ``handle_metadata_updated``).
        """
        for prop in (LOCK_USERCODE_STATUS_PROPERTY, LOCK_USERCODE_PROPERTY):
            if (_UC, prop, slot) not in self._metadata:
                continue
            self._metadata.discard((_UC, prop, slot))
            args = self._value_args(prop, slot)
            args["metadata"] = {
                "type": "any",
                "readable": True,
                "writeable": True,
                **_value_flags(prop),
            }
            self._emit("metadata updated", args=args)

    def _emit(self, event: str, **data: Any) -> None:
        """Deliver one node event the way the client's listen loop does."""
        self.node.receive_event(
            ZwaveEvent(
                type=event,
                data={
                    "source": "node",
                    "event": event,
                    "nodeId": self.node.node_id,
                    **data,
                },
            )
        )

    # ------------------------------------------------------------------
    # Seeding: the driver's cache after the interview the dump records
    # ------------------------------------------------------------------

    def _seed_user_code(self, interviewed: bool, codes: dict[int, str] | None) -> None:
        """
        Cache what the dump holds, and give the lock the codes it caches.

        ``codes``, when given, is what the lock holds instead: a dump of a
        masking lock caches asterisks, not the codes behind them.
        """
        if not self._supports(_UC):
            return
        for value in self.node.values.values():
            if value.command_class != _UC or value.property_ not in _PUBLIC_PROPERTIES:
                continue
            slot = int(value.property_key)
            self._values[(_UC, value.property_, slot)] = value.value
            self._metadata.add((_UC, value.property_, slot))
        for slot in {key[2] for key in self._values if key[1] in _PUBLIC_PROPERTIES}:
            status = self._get(_UC, LOCK_USERCODE_STATUS_PROPERTY, slot)
            code = self._get(_UC, LOCK_USERCODE_PROPERTY, slot)
            if codes is None and status not in (None, UserIDStatus.AVAILABLE):
                self.uc_slots[slot] = (UserIDStatus(status), code or "")
        for slot, code in (codes or {}).items():
            self.uc_slots[slot] = (UserIDStatus.ENABLED, code)
        if interviewed:
            # An interview reads the users count and every slot (UCC
            # interview); the BE469 dump stops at slot 5.
            self._interview_user_code()

    def _seed_user_credential(self) -> None:
        """Cache the User Credential capabilities an interview reads (U3CC:91-140)."""
        if not self._supports(_U3C):
            return
        for prop, value in (
            ("supportedUsers", self.max_users),
            ("maxUserNameLength", self.max_user_name_length),
            ("supportedUserTypes", [UserCredentialUserType.GENERAL]),
            ("supportedCredentialRules", [UserCredentialRule.SINGLE]),
            ("supportedCredentialTypes", [_PIN, *self.other_credential_types]),
        ):
            self._set(_U3C, prop, None, value)
        for kind in self.other_credential_types:
            # Enrolled at the lock or in the vendor's app, never by LCM.
            self._set(
                _U3C,
                "credentialCapabilities",
                kind,
                {
                    "numberOfCredentialSlots": self.pin_slots,
                    "minCredentialLength": 1,
                    "maxCredentialLength": 32,
                    "maxCredentialHashLength": self.max_credential_hash_length,
                    "supportsCredentialLearn": True,
                },
            )
        self._set(
            _U3C,
            "credentialCapabilities",
            _PIN,
            {
                "numberOfCredentialSlots": self.pin_slots,
                "minCredentialLength": self.pin_lengths[0],
                "maxCredentialLength": self.pin_lengths[1],
                "maxCredentialHashLength": self.max_credential_hash_length,
                "supportsCredentialLearn": False,
            },
        )

    # ------------------------------------------------------------------
    # Server command dispatch
    # ------------------------------------------------------------------

    async def _async_dispatch(
        self, message: dict[str, Any], require_schema: int | None = None
    ) -> Any:
        command: str = message["command"]
        if message.get("nodeId") != self.node.node_id or not (
            command.startswith("endpoint.access_control.")
            or (
                command == "endpoint.invoke_cc_api"
                and message.get("commandClass") == _UC
                and message.get("methodName") == "getUsersCount"
            )
        ):
            return await self._previous(message, require_schema)
        self.commands.append(message)
        stats_before = dict(self.statistics)
        try:
            if command == "endpoint.invoke_cc_api":
                return {"response": await self._async_uc_get_users_count()}
            handler: Callable[[dict[str, Any]], Any] | None = getattr(
                self, "_ac_" + command.removeprefix("endpoint.access_control."), None
            )
            if handler is None:
                raise NotImplementedError(f"the fake node does not model {command}")
            # SRV_JSON:3-14 serializes the answer as JSON: a Map becomes an
            # object, and an undefined field disappears.
            return copy.deepcopy(await handler(message))
        finally:
            if self.statistics != stats_before:
                self.emit_statistics()

    # Capabilities (AC:294-443) -------------------------------------------

    async def _ac_is_supported(self, message: dict[str, Any]) -> dict[str, Any]:
        """SRV_AC:31-33: the endpoint has an access-control API (AC mixin)."""
        return {"supported": self._supports(_UC) or self._supports(_U3C)}

    def _user_capabilities(self) -> dict[str, Any]:
        if self.uses_user_credential_cc:
            caps = {
                "maxUsers": self._get(_U3C, "supportedUsers") or 0,
                "supportedUserTypes": self._get(_U3C, "supportedUserTypes") or [],
                "supportedCredentialRules": (
                    self._get(_U3C, "supportedCredentialRules") or []
                ),
                "supportsUsersWithoutCredentials": True,
            }
            if (length := self._get(_U3C, "maxUserNameLength")) is not None:
                caps["maxUserNameLength"] = length
            return caps
        # AC:324-351: User Code CC has no user names; the field is undefined.
        return {
            "maxUsers": self._get(_UC, "supportedUsers") or 0,
            "supportedUserTypes": [UserCredentialUserType.GENERAL],
            "supportedCredentialRules": [UserCredentialRule.SINGLE],
            "supportsUsersWithoutCredentials": False,
        }

    def _credential_capabilities(self) -> dict[str, dict[str, Any]]:
        if self.uses_user_credential_cc:
            return {
                str(kind): cap
                for kind in self._get(_U3C, "supportedCredentialTypes") or []
                if (cap := self._get(_U3C, "credentialCapabilities", kind))
            }
        # AC:401-423: one PIN type, its slot count the cached users count.
        return {
            str(_PIN): {
                "numberOfCredentialSlots": self._get(_UC, "supportedUsers") or 0,
                "minCredentialLength": 4,
                "maxCredentialLength": 10,
                "maxCredentialHashLength": 0,
                "supportsCredentialLearn": False,
            }
        }

    async def _ac_get_user_capabilities_cached(self, message):
        return {"capabilities": self._user_capabilities()}

    async def _ac_get_credential_capabilities_cached(self, message):
        return {
            "capabilities": {
                "supportedCredentialTypes": self._credential_capabilities(),
                "supportsAdminCode": False,
                "supportsAdminCodeDeactivation": False,
                "supportsCredentialAssignment": self.uses_user_credential_cc,
            }
        }

    def _assert_valid_user(self, user_id: int) -> None:
        """AC:1961-1973: a user ID outside the cached count is Argument_Invalid."""
        max_users = self._user_capabilities()["maxUsers"]
        if max_users and not 1 <= user_id <= max_users:
            raise zwave_error(
                ZWaveErrorCode.ARGUMENT_INVALID, f"User ID {user_id} is out of range"
            )

    def _assert_valid_slot(self, kind: int, slot: int) -> None:
        """AC:1945-1959: a slot outside the cached slot count is Argument_Invalid."""
        cap = self._credential_capabilities().get(str(kind))
        if cap is None or not 1 <= slot <= cap["numberOfCredentialSlots"]:
            raise zwave_error(
                ZWaveErrorCode.ARGUMENT_INVALID,
                f"Credential slot {slot} is out of range for credential type "
                f"{_driver_type_name(kind)}",
            )

    def _assert_valid_uc_type(self, kind: int) -> None:
        """AC:1979-1989: User Code CC holds only its one credential type."""
        if kind != _PIN:
            raise zwave_error(
                ZWaveErrorCode.ARGUMENT_INVALID,
                f"Credential type {_driver_type_name(kind)} is not "
                "supported by this node",
            )

    # Cached reads -------------------------------------------------------

    async def _ac_get_users_cached(self, message):
        """AC:563-591: every cached user up to the cached users count."""
        if self.uses_user_credential_cc:
            count = self._get(_U3C, "supportedUsers") or 0
            users = [self._u3c_cached_user(uid) for uid in range(1, count + 1)]
        else:
            count = self._get(_UC, "supportedUsers") or 0
            users = [self._uc_cached_user(uid) for uid in range(1, count + 1)]
        return {"users": [user for user in users if user is not None]}

    async def _ac_get_all_credentials_cached(self, message):
        """AC:1114-1120."""
        return {"credentials": self._all_cached_credentials()}

    def _all_cached_credentials(self) -> list[dict[str, Any]]:
        if self.uses_user_credential_cc:
            # AC:1904-1925: every cached owner entry, sorted by its key.
            return [
                self._u3c_cached_credential(key)
                for (cc_id, prop, key) in sorted(
                    k
                    for k in self._values
                    if k[0] == _U3C and k[1] == "credentialOwner"
                )
            ]
        # AC:1927-1943 with AC:2076-2097.
        count = self._get(_UC, "supportedUsers") or 0
        return [
            cred
            for slot in range(1, count + 1)
            if (cred := self._uc_cached_credential(slot)) is not None
        ]

    def _uc_cached_user(self, user_id: int) -> dict[str, Any] | None:
        """AC:2068-2074 and AC:1600-1636."""
        status = self._get(_UC, LOCK_USERCODE_STATUS_PROPERTY, user_id)
        if status is None:
            return None
        return _uc_user_data(user_id, status)

    def _uc_cached_credential(self, slot: int) -> dict[str, Any] | None:
        """AC:2076-2097: an occupied status and a cached code field."""
        status = self._get(_UC, LOCK_USERCODE_STATUS_PROPERTY, slot)
        if status in (
            None,
            UserIDStatus.AVAILABLE,
            UserIDStatus.STATUS_NOT_AVAILABLE,
        ):
            return None
        code = self._get(_UC, LOCK_USERCODE_PROPERTY, slot)
        if code is None:
            return None
        return {"userId": slot, "type": _PIN, "slot": slot, "data": code}

    def _u3c_cached_user(self, user_id: int) -> dict[str, Any] | None:
        """AC:1991-2027."""
        user_type = self._get(_U3C, "userType", user_id)
        if user_type is None:
            return None
        user = {
            "userId": user_id,
            "active": bool(self._get(_U3C, "userActive", user_id)),
            "userType": user_type,
        }
        if name := self._get(_U3C, "userName", user_id):
            user["userName"] = name
        if (rule := self._get(_U3C, "credentialRule", user_id)) is not None:
            user["credentialRule"] = rule
        return user

    def _u3c_cached_credential(self, key: int) -> dict[str, Any]:
        """AC:2040-2056: data may be absent when the lock sends none back."""
        cred = {
            "userId": self._get(_U3C, "credentialOwner", key),
            "type": key >> 16,
            "slot": key & 0xFFFF,
        }
        if (data := self._get(_U3C, "credential", key)) is not None:
            cred["data"] = data
        return cred

    # Fresh reads --------------------------------------------------------

    async def _ac_get_credential(self, message):
        """
        Read one slot from the lock (AC:955-978).

        User Code CC: a Get whose report is persisted (value events) before
        the answer; Available, StatusNotAvailable and a dropped report all
        answer "no credential". User Credential CC: a Credential Get
        (U3CC persist; no event for a ResponseToGet, U3CH:66-107).
        """
        kind, slot = message["credentialType"], message["credentialSlot"]
        self._assert_valid_slot(kind, slot)
        if self.uses_user_credential_cc:
            return await self._async_u3c_get_credential(kind, slot)
        report = await self._async_uc_get(slot)
        if report is None or report[0] in (
            UserIDStatus.AVAILABLE,
            UserIDStatus.STATUS_NOT_AVAILABLE,
        ):
            return {}
        return {
            "credential": {
                "userId": slot,
                "type": kind,
                "slot": slot,
                "data": report[1],
            }
        }

    async def _ac_get_users(self, message):
        """AC:491-557: a users-count query, then one Get per slot."""
        if self.uses_user_credential_cc:
            for user_id in sorted(self.users):
                self._u3c_persist_user(user_id, UserReportType.RESPONSE_TO_GET)
            self.statistics["commandsTX"] += len(self.users) + 1
            self.statistics["commandsRX"] += len(self.users) + 1
            return {
                "users": [
                    user
                    for uid in sorted(self.users)
                    if (user := self._u3c_cached_user(uid)) is not None
                ]
            }
        count = await self._async_uc_get_users_count()
        users = []
        for slot in range(1, (count or 0) + 1):
            report = await self._async_uc_get(slot)
            if report is not None and (user := _uc_user_data(slot, report[0])):
                users.append(user)
        return {"users": users}

    async def _ac_get_all_credentials(self, message):
        """AC:1102-1108 and AC:1853-1902 (User Code: a Get per slot)."""
        if self.uses_user_credential_cc:
            return {"credentials": await self._async_u3c_walk_credentials()}
        count = await self._async_uc_get_users_count()
        credentials = []
        for slot in range(1, (count or 0) + 1):
            answer = await self._ac_get_credential(
                {"credentialType": _PIN, "credentialSlot": slot}
            )
            if cred := answer.get("credential"):
                credentials.append(cred)
        return {"credentials": credentials}

    # Writes -------------------------------------------------------------

    async def _ac_set_credential(self, message):
        """AC:1126-1238; HA_LH:550-585 maps a non-OK result to an error key."""
        user_id = message["userId"]
        kind = message["credentialType"]
        slot = message["credentialSlot"]
        data = message["data"]
        self._assert_valid_user(user_id)
        if self.uses_user_credential_cc:
            self._assert_valid_slot(kind, slot)
            return {
                "result": await self._async_u3c_set_credential(
                    user_id, kind, slot, data
                )
            }
        self._assert_valid_uc_type(kind)
        return {"result": await self._async_uc_set_credential(user_id, kind, data)}

    async def _ac_delete_credential(self, message):
        """AC:1254-1336."""
        user_id = message.get("userId") or 0
        kind = message["credentialType"]
        slot = message["credentialSlot"]
        if self.uses_user_credential_cc:
            if user_id:
                self._assert_valid_user(user_id)
            self._assert_valid_slot(kind, slot)
            report = await self._async_u3c_credential_set(
                "clear", user_id, kind, slot, None
            )
            return {"result": _report_result(report, _CREDENTIAL_RESULT)}
        if user_id and user_id != slot:
            return {"result": SetCredentialResult.ERROR_MODIFY_REJECTED_LOCATION_EMPTY}
        target = user_id or slot
        self._assert_valid_user(target)
        self._assert_valid_uc_type(kind)
        return {"result": await self._async_uc_delete(target, kind)}

    async def _ac_set_user(self, message):
        """AC:743-868 (User Code: re-sends the cached code with the new status)."""
        user_id, options = message["userId"], message.get("options") or {}
        if not self.uses_user_credential_cc:
            raise NotImplementedError("the fake does not model setUser on User Code CC")
        existing = self._u3c_cached_user(user_id)
        report = await self._async_u3c_user_set(
            "modify" if existing else "add", user_id, {**(existing or {}), **options}
        )
        return {"result": _report_result(report, _USER_RESULT)}

    async def _ac_add_user(self, message):
        """
        ``addUser`` (AC:601-655).

        The user is added first; a bundled credential then goes through
        ``setCredential``, whose user and slot checks raise Argument_Invalid
        only after the user's Set has reached the lock (AC:649-654,
        AC:1132-1135).
        """
        user_id, options = message["userId"], message.get("options") or {}
        credential = message.get("credential")
        self._assert_valid_user(user_id)
        if not self.uses_user_credential_cc:
            raise NotImplementedError("the fake does not model addUser on User Code CC")
        report = await self._async_u3c_user_set("add", user_id, options)
        user_result = _report_result(report, _USER_RESULT)
        if user_result != SetUserResult.OK or credential is None:
            return {"result": {"user": user_result}}
        result = await self._ac_set_credential({"userId": user_id, **credential})
        return {"result": {"user": user_result, "credential": result["result"]}}

    async def _ac_delete_user(self, message):
        """AC:874-919."""
        user_id = message["userId"]
        if not self.uses_user_credential_cc:
            raise NotImplementedError(
                "the fake does not model deleteUser on User Code CC"
            )
        report = await self._async_u3c_user_set("delete", user_id, {})
        return {"result": _report_result(report, _USER_RESULT)}

    # ------------------------------------------------------------------
    # User Code CC: the lock's side and the driver's handling of it
    # ------------------------------------------------------------------

    def _uc_shown(self, slot: int) -> tuple[UserIDStatus, str]:
        """
        Return the report the lock sends for a slot (MOCK_UC ``respondToUserGet``).

        Past its capacity, StatusNotAvailable. An Available slot carries no
        code (UCC:1545-1551). An occupied one carries the code as
        ``code_display`` says.
        """
        if slot > self.capacity:
            return UserIDStatus.STATUS_NOT_AVAILABLE, ""
        status, code = self.uc_slots.get(slot, (UserIDStatus.AVAILABLE, ""))
        if status == UserIDStatus.AVAILABLE:
            return status, ""
        if self.code_display == "masked":
            return status, "*" * len(code)
        if self.code_display == "withheld":
            return status, ""
        return status, code

    def _uc_persist_report(self, slot: int, status: UserIDStatus, code: str) -> None:
        """
        Persist a User Code Report as ``persistUserCode`` does (UCC:245-270).

        StatusNotAvailable removes the slot's values, then their metadata;
        anything else ensures metadata, then sets the status, then the code.
        """
        if status == UserIDStatus.STATUS_NOT_AVAILABLE:
            self._remove(_UC, LOCK_USERCODE_STATUS_PROPERTY, slot)
            self._remove(_UC, LOCK_USERCODE_PROPERTY, slot)
            self._remove_uc_metadata(slot)
            return
        self._ensure_uc_metadata(slot)
        self._set(_UC, LOCK_USERCODE_STATUS_PROPERTY, slot, status)
        self._set(_UC, LOCK_USERCODE_PROPERTY, slot, code)

    async def _async_uc_get_users_count(self) -> int | None:
        """
        Users Number Get (UCC:502-518): the report persists the count (UCC:1675).

        Also what the provider's #1298 recovery calls through ``invoke_cc_api``.
        It addresses no slot, so it meets a ``get`` fault as slot 0 (any fault
        whose slot is None), and is recorded as a ``get`` of slot 0. A report
        that times out answers nothing and caches nothing (DRV:7959-7976): a
        lock asleep through its interview stays uncounted. Every User Code
        walk asks it first and returns no users without it (AC:496-497,
        AC:1860-1861), so a fault on it fails or empties every walk too.
        """
        fault = await self._async_meet("get", 0)
        self.exchanges.append(_Exchange("get", 0))
        self.statistics["commandsTX"] += 1
        if fault is not None and fault.kind is FaultKind.DROP_REPLY:
            self.statistics["timeoutResponse"] += 1
            return None
        self.statistics["commandsRX"] += 1
        self._set(_UC, "supportedUsers", None, self.capacity)
        return self.capacity

    async def _async_uc_get(
        self, slot: int, *, dropped: bool = False
    ) -> tuple[UserIDStatus, str] | None:
        """
        User Code Get (UCC:533-576): the report is persisted before it answers.

        DRV:4279-4281 persists every complete inbound command, solicited or
        not, before matching it to the waiting transaction; a report that
        times out answers nothing (DRV:7959-7976).
        """
        # ``dropped``: the read follows a Set whose replies were all lost, so
        # its outcome is already decided and it meets no fault of its own.
        fault = None if dropped else await self._async_meet("get", slot)
        self.exchanges.append(_Exchange("get", slot))
        self.statistics["commandsTX"] += 1
        if dropped or (fault is not None and fault.kind is FaultKind.DROP_REPLY):
            self.statistics["timeoutResponse"] += 1
            return None
        self.statistics["commandsRX"] += 1
        status, code = self._uc_shown(slot)
        self._uc_persist_report(slot, status, code)
        return status, code

    def _uc_numusers_check(self, user_id: int) -> None:
        """UCC:596-602 and UCC:739-745: the cached users count bounds a Set."""
        count = self._get(_UC, "supportedUsers")
        if count is not None and user_id > count:
            raise zwave_error(
                ZWaveErrorCode.ARGUMENT_INVALID,
                "The user ID must be between 0 and the number of supported "
                f"users {count}.",
            )

    async def _async_uc_device_set(
        self, operation: Literal["set", "clear"], slot: int, code: str | None
    ) -> tuple[SupervisionStatus | None, bool]:
        """
        One User Code Set; return its Supervision status and whether replies were lost.

        Supervised only on a node advertising Supervision CC, until a Set
        comes back NoSupport (DRV:8044-8051, ``mayUseSupervision``). The lock
        applies the Set unless a fault, the ``FAIL`` outcome or a duplicate
        stops it; a lock refusing a duplicate sends Access Control
        notification 15. A supervised Set reports ``supervision`` when
        applied and Fail when refused; ``ignore`` reports it applied anyway.
        NoSupport means the lock did not act on the encapsulated Set: the
        driver remembers that and sends it again unsupervised
        (DRV:8052-8064), so the verification read decides it.
        """
        self._uc_numusers_check(slot)
        supervised = (
            self._supports(SUPERVISION_CC_ID) and not self._uc_supervision_unsupported
        )
        no_support = supervised and self.supervision is SupervisionStatus.NO_SUPPORT
        # A NoSupport frame writes nothing, so faults aimed at the write
        # (ignore, refuse) wait for the frame that does: the resend.
        fault = await self._async_meet(
            operation, slot, passes=_WRITE_ONLY_FAULTS if no_support else frozenset()
        )
        self.exchanges.append(_Exchange(operation, slot, code))
        self.statistics["commandsTX"] += 1
        kind = None if fault is None else fault.kind
        lost = kind is FaultKind.DROP_REPLY
        if no_support:
            if lost:
                # No report, so no NoSupport to act on: the driver takes the
                # undefined result as unsupervised and reads back
                # (DRV:7998-8002, AC:1186-1194), finding what the lock kept.
                self.statistics["timeoutResponse"] += 1
                return None, False
            self.statistics["commandsRX"] += 1
            self._uc_supervision_unsupported = True
            return await self._async_uc_device_set(operation, slot, code)
        duplicate = (
            operation == "set"
            and self.rejects_duplicates
            and any(
                other != slot and held == code for other, held in self.codes.items()
            )
        )
        applied = (
            kind not in (FaultKind.IGNORE, FaultKind.REFUSE)
            and not duplicate
            and not (supervised and self.supervision is SupervisionStatus.FAIL)
        )
        if applied:
            if code is None:
                self.uc_slots.pop(slot, None)
            else:
                self.uc_slots[slot] = (UserIDStatus.ENABLED, code)
        if duplicate:
            self.notify_access_control(
                AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE,
                self.duplicate_parameters,
            )
        if not supervised:
            return None, lost
        if lost:
            self.statistics["timeoutResponse"] += 1
            return None, lost
        self.statistics["commandsRX"] += 1
        if applied or kind is FaultKind.IGNORE:
            return self.supervision, lost
        return SupervisionStatus.FAIL, lost

    async def _async_uc_set_credential(
        self, user_id: int, kind: int, code: str
    ) -> SetCredentialResult:
        """
        ``setCredential`` on User Code CC (AC:1160-1237).

        The status written keeps a cached occupied status, else Enabled. An
        unsupervised Set is confirmed by a read-back (V1 accepts an asterisk
        per digit, AC:245-265); a supervised success persists the code
        written, in clear, unread (AC:1195-1199); a supervised failure reads
        back and needs an exact match. Only a confirmed write raises
        ``credential added``/``modified``, carrying the code written. A
        refused or unconfirmed one answers Error_Unknown, or
        Error_ModifyRejectedLocationEmpty when a cached code's slot reads back
        Available after a failure.
        """
        existing_status = self._get(_UC, LOCK_USERCODE_STATUS_PROPERTY, user_id)
        status = (
            UserIDStatus.ENABLED
            if existing_status in (None, UserIDStatus.AVAILABLE)
            else UserIDStatus(existing_status)
        )
        existed = self._uc_cached_credential(user_id) is not None
        supervision, lost = await self._async_uc_device_set("set", user_id, code)
        failure = SetCredentialResult.ERROR_UNKNOWN
        if supervision is None:
            verified = await self._async_uc_get(user_id, dropped=lost)
            succeeded = (
                verified is not None
                and verified[0] == status
                and _read_back_confirms(code, verified[1], self.uc_version)
            )
        elif supervision in (SupervisionStatus.SUCCESS, SupervisionStatus.WORKING):
            succeeded = True
            # #persistCachedUserCode (AC:1683-1699): the two values, no metadata.
            # Before 15.25.2 nothing was persisted.
            if not self.driver_before_15_25_2:
                self._set(_UC, LOCK_USERCODE_STATUS_PROPERTY, user_id, status)
                self._set(_UC, LOCK_USERCODE_PROPERTY, user_id, code)
        elif self.driver_before_15_25_2:
            # Before 15.25.2 a reported failure was not read back.
            succeeded = False
        else:
            verified = await self._async_uc_get(user_id)
            succeeded = (
                verified is not None and verified[0] == status and verified[1] == code
            )
            if (
                not succeeded
                and existed
                and verified is not None
                and verified[0] == UserIDStatus.AVAILABLE
            ):
                failure = SetCredentialResult.ERROR_MODIFY_REJECTED_LOCATION_EMPTY
        if not succeeded:
            return failure
        self._emit(
            "credential modified" if existed else "credential added",
            endpointIndex=0,
            args={
                "userId": user_id,
                "credentialType": kind,
                "credentialSlot": user_id,
                "data": code,
            },
        )
        return SetCredentialResult.OK

    async def _async_uc_delete(self, user_id: int, kind: int) -> SetCredentialResult:
        """
        ``deleteCredential`` on User Code CC (AC:1286-1335).

        Success is a supervised success or a slot that reads back Available;
        then every cached value of the slot is reset to Available and ""
        (AC:1706-1727), and only a slot that held a cached code raises
        ``credential deleted`` then ``user deleted``.
        """
        existed = self._uc_cached_credential(user_id) is not None
        supervision, lost = await self._async_uc_device_set("clear", user_id, None)
        if supervision in (SupervisionStatus.SUCCESS, SupervisionStatus.WORKING):
            succeeded = True
        elif supervision is not None and self.driver_before_15_25_2:
            # Before 15.25.2 a reported failure was not read back.
            succeeded = False
        else:
            verified = await self._async_uc_get(user_id, dropped=lost)
            succeeded = verified is not None and verified[0] == UserIDStatus.AVAILABLE
        if not succeeded:
            return SetCredentialResult.ERROR_UNKNOWN
        for prop, value in (
            (LOCK_USERCODE_STATUS_PROPERTY, UserIDStatus.AVAILABLE),
            (LOCK_USERCODE_PROPERTY, ""),
        ):
            if (_UC, prop, user_id) in self._values:
                self._set(_UC, prop, user_id, value)
        if existed:
            self._emit(
                "credential deleted",
                endpointIndex=0,
                args={
                    "userId": user_id,
                    "credentialType": kind,
                    "credentialSlot": user_id,
                },
            )
            self._emit("user deleted", endpointIndex=0, args={"userId": user_id})
        return SetCredentialResult.OK

    # ------------------------------------------------------------------
    # User Credential CC: the lock's side and the driver's handling of it
    # ------------------------------------------------------------------

    @property
    def _read_back(self) -> bool:
        """MOCK_U3C:1062-1068: credential data comes back iff hash length is 0."""
        return self.max_credential_hash_length == 0

    def _u3c_persist_user(self, user_id: int, report_type: UserReportType) -> None:
        """U3CC:2314-2470: the driver's cache of one User Report."""
        if user_id == 0:
            # U3CC:2317-2346: user 0 deleted clears every cached user and
            # credential; any other user-0 report caches nothing.
            if report_type == UserReportType.USER_DELETED:
                for value_key in [
                    k for k in self._values if k[1] in _U3C_ENTRY_PROPERTIES
                ]:
                    del self._values[value_key]
            return
        if report_type in (
            UserReportType.USER_DELETED,
            UserReportType.USER_MODIFY_REJECTED_LOCATION_EMPTY,
        ):
            for prop in ("userType", "userActive", "credentialRule", "userName"):
                self._values.pop((_U3C, prop, user_id), None)
            for key in [
                key
                for (cc_id, prop, key), owner in self._values.items()
                if cc_id == _U3C and prop == "credentialOwner" and owner == user_id
            ]:
                self._values.pop((_U3C, "credentialOwner", key), None)
                self._values.pop((_U3C, "credential", key), None)
            return
        if report_type not in (
            UserReportType.RESPONSE_TO_GET,
            UserReportType.USER_ADDED,
            UserReportType.USER_MODIFIED,
            UserReportType.USER_ADD_REJECTED_LOCATION_OCCUPIED,
        ):
            return
        user = self.users[user_id]
        self._set(_U3C, "userType", user_id, user.user_type)
        self._set(_U3C, "userActive", user_id, user.active)
        self._set(_U3C, "credentialRule", user_id, user.credential_rule)
        self._set(_U3C, "userName", user_id, user.user_name)

    def _u3c_user_event(self, user_id: int, report_type: UserReportType) -> None:
        """U3CH:27-54: the events a User Report raises."""
        if report_type in (
            UserReportType.USER_ADDED,
            UserReportType.USER_ADD_REJECTED_LOCATION_OCCUPIED,
            UserReportType.USER_MODIFIED,
        ):
            user = self.users[user_id]
            args = {
                "userId": user_id,
                "active": user.active,
                "userType": user.user_type,
                "credentialRule": user.credential_rule,
            }
            if user.user_name:
                args["userName"] = user.user_name
            event = (
                "user modified"
                if report_type == UserReportType.USER_MODIFIED
                else "user added"
            )
            self._emit(event, endpointIndex=0, args=args)
        elif report_type in (
            UserReportType.USER_DELETED,
            UserReportType.USER_MODIFY_REJECTED_LOCATION_EMPTY,
        ):
            self._emit("user deleted", endpointIndex=0, args={"userId": user_id})

    async def _async_u3c_user_set(
        self, operation: Literal["add", "modify", "delete"], user_id: int, options
    ) -> UserReportType | None:
        """
        One User Set (MOCK_U3C:743-1047), its report persisted and handled.

        The lock ignores -- sends no report, so the driver answers
        Error_Unknown (AC:139-142) -- a user 0 Add or Modify, a user past its
        count, and a user type or credential rule it does not advertise
        (CC:0083.01.05.11.014/.015/.030/.039). It truncates a name past its
        maximum (CC:0083.01.05.13.047) and names a nameless user
        ``User-{id}``. The driver hands the report to the node's handler
        (AC:786), which raises the events.
        """
        fault = await self._async_meet("user", user_id)
        self.statistics["commandsTX"] += 1
        report_type = self._u3c_lock_handles_user_set(operation, user_id, options)
        if report_type is None or (
            fault is not None and fault.kind is FaultKind.DROP_REPLY
        ):
            self.statistics["timeoutResponse"] += 1
            return None
        self.statistics["commandsRX"] += 1
        self._u3c_persist_user(user_id, report_type)
        self._u3c_user_event(user_id, report_type)
        return report_type

    def _u3c_lock_handles_user_set(
        self, operation: Literal["add", "modify", "delete"], user_id: int, options
    ) -> UserReportType | None:
        """Act on a User Set as MOCK_U3C:743-1047 does; None when ignored."""
        if user_id > self.max_users or (operation != "delete" and user_id == 0):
            return None
        if operation == "delete" and user_id == 0:
            # MOCK_U3C:752-781 (CC:0083.01.05.11.007/.010): every user and
            # credential goes, reported as user 0's deletion.
            self.users.clear()
            self.credentials.clear()
            return UserReportType.USER_DELETED
        if operation == "delete":
            self.users.pop(user_id, None)
            for key in [
                k for k, (owner, _d) in self.credentials.items() if owner == user_id
            ]:
                del self.credentials[key]
            return UserReportType.USER_DELETED
        user_type = options.get("userType", UserCredentialUserType.GENERAL)
        rule = options.get("credentialRule", UserCredentialRule.SINGLE)
        if user_type not in (
            self._get(_U3C, "supportedUserTypes") or []
        ) or rule not in (self._get(_U3C, "supportedCredentialRules") or []):
            return None
        existing = self.users.get(user_id)
        if operation == "add" and existing is not None:
            return UserReportType.USER_ADD_REJECTED_LOCATION_OCCUPIED
        if operation == "modify" and existing is None:
            return UserReportType.USER_MODIFY_REJECTED_LOCATION_EMPTY
        name = (options.get("userName") or "")[: self.max_user_name_length]
        user = LockUser(
            user_type=user_type,
            active=options.get("active", True),
            credential_rule=rule,
            user_name=name or f"User-{user_id}",
        )
        if existing == user:
            return UserReportType.USER_UNCHANGED
        self.users[user_id] = user
        return (
            UserReportType.USER_ADDED
            if operation == "add"
            else UserReportType.USER_MODIFIED
        )

    def _u3c_credential_report(
        self,
        report_type: CredentialReportType,
        user_id: int,
        slot: int,
        data: str | None,
        kind: int = _PIN,
    ) -> None:
        """
        Persist and handle one Credential Report (U3CC:2874-3040, U3CH:56-108).

        Deletions and modify-rejections drop the cached slot; additions,
        modifications, read-back rejections and get responses cache its owner
        and, when the lock sent it back, its data. Then the events.
        """
        key = (kind << 16) | slot
        sent = data if self._read_back else None
        if report_type in (
            CredentialReportType.CREDENTIAL_DELETED,
            CredentialReportType.CREDENTIAL_MODIFY_REJECTED_LOCATION_EMPTY,
        ):
            self._values.pop((_U3C, "credential", key), None)
            self._values.pop((_U3C, "credentialOwner", key), None)
            self._emit(
                "credential deleted",
                endpointIndex=0,
                args={
                    "userId": user_id,
                    "credentialType": kind,
                    "credentialSlot": slot,
                },
            )
            return
        if report_type in (
            CredentialReportType.RESPONSE_TO_GET,
            CredentialReportType.CREDENTIAL_ADDED,
            CredentialReportType.CREDENTIAL_MODIFIED,
        ) or (
            report_type
            == CredentialReportType.CREDENTIAL_ADD_REJECTED_LOCATION_OCCUPIED
            and self._read_back
        ):
            if sent is not None:
                self._set(_U3C, "credential", key, sent)
            self._set(_U3C, "credentialOwner", key, user_id)
        event = {
            CredentialReportType.CREDENTIAL_ADDED: "credential added",
            CredentialReportType.CREDENTIAL_MODIFIED: "credential modified",
        }.get(report_type)
        if (
            report_type
            == CredentialReportType.CREDENTIAL_ADD_REJECTED_LOCATION_OCCUPIED
            and self._read_back
        ):
            event = "credential added"
        if event is None:
            return
        args: dict[str, Any] = {
            "userId": user_id,
            "credentialType": kind,
            "credentialSlot": slot,
        }
        if sent is not None:
            args["data"] = sent
        self._emit(event, endpointIndex=0, args=args)

    async def _async_u3c_credential_set(
        self,
        operation: Literal["set", "clear"],
        user_id: int,
        kind: int,
        slot: int,
        data: str | None,
        *,
        modify: bool = False,
    ) -> CredentialReportType | None:
        """
        One Credential Set, and the report it answers with (MOCK_U3C:1423-1780).

        None when the lock ignores the frame or its report is lost, which the
        driver maps to Error_Unknown (AC:158-161). A lost report changes
        nothing on the lock's side: it has already acted on the Set.
        """
        fault = await self._async_meet(operation, slot)
        self.exchanges.append(_Exchange(operation, slot, data))
        self.statistics["commandsTX"] += 1
        kind_of_fault = None if fault is None else fault.kind
        answer = self._u3c_lock_handles_credential_set(
            operation, user_id, kind, slot, data, modify, kind_of_fault
        )
        if answer is None or kind_of_fault is FaultKind.DROP_REPLY:
            self.statistics["timeoutResponse"] += 1
            return None
        self.statistics["commandsRX"] += 1
        report_type, report_user, report_data = answer
        self._u3c_credential_report(report_type, report_user, slot, report_data, kind)
        return report_type

    def _u3c_lock_handles_credential_set(
        self,
        operation: Literal["set", "clear"],
        user_id: int,
        kind: int,
        slot: int,
        data: str | None,
        modify: bool,
        fault: FaultKind | None,
    ) -> tuple[CredentialReportType, int, str | None] | None:
        """
        Act on a Credential Set as MOCK_U3C:1423-1780 does.

        Returns the report (type, user, data) or None when the lock ignores
        the frame. A Delete addressed to user 0 with a type and slot is an
        invalid combination and ignored (MOCK_U3C:1476-1513); one addressed to
        a user deletes the credential only if that user owns it
        (MOCK_U3C:202-214), yet reports it deleted either way -- so the
        driver drops its cached entry while the lock keeps the code.
        """
        occupied = self.credentials.get((kind, slot))
        if operation == "clear":
            if user_id == 0 or fault is FaultKind.REFUSE:
                return None
            owned = occupied is not None and occupied[0] == user_id
            if owned and fault is not FaultKind.IGNORE:
                del self.credentials[(kind, slot)]
            return CredentialReportType.CREDENTIAL_DELETED, user_id, None
        assert data is not None
        # MOCK_U3C:1552-1570: an Add or Modify with no settable length
        # (CC:0083.01.0A.11.023), or data longer than the maximum (.014) or
        # shorter than the minimum (.015), is ignored.
        shortest, longest = self.pin_lengths
        if longest == 0 or not shortest <= len(data) <= longest:
            return None
        if user_id not in self.users:
            return CredentialReportType.WRONG_USER_UNIQUE_IDENTIFIER, user_id, data
        if not modify and occupied is not None:
            return (
                CredentialReportType.CREDENTIAL_ADD_REJECTED_LOCATION_OCCUPIED,
                occupied[0],
                occupied[1],
            )
        if modify and occupied is None:
            return (
                CredentialReportType.CREDENTIAL_MODIFY_REJECTED_LOCATION_EMPTY,
                user_id,
                data,
            )
        if modify and occupied[0] != user_id:
            return CredentialReportType.WRONG_USER_UNIQUE_IDENTIFIER, user_id, data
        if any(
            other != (kind, slot) and held == data
            for other, (_owner, held) in self.credentials.items()
            if other[0] == kind
        ):
            return CredentialReportType.DUPLICATE_CREDENTIAL, user_id, data
        if fault is FaultKind.REFUSE:
            return CredentialReportType.MANUFACTURER_SECURITY_RULES, user_id, data
        if occupied is not None and occupied[1] == data:
            return CredentialReportType.CREDENTIAL_UNCHANGED, user_id, data
        if fault is not FaultKind.IGNORE:
            self.credentials[(kind, slot)] = (user_id, data)
        return (
            CredentialReportType.CREDENTIAL_MODIFIED
            if modify
            else CredentialReportType.CREDENTIAL_ADDED,
            user_id,
            data,
        )

    async def _async_u3c_set_credential(
        self, user_id: int, kind: int, slot: int, data: str
    ) -> SetCredentialResult:
        """AC:1134-1159: Modify only when the cache says this user owns the slot."""
        modify = self._get(_U3C, "credentialOwner", (kind << 16) | slot) == user_id
        report = await self._async_u3c_credential_set(
            "set", user_id, kind, slot, data, modify=modify
        )
        return _report_result(report, _CREDENTIAL_RESULT)

    def _u3c_next_after(self, kind: int, slot: int) -> tuple[int, int]:
        """MOCK_U3C:376-405: the next credential after (kind, slot), or (0, 0)."""
        return next(
            (ref for ref in sorted(self.credentials) if ref > (kind, slot)), (0, 0)
        )

    async def _async_u3c_credential_get(
        self, kind: int, slot: int
    ) -> dict[str, Any] | None:
        """
        One Credential Get (0, kind, slot): the report, persisted, or None if lost.

        The lock answers as MOCK_U3C:1049-1414 does: (0, 0) asks for the first
        credential, (kind, slot) for that one; either names the next one. A
        miss is a length-zero report naming user 0 (MOCK_U3C:1180-1196,
        1376-1388), which the driver caches unless it names no slot
        (U3CC:2981-2988). No event: a get response raises none (U3CH:66-107).
        """
        fault = await self._async_meet("get", slot)
        self.exchanges.append(_Exchange("get", slot))
        self.statistics["commandsTX"] += 1
        if fault is not None and fault.kind is FaultKind.DROP_REPLY:
            self.statistics["timeoutResponse"] += 1
            return None
        self.statistics["commandsRX"] += 1
        if (kind, slot) == (0, 0):
            ref = min(self.credentials, default=None)
        elif (kind, slot) in self.credentials:
            ref = (kind, slot)
        else:
            ref = None
        if ref is None:
            report = {
                "userId": 0,
                "type": kind,
                "slot": slot,
                "data": "",
                "next": (0, 0) if kind == 0 else self._u3c_next_after(kind, slot),
            }
        else:
            owner, data = self.credentials[ref]
            report = {
                "userId": owner,
                "type": ref[0],
                "slot": ref[1],
                "data": data,
                "next": self._u3c_next_after(*ref),
            }
        if not self._read_back or report["type"] == 0:
            report.pop("data")
        if report["type"] != 0 and report["slot"] != 0:
            self._u3c_credential_report(
                CredentialReportType.RESPONSE_TO_GET,
                report["userId"],
                report["slot"],
                report.get("data"),
                report["type"],
            )
        return report

    async def _async_u3c_get_credential(self, kind: int, slot: int) -> dict[str, Any]:
        """
        ``getCredential`` (AC:961-964): any report naming a slot is a credential.

        So an empty slot comes back as user 0's (AC:1729-1745).
        """
        report = await self._async_u3c_credential_get(kind, slot)
        if report is None or not report["slot"]:
            return {}
        return {"credential": _u3c_credential_data(report)}

    def _u3c_purge(self, from_key: int, to_key: int) -> None:
        """``#purgeCachedCredentials`` (AC:1645-1677) for user 0: the keys in [from, to)."""
        for cc_id, prop, key in list(self._values):
            if (
                cc_id == _U3C
                and prop in ("credentialOwner", "credential")
                and from_key <= key < to_key
            ):
                del self._values[(cc_id, prop, key)]

    async def _async_u3c_walk_credentials(self) -> list[dict[str, Any]]:
        """
        ``#queryCredentials_U3C(0, None, 0)`` (AC:1747-1851): the driver's cursor walk.

        Get (0, None, 0) first, then the (type, slot) each report points to
        next. Purges stale cache entries before the first credential, between
        each and the next, and after the last; a report that never arrives
        ends the walk where it is, purging nothing more (AC:1784-1786).
        """
        scope_end = 0x100 << 16
        credentials: list[dict[str, Any]] = []
        query = (0, 0)
        while True:
            report = await self._async_u3c_credential_get(*query)
            if report is not None and not report["slot"] and not credentials:
                self._u3c_purge(0, scope_end)
            if report is None or not report["slot"]:
                break
            key = (report["type"] << 16) | report["slot"]
            if not credentials:
                self._u3c_purge((query[0] << 16) | query[1], key)
            credentials.append(_u3c_credential_data(report))
            next_type, next_slot = report["next"]
            if (next_type, next_slot) == (0, 0):
                self._u3c_purge(key + 1, scope_end)
                break
            if (next_type, next_slot) == query:
                break
            self._u3c_purge(key + 1, (next_type << 16) | next_slot)
            query = (next_type, next_slot)
        return credentials


# The driver's own names for credential types, which its messages use
# (getEnumMemberName, shared/src/utils.ts:56-58, over cc/src/lib/_Types.ts:
# 1654-1669); the python
# client's enum spells them differently.
_DRIVER_CREDENTIAL_TYPE_NAMES = {
    0x00: "None",
    0x01: "PINCode",
    0x02: "Password",
    0x03: "RFIDCode",
    0x04: "BLE",
    0x05: "NFC",
    0x06: "UWB",
    0x07: "EyeBiometric",
    0x08: "FaceBiometric",
    0x09: "FingerBiometric",
    0x0A: "HandBiometric",
    0x0B: "UnspecifiedBiometric",
    0x0C: "DESFire",
}


def _driver_type_name(kind: int) -> str:
    """Return a credential type as the driver names it in a message."""
    # num2hex (shared/src/strings.ts:18-27) pads to an even number of digits.
    digits = f"{kind:x}"
    return _DRIVER_CREDENTIAL_TYPE_NAMES.get(
        kind, f"unknown (0x{'0' * (len(digits) % 2)}{digits})"
    )


def _value_flags(prop: str) -> dict[str, bool]:
    """
    Return the flags the node merges into every metadata event (VAL:95-121).

    The server forwards them from schema 28 on (``dumpMetadata``); only the
    code is secret (UCC ``UserCodeCCValues``).
    """
    return {"stateful": True, "secret": prop == LOCK_USERCODE_PROPERTY}


def _u3c_credential_data(report: dict[str, Any]) -> dict[str, Any]:
    """``#mapCredentialData`` (AC:1729-1745): a report as a credential."""
    cred = {"userId": report["userId"], "type": report["type"], "slot": report["slot"]}
    if "data" in report:
        cred["data"] = report["data"]
    return cred


def _uc_user_data(user_id: int, status: int) -> dict[str, Any] | None:
    """AC:1600-1636: a slot's status as a unified user, or None if no user."""
    if status in (
        UserIDStatus.AVAILABLE,
        UserIDStatus.STATUS_NOT_AVAILABLE,
        UserIDStatus.PASSAGE_MODE,
    ):
        return None
    return {
        "userId": user_id,
        "active": status != UserIDStatus.DISABLED,
        "userType": (
            UserCredentialUserType.NON_ACCESS
            if status == UserIDStatus.MESSAGING
            else UserCredentialUserType.GENERAL
        ),
    }


def _read_back_confirms(expected: str, actual: str | None, version: int) -> bool:
    """
    ``userCodeReadBackConfirmsSet`` (AC:245-265).

    An exact match, or, on User Code CC version 1 only, a non-empty code of
    nothing but asterisks (CC:0063.01.00.32.002).
    """
    if actual is None:
        return False
    if actual == expected:
        return True
    return version == 1 and bool(actual) and actual == "*" * len(actual)


def _report_result(report: IntEnum | None, table: dict[Any, IntEnum]) -> int:
    """Map a report type to the API result; a missing report is Error_Unknown."""
    if report is None:
        return 0xFF
    return int(table.get(report, 0xFF))
