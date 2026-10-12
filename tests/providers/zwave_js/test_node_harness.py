"""
Self-tests for the stateful Z-Wave JS lock fake (``fake_node.py``).

Each test drives the fake through the real layers above it -- the python
client's ``AccessControlAPI``, Home Assistant's ``lock_helpers`` and, for the
end-to-end ones, the provider inside a loaded entry -- and asserts what the
driver does, cited by file and line (abbreviations in ``fake_node.py``).
"""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass, NodeStatus
from zwave_js_server.const.command_class.access_control import (
    SetUserResult,
    UserCredentialType,
    UserCredentialUserType,
)
from zwave_js_server.const.command_class.lock import (
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
)
from zwave_js_server.exceptions import FailedZWaveCommand
from zwave_js_server.model.access_control import SetUserOptions
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import get_usercode

from homeassistant.components.zwave_js import lock_helpers
from homeassistant.components.zwave_js.const import ZWAVE_JS_NOTIFICATION_EVENT
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.domain.credentials import WriteResult
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockDisconnected,
    LockOperationUnsupported,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from tests.common import (
    async_advance_clock,
    async_run_until_quiet,
    async_tick_until,
    code_entity_id,
    in_sync_entity_id,
)
from tests.providers.zwave_js.conftest import get_zwave_lock
from tests.providers.zwave_js.fake_node import (
    FakeZWaveLock,
    SupervisionStatus,
    UserIDStatus,
    ZWaveErrorCode,
    supervised_lock_state,
    user_credential_lock_state,
)
from tests.providers.zwave_js.helpers import async_capture_events

PIN = UserCredentialType.PIN_CODE

_RECORDED = (
    "value added",
    "value updated",
    "value removed",
    "metadata updated",
    "credential added",
    "credential modified",
    "credential deleted",
    "user added",
    "user modified",
    "user deleted",
    "notification",
)


def record(node: Node) -> list[tuple[Any, ...]]:
    """
    Record the node events the client delivers, in order, as short tuples.

    Value events record (event, property, key, new value), metadata events
    (event, property, key, metadata type); access-control
    events (event, slot or user, data); notifications (event, type, event).
    """
    seen: list[tuple[Any, ...]] = []

    def on(name: str):
        def handler(event: dict[str, Any]) -> None:
            args = event.get("args")
            if name.startswith("value"):
                seen.append(
                    (name, args["property"], args["propertyKey"], args.get("newValue"))
                )
            elif name.startswith("metadata"):
                seen.append(
                    (
                        name,
                        args["property"],
                        args["propertyKey"],
                        args["metadata"]["type"],
                    )
                )
            elif name.startswith("credential"):
                seen.append((name, args.credential_slot, getattr(args, "data", None)))
            elif name.startswith("user"):
                seen.append((name, args.user_id))
            else:
                seen.append((name, args["type"], args["event"]))

        return handler

    for name in _RECORDED:
        node.on(name, on(name))
    return seen


def _values(seen: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    return [event for event in seen if event[0].startswith("value")]


# ---------------------------------------------------------------------------
# 1. A stateful node: the value database, cached reads, writes and their events
# ---------------------------------------------------------------------------


class TestUserCodeNode:
    """The BE469: User Code CC v1, unsupervised, as the dump caches it."""

    async def test_cached_reads_answer_from_the_value_database(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        ``get_users_cached``/``get_all_credentials_cached`` read the cache (AC:563-591, 1927-1943).

        The dump caches slots 1 and 2 Enabled; the interview the fake
        completes caches the other 28 Available, so neither read lists them.
        A slot the lock changes without a report is not in the cache.
        """
        ac = lock_schlage_be469.access_control
        assert [u.user_id for u in await ac.get_users_cached()] == [1, 2]
        assert [(c.slot, c.data) for c in await ac.get_all_credentials_cached()] == [
            (1, "9999"),
            (2, "1234"),
        ]
        fake_zwave_lock.uc_slots[3] = (UserIDStatus.ENABLED, "5555")

        assert [c.slot for c in await ac.get_all_credentials_cached()] == [1, 2]
        assert fake_zwave_lock.holds(3) == "5555"

    async def test_a_report_persists_status_then_code_and_reaches_the_client(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A User Code report sets status, then code, each with a value event (UCC:245-270).

        No unified credential event follows on a 15.x driver: the report
        path never calls the access-control API (UCC:1586-1597).
        """
        seen = record(lock_schlage_be469)

        fake_zwave_lock.program(3, "5555")

        assert seen == [
            ("value updated", LOCK_USERCODE_STATUS_PROPERTY, 3, UserIDStatus.ENABLED),
            ("value updated", LOCK_USERCODE_PROPERTY, 3, "5555"),
        ]
        assert get_usercode(lock_schlage_be469, 3)["usercode"] == "5555"
        assert [
            c.slot
            for c in await lock_schlage_be469.access_control.get_all_credentials_cached()
        ] == [1, 2, 3]

    async def test_a_value_event_fires_even_when_nothing_changed(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """``ValueDB.setValue`` emits ``value updated`` for an unchanged value (VDB:239-259)."""
        seen = record(lock_schlage_be469)

        fake_zwave_lock.report(2)

        assert _values(seen) == [
            ("value updated", LOCK_USERCODE_STATUS_PROPERTY, 2, UserIDStatus.ENABLED),
            ("value updated", LOCK_USERCODE_PROPERTY, 2, "1234"),
        ]

    async def test_a_new_slot_gets_metadata_then_value_added(
        self, lock_schlage_be469: Node, zwave_client
    ) -> None:
        """
        The first report of a slot creates metadata, then adds values (UCC:197-243, VDB:251-253).

        Seen while the fake completes an interview the dump stops short of.
        """
        seen = record(lock_schlage_be469)

        FakeZWaveLock(zwave_client, lock_schlage_be469, supported_users=6)

        assert seen[-4:] == [
            ("metadata updated", LOCK_USERCODE_STATUS_PROPERTY, 6, "number"),
            ("metadata updated", LOCK_USERCODE_PROPERTY, 6, "string"),
            ("value added", LOCK_USERCODE_STATUS_PROPERTY, 6, UserIDStatus.AVAILABLE),
            ("value added", LOCK_USERCODE_PROPERTY, 6, ""),
        ]

    @pytest.mark.parametrize(
        ("slot", "event"), [(3, "credential added"), (2, "credential modified")]
    )
    async def test_an_unsupervised_set_reads_back_then_announces(
        self,
        fake_zwave_lock: FakeZWaveLock,
        lock_schlage_be469: Node,
        slot: int,
        event: str,
    ) -> None:
        """
        An unsupervised Set is verified by a Get, then announced (AC:1186-1194, 1219-1235).

        The read-back persists first; the event carries the PIN written, and
        is ``modified`` only for a slot whose code the cache held.
        """
        seen = record(lock_schlage_be469)

        await lock_helpers.async_set_credential(
            lock_schlage_be469, slot, PIN, "4321", credential_slot=slot
        )

        assert seen == [
            (
                "value updated",
                LOCK_USERCODE_STATUS_PROPERTY,
                slot,
                UserIDStatus.ENABLED,
            ),
            ("value updated", LOCK_USERCODE_PROPERTY, slot, "4321"),
            (event, slot, "4321"),
        ]
        assert [(e.operation, e.slot) for e in fake_zwave_lock.exchanges] == [
            ("set", slot),
            ("get", slot),
        ]
        assert fake_zwave_lock.holds(slot) == "4321"

    async def test_a_clear_reads_back_resets_the_cache_and_announces_a_held_code(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A clear reads back, resets the slot's cached values, then deletes (AC:1286-1335).

        ``#clearCachedUserCodes`` re-sets Available and "" (AC:1706-1727),
        events and all; ``credential deleted`` and ``user deleted`` follow only
        for a slot whose code the cache held.
        """
        seen = record(lock_schlage_be469)

        await lock_helpers.async_delete_credential(lock_schlage_be469, 2, PIN, 2)
        await lock_helpers.async_delete_credential(lock_schlage_be469, 4, PIN, 4)

        cleared = [
            ("value updated", LOCK_USERCODE_STATUS_PROPERTY, 2, UserIDStatus.AVAILABLE),
            ("value updated", LOCK_USERCODE_PROPERTY, 2, ""),
        ]
        assert seen == [
            *cleared,
            *cleared,
            ("credential deleted", 2, None),
            ("user deleted", 2),
            *[(n, p, 4, v) for n, p, _k, v in cleared],
            *[(n, p, 4, v) for n, p, _k, v in cleared],
        ]
        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.clears == [2, 4]


# ---------------------------------------------------------------------------
# 2. The single-slot read
# ---------------------------------------------------------------------------


class TestSingleSlotRead:
    """``get_credential``: a Get of one slot, the provider's confirmation read."""

    async def test_it_answers_what_the_lock_holds_and_repairs_the_cache(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        The report is persisted before the answer (DRV:4279-4281, AC:966-977).

        So a slot the lock changed silently is right in the cache afterwards.
        """
        fake_zwave_lock.uc_slots[3] = (UserIDStatus.ENABLED, "5555")
        ac = lock_schlage_be469.access_control

        cred = await ac.get_credential(PIN, 3)

        assert (cred.user_id, cred.slot, cred.data) == (3, 3, "5555")
        assert get_usercode(lock_schlage_be469, 3)["usercode"] == "5555"
        assert await ac.get_credential(PIN, 4) is None

    async def test_a_report_that_never_arrives_answers_nothing(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A Get that times out resolves with nothing, not an error (DRV:7959-7976).

        The cache keeps its stale value, and the timeout is counted
        (DRV:7733-7743) and announced in ``statistics updated``.
        """
        fake_zwave_lock.uc_slots.pop(2)
        seen = record(lock_schlage_be469)

        with fake_zwave_lock.drop_reply("get", 2):
            assert (
                await lock_schlage_be469.access_control.get_credential(PIN, 2) is None
            )

        assert _values(seen) == []
        assert get_usercode(lock_schlage_be469, 2)["usercode"] == "1234"
        assert lock_schlage_be469.statistics.timeout_response == 1

    async def test_a_get_that_cannot_be_sent_raises(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """An unacknowledged frame fails the command with Controller_CallbackNOK (SM:101-112)."""
        fake_zwave_lock.fail("get", 2)

        with pytest.raises(FailedZWaveCommand) as err:
            await lock_schlage_be469.access_control.get_credential(PIN, 2)

        assert err.value.zwave_error_code == ZWaveErrorCode.CONTROLLER_CALLBACK_NOK
        assert err.value.zwave_error_message.endswith("(ZW0204)")
        assert lock_schlage_be469.statistics.commands_dropped_tx == 1

    async def test_a_slot_beyond_the_cached_count_is_argument_invalid(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """``#assertValidSlot`` refuses before any radio traffic (AC:959, 1945-1959)."""
        with pytest.raises(FailedZWaveCommand) as err:
            await lock_schlage_be469.access_control.get_credential(PIN, 31)

        assert err.value.zwave_error_code == ZWaveErrorCode.ARGUMENT_INVALID
        assert fake_zwave_lock.exchanges == []


# ---------------------------------------------------------------------------
# 3. Supervision outcomes, masked and withheld codes
# ---------------------------------------------------------------------------


class TestSupervisedNode:
    """A User Code lock advertising Supervision CC (DRV:8044-8051)."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, advertising Supervision CC."""
        return supervised_lock_state(lock_schlage_be469_state)

    @pytest.mark.parametrize(
        "status", [SupervisionStatus.SUCCESS, SupervisionStatus.WORKING]
    )
    async def test_success_or_working_persists_the_code_without_reading_back(
        self,
        fake_zwave_lock: FakeZWaveLock,
        lock_schlage_be469: Node,
        status: SupervisionStatus,
    ) -> None:
        """
        Working counts as success (SUP:35-45); the code written is cached unread (AC:1195-1199).

        In clear, even on a lock that masks its reports.
        """
        fake_zwave_lock.supervision = status
        fake_zwave_lock.code_display = "masked"
        seen = record(lock_schlage_be469)

        await lock_helpers.async_set_credential(
            lock_schlage_be469, 3, PIN, "4321", credential_slot=3
        )

        assert [(e.operation, e.slot) for e in fake_zwave_lock.exchanges] == [
            ("set", 3)
        ]
        assert seen[-1] == ("credential added", 3, "4321")
        assert get_usercode(lock_schlage_be469, 3)["usercode"] == "4321"

    async def test_an_ignored_set_reported_success_leaves_the_cache_wrong(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """A lock that reports Success without storing: the driver believes it (AC:1195-1199)."""
        fake_zwave_lock.supervision = SupervisionStatus.SUCCESS
        fake_zwave_lock.ignore("set", 3)

        await lock_helpers.async_set_credential(
            lock_schlage_be469, 3, PIN, "4321", credential_slot=3
        )

        assert fake_zwave_lock.holds(3) is None
        assert get_usercode(lock_schlage_be469, 3)["usercode"] == "4321"

    async def test_fail_reads_back_and_answers_unknown(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A supervised Fail is read back for an exact match (AC:1200-1208).

        Not found, the driver answers Error_Unknown, which Home Assistant
        raises as ``credential_rejected_unknown`` (HA_LH:106-144).
        """
        fake_zwave_lock.supervision = SupervisionStatus.SUCCESS
        fake_zwave_lock.refuse("set", 3)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 3, PIN, "4321", credential_slot=3
            )

        assert err.value.translation_key == "credential_rejected_unknown"
        assert [(e.operation, e.slot) for e in fake_zwave_lock.exchanges] == [
            ("set", 3),
            ("get", 3),
        ]

    async def test_fail_on_a_cached_code_now_empty_is_modify_rejected(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """A failed Set of a cached slot that reads back Available (AC:1209-1217)."""
        fake_zwave_lock.supervision = SupervisionStatus.SUCCESS
        fake_zwave_lock.uc_slots.pop(2)
        fake_zwave_lock.refuse("set", 2)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 2, PIN, "4321", credential_slot=2
            )

        assert err.value.translation_key == "credential_rejected_modify_empty"

    async def test_a_lost_supervision_report_falls_back_to_a_read_back(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        No Supervision report is an unsupervised Set (DRV:7998-8002).

        ``drop_reply`` loses the read-back too: the code landed, and the
        driver answers Error_Unknown -- the #1397 shape.
        """
        fake_zwave_lock.supervision = SupervisionStatus.SUCCESS
        fake_zwave_lock.drop_reply("set", 3)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 3, PIN, "4321", credential_slot=3
            )

        assert err.value.translation_key == "credential_rejected_unknown"
        assert fake_zwave_lock.holds(3) == "4321"


class TestSupervisionComesFromTheDump:
    """Whether Sets are supervised is the node's to say (DRV:8044-8051)."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, request: pytest.FixtureRequest, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, with Supervision CC when the test is parametrized so."""
        if hasattr(request, "param"):
            return supervised_lock_state(lock_schlage_be469_state)
        return lock_schlage_be469_state

    async def test_an_outcome_on_a_node_without_supervision_is_refused(
        self, zwave_client, lock_schlage_be469: Node
    ) -> None:
        """The BE469 advertises no Supervision CC, so no Set of it is supervised."""
        with pytest.raises(ValueError, match="Supervision CC"):
            FakeZWaveLock(
                zwave_client, lock_schlage_be469, supervision=SupervisionStatus.SUCCESS
            )

    @pytest.mark.parametrize(
        "lock_schlage_be469_state",
        [pytest.param(None, id="supervised")],
        indirect=True,
    )
    async def test_a_supervised_node_supervises_by_default(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """Success, so no read-back (AC:1195-1199)."""
        await lock_helpers.async_set_credential(
            lock_schlage_be469, 3, PIN, "4321", credential_slot=3
        )

        assert [(e.operation, e.slot) for e in fake_zwave_lock.exchanges] == [
            ("set", 3)
        ]

    @pytest.mark.parametrize(
        "lock_schlage_be469_state",
        [pytest.param(None, id="supervised")],
        indirect=True,
    )
    async def test_no_support_resends_unsupervised_and_reads_back(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        NoSupport: the driver remembers it and resends unsupervised (DRV:8052-8064).

        So the read-back decides, with the version 1 asterisk tolerance
        (AC:245-265), and the next Set goes out unsupervised at once.
        """
        fake_zwave_lock.supervision = SupervisionStatus.NO_SUPPORT
        fake_zwave_lock.code_display = "masked"

        await lock_helpers.async_set_credential(
            lock_schlage_be469, 3, PIN, "4321", credential_slot=3
        )
        await lock_helpers.async_set_credential(
            lock_schlage_be469, 4, PIN, "8765", credential_slot=4
        )

        assert [(e.operation, e.slot) for e in fake_zwave_lock.exchanges] == [
            ("set", 3),
            ("set", 3),
            ("get", 3),
            ("set", 4),
            ("get", 4),
        ]
        assert fake_zwave_lock.codes == {1: "9999", 2: "1234", 3: "4321", 4: "8765"}

    @pytest.mark.parametrize(
        "lock_schlage_be469_state",
        [pytest.param(None, id="supervised")],
        indirect=True,
    )
    @pytest.mark.parametrize(
        "fake_zwave_lock_options",
        [{"interviewed": False, "supervision": SupervisionStatus.SUCCESS}],
    )
    async def test_a_supervised_success_persists_values_without_metadata(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """``#persistCachedUserCode`` only sets the two values (AC:1683-1699)."""
        seen = record(lock_schlage_be469)

        await lock_helpers.async_set_credential(
            lock_schlage_be469, 6, PIN, "4321", credential_slot=6
        )

        assert seen == [
            ("value added", LOCK_USERCODE_STATUS_PROPERTY, 6, UserIDStatus.ENABLED),
            ("value added", LOCK_USERCODE_PROPERTY, 6, "4321"),
            ("credential added", 6, "4321"),
        ]


class TestCodeDisplay:
    """What the read-back sees on locks that do not show their codes."""

    @pytest.mark.parametrize(
        ("version_fixture", "display", "confirmed"),
        [
            ("lock_schlage_be469", "masked", True),
            ("lock_schlage_be469_v2", "masked", False),
            ("lock_schlage_be469", "withheld", False),
        ],
        ids=["v1_masked", "v2_masked", "v1_withheld"],
    )
    async def test_the_read_back_tolerates_asterisks_only_on_version_1(
        self,
        request: pytest.FixtureRequest,
        zwave_client,
        version_fixture: str,
        display: str,
        confirmed: bool,
    ) -> None:
        """
        ``userCodeReadBackConfirmsSet`` (AC:245-265): asterisks pass on v1 only.

        Otherwise the driver answers Error_Unknown even though the code
        landed (the #1258 class of report).
        """
        node: Node = request.getfixturevalue(version_fixture)
        fake = FakeZWaveLock(zwave_client, node, code_display=display)

        if confirmed:
            await lock_helpers.async_set_credential(
                node, 3, PIN, "4321", credential_slot=3
            )
        else:
            with pytest.raises(HomeAssistantError) as err:
                await lock_helpers.async_set_credential(
                    node, 3, PIN, "4321", credential_slot=3
                )
            assert err.value.translation_key == "credential_rejected_unknown"
        assert fake.holds(3) == "4321"
        shown = {"masked": "****", "withheld": ""}[display]
        assert get_usercode(node, 3)["usercode"] == shown


# ---------------------------------------------------------------------------
# 4. Driver errors on writes: capacity and duplicates
# ---------------------------------------------------------------------------


class TestWriteErrors:
    """Errors the driver raises, and the duplicate User Code CC cannot report."""

    async def test_a_user_beyond_the_cached_count_is_argument_invalid(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """``#assertValidUser`` (AC:1132, 1961-1973): code 322, as in #1401."""
        with pytest.raises(FailedZWaveCommand) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 31, PIN, "4321", credential_slot=31
            )

        assert err.value.zwave_error_code == ZWaveErrorCode.ARGUMENT_INVALID
        assert fake_zwave_lock.exchanges == []

    @pytest.mark.parametrize("parameters", [None, {"userId": 0}])
    async def test_a_duplicate_on_user_code_is_a_notification_and_unknown(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_schlage_be469: Node,
        parameters: dict[str, Any] | None,
    ) -> None:
        """
        A User Code lock refuses a duplicate out of band (#848).

        The lock sends Access Control notification 15, which Home Assistant
        fires as ``zwave_js_notification``; the driver, whose User Code path
        knows no duplicate result (AC:1160-1237), reads the slot back,
        finds it unchanged and answers Error_Unknown.
        """
        fake_zwave_lock.duplicate_parameters = parameters
        notifications = async_capture_events(hass, ZWAVE_JS_NOTIFICATION_EVENT)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 3, PIN, "1234", credential_slot=3
            )
        await hass.async_block_till_done()

        assert err.value.translation_key == "credential_rejected_unknown"
        assert fake_zwave_lock.holds(3) is None
        assert [
            (n.data["type"], n.data["event"], n.data["parameters"])
            for n in notifications
        ] == [
            (
                6,
                AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE,
                parameters or {},
            )
        ]


# ---------------------------------------------------------------------------
# 5. Capabilities that change over the node's life
# ---------------------------------------------------------------------------


class TestCapabilities:
    """The PIN slot count comes from the cached users count (AC:401-423)."""

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """An interview that completed while the lock slept (#1298, #1251)."""
        return {"interviewed": False}

    async def test_an_uncached_count_reads_zero_slots_until_queried(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        Zero slots until ``getUsersCount`` persists the count (UCC:502-518, 1675).

        The provider's #1298 recovery calls it through ``invoke_cc_api``.
        """
        caps = await lock_helpers.async_get_credential_capabilities(lock_schlage_be469)
        assert caps["supported_credential_types"]["pin_code"]["num_slots"] == 0

        count = await lock_schlage_be469.endpoints[0].async_invoke_cc_api(
            CommandClass.USER_CODE, "getUsersCount"
        )

        assert count == 30
        caps = await lock_helpers.async_get_credential_capabilities(lock_schlage_be469)
        assert caps["supported_credential_types"]["pin_code"]["num_slots"] == 30

    async def test_a_reinterview_caches_the_count_and_every_slot(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """Re-interviewing reads the count and a report per slot (value events)."""
        fake_zwave_lock.reinterview()

        caps = await lock_helpers.async_get_credential_capabilities(lock_schlage_be469)
        assert caps["max_users"] == 30
        assert get_usercode(lock_schlage_be469, 30)["in_use"] is False

    async def test_a_lock_with_fewer_slots_than_cached_answers_not_available(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A slot past the lock's capacity reports StatusNotAvailable (MOCK_UC respondToUserGet).

        ``persistUserCode`` removes both values, then both metadata (UCC:255-261).
        Each metadata removal still raises ``metadata updated``, carrying the
        default "any" the node falls back to (VDB:411-451, VAL:95-121), from
        which the client recreates the value, valueless. A later report of
        the slot creates metadata and adds the values again.
        """
        fake_zwave_lock.reinterview()
        fake_zwave_lock.capacity = 2
        seen = record(lock_schlage_be469)

        fake_zwave_lock.report(5)

        assert seen == [
            ("value removed", LOCK_USERCODE_STATUS_PROPERTY, 5, None),
            ("value removed", LOCK_USERCODE_PROPERTY, 5, None),
            ("metadata updated", LOCK_USERCODE_STATUS_PROPERTY, 5, "any"),
            ("metadata updated", LOCK_USERCODE_PROPERTY, 5, "any"),
        ]
        assert get_usercode(lock_schlage_be469, 5)["in_use"] is None
        seen.clear()

        fake_zwave_lock.capacity = 30
        fake_zwave_lock.program(5, "5555")

        assert seen == [
            ("metadata updated", LOCK_USERCODE_STATUS_PROPERTY, 5, "number"),
            ("metadata updated", LOCK_USERCODE_PROPERTY, 5, "string"),
            ("value added", LOCK_USERCODE_STATUS_PROPERTY, 5, UserIDStatus.ENABLED),
            ("value added", LOCK_USERCODE_PROPERTY, 5, "5555"),
        ]


# ---------------------------------------------------------------------------
# 6. A User Credential CC node
# ---------------------------------------------------------------------------


class TestUserCredentialNode:
    """A lock the driver dispatches to User Credential CC (AC:269-288)."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump (see ``user_credential_lock_state``)."""
        return user_credential_lock_state(lock_schlage_be469_state)

    async def test_capabilities_offer_named_users(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """User Credential capabilities come from its cached values (AC:294-399)."""
        assert fake_zwave_lock.uses_user_credential_cc
        caps = await lock_helpers.async_get_credential_capabilities(lock_schlage_be469)

        assert caps["max_users"] == 30
        assert caps["max_user_name_length"] == 16
        assert caps["supported_credential_types"]["pin_code"]["num_slots"] == 30

    async def test_a_user_then_a_credential_is_announced_and_cached(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        ``addUser`` then ``setCredential``: each report persisted, then handled (U3CH:27-108).

        No value event: every User Credential value is internal (VAL:186).
        """
        seen = record(lock_schlage_be469)

        user = await lock_helpers.async_set_user(
            lock_schlage_be469, user_name="lcm:3:x"
        )
        await lock_helpers.async_set_credential(
            lock_schlage_be469, user["user_id"], PIN, "4321", credential_slot=3
        )

        assert seen == [("user added", 1), ("credential added", 3, "4321")]
        ac = lock_schlage_be469.access_control
        assert [(u.user_id, u.user_name) for u in await ac.get_users_cached()] == [
            (1, "lcm:3:x")
        ]
        assert [
            (c.user_id, c.slot, c.data) for c in await ac.get_all_credentials_cached()
        ] == [(1, 3, "4321")]

    async def test_a_duplicate_is_rejected_in_band(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """DuplicateCredential (MOCK_U3C:1663-1683) maps to ``credential_rejected_duplicate``."""
        fake_zwave_lock.program(1, "1234")
        await lock_helpers.async_set_user(lock_schlage_be469, user_name="b")

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 2, PIN, "1234", credential_slot=2
            )

        assert err.value.translation_key == "credential_rejected_duplicate"

    async def test_an_empty_slot_read_is_cached_as_user_zero(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A Credential Get of an empty slot answers user 0, which the driver caches (AC:1729-1745).

        The length-zero report still names the slot (MOCK_U3C:1180-1196).
        """
        cred = await lock_schlage_be469.access_control.get_credential(PIN, 5)

        assert (cred.user_id, cred.slot, cred.data) == (0, 5, "")
        assert fake_zwave_lock.cached(131, "credentialOwner", (PIN << 16) | 5) == 0

    async def test_a_hashing_lock_sends_no_data_back(
        self, zwave_client, lock_schlage_be469: Node
    ) -> None:
        """A credential hash length above 0 means no read-back (MOCK_U3C:1062-1068)."""
        fake = FakeZWaveLock(
            zwave_client, lock_schlage_be469, max_credential_hash_length=8
        )
        seen = record(lock_schlage_be469)

        fake.program(3, "4321")

        assert seen == [("user added", 3), ("credential added", 3, None)]


class TestDualCommandClassNode:
    """A node advertising both: User Code CC wins while User Credential has 0 users."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, also advertising User Credential CC."""
        dual = user_credential_lock_state(lock_schlage_be469_state)
        dual["values"] = lock_schlage_be469_state["values"]
        dual["endpoints"][0]["commandClasses"] += [
            cc
            for cc in lock_schlage_be469_state["endpoints"][0]["commandClasses"]
            if cc["id"] == CommandClass.USER_CODE
        ]
        return dual

    @pytest.mark.parametrize(("max_users", "uses_u3c"), [(0, False), (30, True)])
    async def test_dispatch_follows_the_user_credential_users_count(
        self, zwave_client, lock_schlage_be469: Node, max_users: int, uses_u3c: bool
    ) -> None:
        """CL:0083.01.21.00.4 as the driver applies it (AC:276-287)."""
        fake = FakeZWaveLock(zwave_client, lock_schlage_be469, max_users=max_users)

        assert fake.uses_user_credential_cc is uses_u3c
        caps = await lock_helpers.async_get_credential_capabilities(lock_schlage_be469)
        assert caps["max_user_name_length"] == (16 if uses_u3c else 0)


# ---------------------------------------------------------------------------
# 7. Statistics, dead and alive
# ---------------------------------------------------------------------------


class TestNodeStatus:
    """What the node reports about its link and its life."""

    async def test_exchanges_move_the_counters_and_announce_them(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A sent frame counts in ``commandsTX`` and a report in ``commandsRX`` (DRV:4142, 6667-6672).

        The fake announces the counters after each command (STAT:29-43
        throttles them to one event per 250 ms).
        """
        await lock_schlage_be469.access_control.get_credential(PIN, 1)

        stats = lock_schlage_be469.statistics
        assert (stats.commands_tx, stats.commands_rx) == (1, 1)

        fake_zwave_lock.emit_statistics(rtt=42.0)
        assert lock_schlage_be469.statistics.rtt == 42.0

    async def test_dead_and_alive_set_the_status(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """The server forwards ``dead``/``alive`` (SRV_FWD:408-413); the client sets the status."""
        fake_zwave_lock.mark_dead()
        assert lock_schlage_be469.status is NodeStatus.DEAD

        fake_zwave_lock.mark_alive()
        assert lock_schlage_be469.status is NodeStatus.ALIVE


# ---------------------------------------------------------------------------
# 8. Fault knobs
# ---------------------------------------------------------------------------


class TestFaults:
    """The knobs hold, delay and expire the way ``LockFault`` does."""

    async def test_hang_holds_the_command_until_cleared(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        lock_schlage_be469: Node,
    ) -> None:
        """A held Set holds the server command, then goes on as usual."""
        fault = fake_zwave_lock.hang("set", 3)
        task = hass.async_create_task(
            lock_helpers.async_set_credential(
                lock_schlage_be469, 3, PIN, "4321", credential_slot=3
            )
        )
        await fault.async_wait_held()
        assert fake_zwave_lock.holds(3) is None

        fault.clear()
        await task

        assert fake_zwave_lock.holds(3) == "4321"

    async def test_slow_takes_its_time_on_the_clock(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        lock_schlage_be469: Node,
    ) -> None:
        """A slow Get answers once its seconds pass on the clock."""
        fault = fake_zwave_lock.slow("get", 1, seconds=5)
        task = hass.async_create_task(
            lock_schlage_be469.access_control.get_credential(PIN, 1)
        )
        await fault.async_wait_held()

        await async_advance_clock(hass, freezer, 6)

        assert (await task).data == "9999"

    async def test_times_limits_how_many_exchanges_a_fault_meets(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """``times`` spends the fault (as ``LockFault.times``)."""
        fault = fake_zwave_lock.drop_reply("get", 1, times=1)
        ac = lock_schlage_be469.access_control

        assert await ac.get_credential(PIN, 1) is None
        assert (await ac.get_credential(PIN, 1)).data == "9999"
        assert fault.hits == 1

    async def test_a_lost_set_leaves_get_faults_for_later_reads(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        A Set whose replies are lost decides its own read-back; a ``get`` fault waits.

        The verification read's outcome follows from the lost Set
        (AC:1186-1194), so it meets no fault meant for a later read.
        """
        fake_zwave_lock.drop_reply("set", 3)
        later = fake_zwave_lock.drop_reply("get", 3, times=1)

        with pytest.raises(HomeAssistantError):
            await lock_helpers.async_set_credential(
                lock_schlage_be469, 3, PIN, "4321", credential_slot=3
            )
        assert later.hits == 0

        assert await lock_schlage_be469.access_control.get_credential(PIN, 3) is None
        assert later.hits == 1

    async def test_clear_faults_releases_a_spent_fault_still_holding(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        lock_schlage_be469: Node,
    ) -> None:
        """
        A ``times=1`` hang is spent once it holds, and still released (as ``MockLCMLock``).

        Waits on the loop going quiet, not on clock steps: nothing here runs
        in an executor thread.
        """
        fault = fake_zwave_lock.hang("set", 3, times=1)
        task = hass.async_create_task(
            lock_helpers.async_set_credential(
                lock_schlage_be469, 3, PIN, "4321", credential_slot=3
            )
        )
        await fault.async_wait_held()
        assert fault not in fake_zwave_lock.faults

        fake_zwave_lock.clear_faults()
        await async_run_until_quiet()

        assert task.done()
        assert fake_zwave_lock.holds(3) == "4321"

    @pytest.mark.parametrize(
        ("knob", "operation"),
        [("ignore", "get"), ("refuse", "user"), ("ignore", None), ("refuse", None)],
    )
    async def test_a_fault_that_cannot_happen_is_refused(
        self, fake_zwave_lock: FakeZWaveLock, knob: str, operation: str | None
    ) -> None:
        """Only a Set that stores or empties a slot is ignored or refused (as ``LockFault``)."""
        with pytest.raises(ValueError, match="set or clear"):
            getattr(fake_zwave_lock, knob)(operation)

    async def test_an_unmodeled_command_fails_loudly(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """No canned answer for a command the fake does not model."""
        with pytest.raises(NotImplementedError):
            await lock_schlage_be469.access_control.get_admin_code()


# ---------------------------------------------------------------------------
# End to end: the provider sees each behavior as it would in production
# ---------------------------------------------------------------------------


async def _async_in_sync(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    slot: int,
) -> None:
    await async_tick_until(
        hass,
        freezer,
        lambda: (
            hass.states.get(
                in_sync_entity_id(hass, entry, slot, lock_entity.entity_id)
            ).state
            == STATE_ON
        ),
        max_ticks=8,
        what=f"slot {slot} in sync",
    )


class TestThroughTheProvider:
    """The behaviors above, as a loaded entry and its provider meet them."""

    async def test_a_keypad_change_reaches_the_code_sensor(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A report's value events reach the slot through the provider's report shim."""
        await _async_in_sync(hass, freezer, fake_zwave_lock_entry, lock_entity, 2)

        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()

        entity = lock_entity.entity_id
        assert (
            hass.states.get(
                in_sync_entity_id(hass, fake_zwave_lock_entry, 2, entity)
            ).state,
            hass.states.get(
                code_entity_id(hass, fake_zwave_lock_entry, 2, entity)
            ).state,
        ) == (STATE_OFF, "4321")

    async def test_an_unconfirmed_set_is_optimistic(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Error_Unknown is an optimistic write to the provider, not a rejection."""
        lock = get_zwave_lock(hass, fake_zwave_lock_entry, lock_entity)
        fake_zwave_lock.drop_reply("set", 2)

        assert await lock.async_set_usercode(2, "4321") is WriteResult.OPTIMISTIC
        assert fake_zwave_lock.holds(2) == "4321"

    async def test_a_slot_beyond_capacity_is_unsupported(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Argument_Invalid (322) is permanent to the provider (#1398, #1401)."""
        lock = get_zwave_lock(hass, fake_zwave_lock_entry, lock_entity)

        with pytest.raises(LockOperationUnsupported):
            await lock.async_set_usercode(31, "4321")

    async def test_an_unreachable_lock_is_disconnected(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Controller_CallbackNOK is transient to the provider."""
        lock = get_zwave_lock(hass, fake_zwave_lock_entry, lock_entity)
        fake_zwave_lock.fail("set", 2)

        with pytest.raises(LockDisconnected):
            await lock.async_set_usercode(2, "4321")

    async def test_link_health_and_status_come_from_the_node(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Statistics feed the #1397 link-health text; ``dead`` the availability check."""
        lock = get_zwave_lock(hass, fake_zwave_lock_entry, lock_entity)
        fake_zwave_lock.emit_statistics(
            commandsTX=10, commandsDroppedTX=3, timeoutResponse=4
        )

        assert "10 commands reached this lock, 3 could not be sent" in (
            lock.describe_link_health() or ""
        )
        assert "4 read requests went unanswered" in lock.describe_link_health()

        fake_zwave_lock.mark_dead()
        assert await lock.async_is_device_available() is False


class TestUserCredentialThroughTheProvider:
    """The provider's named-user path on a User Credential lock."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    async def test_the_entry_writes_tagged_users_and_their_pins(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A fresh lock is programmed: a tagged user, then its PIN, per slot."""
        for slot in (1, 2):
            await _async_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )

        assert fake_zwave_lock.codes == {1: "9999", 2: "1234"}
        assert sorted(u.user_name for u in fake_zwave_lock.users.values()) == [
            "lcm:1:slot1",
            "lcm:2:slot2",
        ]

    async def test_a_duplicate_raises_the_providers_duplicate_error(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """DuplicateCredential reaches the provider as ``DuplicateCodeError``."""
        await _async_in_sync(hass, freezer, fake_zwave_lock_entry, lock_entity, 2)
        lock = get_zwave_lock(hass, fake_zwave_lock_entry, lock_entity)
        fake_zwave_lock.program(7, "5555")

        with pytest.raises(DuplicateCodeError):
            await lock.async_set_usercode(2, "5555")
        assert fake_zwave_lock.holds(2) == "1234"

    async def test_a_new_user_whose_pin_is_refused_is_rolled_back(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The provider deletes the user it created when its PIN is refused.

        ``deleteUser`` answers User Deleted (MOCK_U3C:743-1047); the driver
        drops the user from its cache (U3CC:2314-2470) and raises ``user
        deleted`` (U3CH:27-54).
        """
        await _async_in_sync(hass, freezer, fake_zwave_lock_entry, lock_entity, 2)
        lock = get_zwave_lock(hass, fake_zwave_lock_entry, lock_entity)
        users_before = dict(fake_zwave_lock.users)
        seen = record(fake_zwave_lock.node)

        with pytest.raises(DuplicateCodeError):
            await lock.async_set_usercode(3, "1234")

        assert fake_zwave_lock.users == users_before
        assert [event[0] for event in seen] == ["user added", "user deleted"]


class TestFreshReads:
    """The walks a hard refresh runs, which replace what the cache holds."""

    async def test_a_user_code_walk_repairs_a_stale_cache(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock,
        lock_schlage_be469: Node,
    ) -> None:
        """
        ``getUsers``/``getAllCredentials`` query the count, then Get every slot (AC:491-557, 1853-1902).

        Each report is persisted, so a slot the lock changed silently is
        right afterwards, in the cache and in what the provider reads.
        """
        fake_zwave_lock.uc_slots[1] = (UserIDStatus.ENABLED, "1111")

        codes = await fake_e2e_lock.async_hard_refresh_codes()

        assert codes[1] == SlotCredential.known("1111")
        assert get_usercode(lock_schlage_be469, 1)["usercode"] == "1111"
        gets = [e.slot for e in fake_zwave_lock.exchanges if e.operation == "get"]
        assert gets == [*range(1, 31), *range(1, 31)]

    async def test_a_scoped_refresh_gets_only_its_slots(
        self, fake_zwave_lock: FakeZWaveLock, fake_e2e_lock
    ) -> None:
        """The provider's confirmation read: one Get per slot asked (AC:955-978)."""
        await fake_e2e_lock.async_hard_refresh_codes([2])

        assert [(e.operation, e.slot) for e in fake_zwave_lock.exchanges] == [
            ("get", 2)
        ]

    async def test_a_keypad_deletion_reports_the_slot_available(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """A code deleted at the keypad is reported Available with no code (UCC:1545-1551)."""
        fake_zwave_lock.program(2, None)

        assert get_usercode(lock_schlage_be469, 2)["in_use"] is False
        assert get_usercode(lock_schlage_be469, 2)["usercode"] == ""


class TestUserCredentialCache:
    """User Credential writes the driver chooses, and gets wrong, from its cache."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    @pytest.fixture
    async def user(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> int:
        """A user holding PIN 4321 at slot 3, cached."""
        user = await lock_helpers.async_set_user(lock_schlage_be469, user_name="a")
        await lock_helpers.async_set_credential(
            lock_schlage_be469, user["user_id"], PIN, "4321", credential_slot=3
        )
        return user["user_id"]

    async def test_renaming_modifies_and_an_identical_write_is_unchanged(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """
        Modify when cached, Unchanged when identical (AC:747-787, MOCK_U3C:977-1000).

        Unchanged raises no event (U3CH:27-108), and is still OK.
        """
        seen = record(lock_schlage_be469)

        await lock_helpers.async_set_user(
            lock_schlage_be469, user_id=user, user_name="b"
        )
        await lock_helpers.async_set_user(
            lock_schlage_be469, user_id=user, user_name="b"
        )
        await lock_helpers.async_set_credential(
            lock_schlage_be469, user, PIN, "4321", credential_slot=3
        )

        assert seen == [("user modified", user)]
        assert fake_zwave_lock.users[user].user_name == "b"

    async def test_a_delete_drops_the_cached_credential(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """Credential Deleted removes the cached entry (U3CC:2874-2925) and is announced."""
        seen = record(lock_schlage_be469)

        await lock_helpers.async_delete_credential(lock_schlage_be469, user, PIN, 3)

        assert seen == [("credential deleted", 3, None)]
        assert (
            await lock_schlage_be469.access_control.get_all_credentials_cached() == []
        )
        assert fake_zwave_lock.holds(3) is None

    async def test_an_add_to_a_slot_the_cache_missed_is_rejected_occupied(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """
        The lock holds a credential the cache does not (MOCK_U3C:1603-1620).

        Rejected occupied, read back: the driver caches the occupant and
        announces it (U3CH:94-106).
        """
        fake_zwave_lock.users[9] = fake_zwave_lock.users[user]
        fake_zwave_lock.credentials[(PIN, 4)] = (9, "7777")
        seen = record(lock_schlage_be469)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, user, PIN, "2468", credential_slot=4
            )

        assert err.value.translation_key == "credential_rejected_add_occupied"
        assert seen == [("credential added", 4, "7777")]

    async def test_a_modify_of_a_slot_emptied_behind_the_cache_is_rejected_empty(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """Modify of an emptied slot: rejected empty, cache dropped, deletion announced (U3CH:84-93)."""
        del fake_zwave_lock.credentials[(PIN, 3)]
        seen = record(lock_schlage_be469)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, user, PIN, "2468", credential_slot=3
            )

        assert err.value.translation_key == "credential_rejected_modify_empty"
        assert seen == [("credential deleted", 3, None)]

    async def test_a_walk_purges_what_the_lock_no_longer_holds(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """The credential walk purges stale cache entries (AC:1747-1851); no events."""
        del fake_zwave_lock.credentials[(PIN, 3)]
        fake_zwave_lock.credentials[(PIN, 5)] = (user, "1357")
        seen = record(lock_schlage_be469)
        ac = lock_schlage_be469.access_control

        users = await ac.get_users()
        credentials = await ac.get_all_credentials()

        assert [u.user_id for u in users] == [user]
        assert [(c.slot, c.data) for c in credentials] == [(5, "1357")]
        assert [(c.slot, c.data) for c in await ac.get_all_credentials_cached()] == [
            (5, "1357")
        ]
        assert seen == []

    async def test_a_keypad_deletion_is_announced(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """An unsolicited Credential Deleted report raises the event (U3CH:84-93)."""
        seen = record(lock_schlage_be469)

        fake_zwave_lock.program(3, None)

        assert seen == [("credential deleted", 3, None)]

    async def test_clear_faults_releases_everything(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """``clear_faults`` clears every fault on the lock."""
        fake_zwave_lock.refuse("clear", 3)
        fake_zwave_lock.drop_reply("get")

        fake_zwave_lock.clear_faults()

        assert fake_zwave_lock.faults == []
        await lock_helpers.async_delete_credential(lock_schlage_be469, user, PIN, 3)
        assert fake_zwave_lock.holds(3) is None

    async def test_a_refused_pin_is_the_providers_rejection(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock,
    ) -> None:
        """
        A lock refusing a PIN by its own rules answers Manufacturer Security Rules.

        The driver maps it to its own result (AC:174-175), Home Assistant to
        ``credential_rejected_manufacturer_rules`` (HA_LH:114-116), and the
        provider to ``CodeRejectedError``.
        """
        fake_zwave_lock.refuse("set", 4)

        with pytest.raises(CodeRejectedError):
            await fake_e2e_lock.async_set_usercode(4, "2468")

        assert fake_zwave_lock.holds(4) is None

    async def test_a_delete_only_removes_a_credential_its_user_owns(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """
        A non-owner's Delete leaves the code, yet reports it deleted (MOCK_U3C:202-214, 1515-1530).

        The driver believes the report: it drops its cached entry
        (U3CC:2874-2925) and announces the deletion, so its cache and the
        lock part ways.
        """
        other = await lock_helpers.async_set_user(lock_schlage_be469, user_name="b")
        seen = record(lock_schlage_be469)

        await lock_helpers.async_delete_credential(
            lock_schlage_be469, other["user_id"], PIN, 3
        )

        assert fake_zwave_lock.holds(3) == "4321"
        assert seen == [("credential deleted", 3, None)]
        assert (
            await lock_schlage_be469.access_control.get_all_credentials_cached() == []
        )

    async def test_a_delete_for_user_zero_is_ignored(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """
        (user 0, type, slot) is no valid Delete; the lock ignores it (MOCK_U3C:1510-1513).

        No report, so the driver answers Error_Unknown (AC:158-161).
        """
        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_delete_credential(lock_schlage_be469, 0, PIN, 3)

        assert err.value.translation_key == "credential_rejected_unknown"
        assert fake_zwave_lock.holds(3) == "4321"
        assert (
            len(await lock_schlage_be469.access_control.get_all_credentials_cached())
            == 1
        )

    async def test_the_walk_follows_the_cursor_and_stops_where_a_report_is_lost(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """
        Get (0, None, 0), then each report's next pointer (AC:1747-1851).

        The range between two credentials is purged as the walk passes it;
        a lost report ends the walk there, purging nothing after it
        (AC:1784-1786).
        """
        for slot in (4, 7):
            # Cached from the lock's own report, then gone without one.
            fake_zwave_lock.program(slot, f"{slot}{slot}{slot}{slot}")
            del fake_zwave_lock.credentials[(PIN, slot)]
        fake_zwave_lock.credentials[(PIN, 5)] = (user, "1357")
        fake_zwave_lock.drop_reply("get", 5)
        ac = lock_schlage_be469.access_control

        credentials = await ac.get_all_credentials()

        assert [(c.slot, c.data) for c in credentials] == [(3, "4321")]
        gets = [e.slot for e in fake_zwave_lock.exchanges if e.operation == "get"]
        assert gets == [0, 5]
        assert [c.slot for c in await ac.get_all_credentials_cached()] == [3, 7]

    async def test_a_lost_report_still_leaves_the_lock_its_own_checks(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node, user: int
    ) -> None:
        """
        Only the report is lost: the lock refused the duplicate all the same.

        So the driver answers Error_Unknown (AC:158-161) and the lock does
        not hold the code (MOCK_U3C:1663-1683).
        """
        other = await lock_helpers.async_set_user(lock_schlage_be469, user_name="b")
        fake_zwave_lock.drop_reply("set", 4)

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.async_set_credential(
                lock_schlage_be469, other["user_id"], PIN, "4321", credential_slot=4
            )

        assert err.value.translation_key == "credential_rejected_unknown"
        assert fake_zwave_lock.holds(4) is None

    async def test_a_bundled_credential_is_checked_after_the_user_is_added(
        self, fake_zwave_lock: FakeZWaveLock, lock_schlage_be469: Node
    ) -> None:
        """
        ``addUser`` adds the user, then ``setCredential`` checks the slot (AC:649-654, 1132-1135).

        So a slot out of range raises Argument_Invalid with the user already
        on the lock.
        """
        with pytest.raises(FailedZWaveCommand) as err:
            await lock_helpers.async_set_user(
                lock_schlage_be469,
                user_name="a",
                credential_type=PIN,
                credential_data="4321",
                credential_slot=31,
            )

        assert err.value.zwave_error_code == ZWaveErrorCode.ARGUMENT_INVALID
        assert list(fake_zwave_lock.users) == [1]
        assert fake_zwave_lock.credentials == {}

    @pytest.mark.parametrize(
        ("user_id", "options"),
        [
            pytest.param(31, SetUserOptions(user_name="a"), id="past_the_count"),
            pytest.param(
                2,
                SetUserOptions(user_type=UserCredentialUserType.NON_ACCESS),
                id="unadvertised_type",
            ),
        ],
    )
    async def test_a_user_set_the_lock_ignores_is_unknown(
        self,
        fake_zwave_lock: FakeZWaveLock,
        lock_schlage_be469: Node,
        user_id: int,
        options: SetUserOptions,
    ) -> None:
        """
        The lock ignores a User Set past its count or of an unadvertised type.

        CC:0083.01.05.11.014/.030 (MOCK_U3C:821-843); no report is
        Error_Unknown (AC:139-142).
        """
        result = await lock_schlage_be469.access_control.set_user(user_id, options)

        assert result is SetUserResult.ERROR_UNKNOWN
        assert user_id not in fake_zwave_lock.users
