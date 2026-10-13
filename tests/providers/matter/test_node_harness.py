"""
Self-tests for the stateful Matter lock fake (``fake_node.py``).

Each test checks one behavior the fake models against the source it cites,
driven the way the integration drives the lock: through the client's
``send_device_command`` and Home Assistant's ``lock_helpers``, which run for
real above the fake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

from freezegun.api import FrozenDateTimeFactory
from matter_server.common.errors import NodeNotResolving, UnknownError
from matter_server.common.models import EventType
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.components.matter import lock_helpers
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import BUS_EVENT_CREDENTIAL_USED
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_run_until_quiet,
    code_entity_id,
    in_sync_entity_id,
)

from .conftest import FAKE_MATTER_SLOTS
from .fake_node import (
    CLEAR_ALL,
    HA_FABRIC,
    DlStatus,
    DoorLock,
    FakeMatterLock,
    ImStatus,
    LockUser,
)

PIN = DoorLock.Enums.CredentialTypeEnum.kPin
ADD = DoorLock.Enums.DataOperationTypeEnum.kAdd
MODIFY = DoorLock.Enums.DataOperationTypeEnum.kModify
CLEAR = DoorLock.Enums.DataOperationTypeEnum.kClear


async def _command(lock: FakeMatterLock, command: Any) -> Any:
    """Send one Door Lock command the way ``lock_helpers`` sends it."""
    return await lock.client.send_device_command(
        node_id=lock.node.node_id, endpoint_id=lock.endpoint_id, command=command
    )


async def _record_events(lock: FakeMatterLock) -> list[tuple[int, dict[str, Any]]]:
    """
    Subscribe to the node's events as the provider does; return what arrives.

    Events from what the test did before are delivered first, unrecorded.
    """
    await async_run_until_quiet()
    seen: list[tuple[int, dict[str, Any]]] = []

    def _on_event(_event: EventType, node_event: Any) -> None:
        seen.append((node_event.event_id, node_event.data))

    lock.client.subscribe_events(
        callback=_on_event,
        event_filter=EventType.NODE_EVENT,
        node_filter=lock.node.node_id,
    )
    return seen


def _set_user(index: int, name: str | None, operation: int = ADD) -> Any:
    return DoorLock.Commands.SetUser(
        operationType=operation,
        userIndex=index,
        userName=name,
        userUniqueID=None,
        userStatus=None,
        userType=None,
        credentialRule=None,
    )


def _set_credential(
    index: int, pin: str, user: int | None, operation: int = ADD
) -> Any:
    return DoorLock.Commands.SetCredential(
        operationType=operation,
        credential=DoorLock.Structs.CredentialStruct(
            credentialType=PIN, credentialIndex=index
        ),
        credentialData=pin.encode(),
        userIndex=user,
        userStatus=None,
        userType=None,
    )


def _clear_credential(index: int) -> Any:
    return DoorLock.Commands.ClearCredential(
        credential=DoorLock.Structs.CredentialStruct(
            credentialType=PIN, credentialIndex=index
        )
    )


def _im(status: ImStatus) -> str:
    """Match a matter.js status error's code (MJS_SRE:76-85)."""
    return rf"\(code {int(status)}[;)]"


# ---------------------------------------------------------------------------
# 1. The lock as its node describes it
# ---------------------------------------------------------------------------


class TestCapacities:
    """What the fake can hold is what the node's Door Lock attributes report."""

    async def test_the_node_s_attributes_set_the_lock_s_capacities(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """The Secuyou dump: ten users, ten PINs, three per user, six to eight digits."""
        assert fake_matter_lock.supports_users
        assert fake_matter_lock.max_users == 10
        assert fake_matter_lock.pin_slots == 10
        assert fake_matter_lock.per_user == 3
        assert fake_matter_lock.pin_lengths == (6, 8)

    async def test_lock_info_reads_the_same_attributes(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """Home Assistant's capability read sees what the fake enforces (HA_LH:278-336)."""
        info = await lock_helpers.get_lock_info(
            fake_matter_lock.client, fake_matter_lock.node
        )

        assert info["supports_user_management"] is True
        assert info["supported_credential_types"] == ["pin"]
        assert (info["max_users"], info["min_pin_length"], info["max_pin_length"]) == (
            10,
            6,
            8,
        )


# ---------------------------------------------------------------------------
# 2. Users
# ---------------------------------------------------------------------------


class TestUsers:
    """GetUser, SetUser and ClearUser, as the SDK's door-lock server answers them."""

    async def test_an_empty_index_has_no_user_fields_and_names_the_next_user(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:532-536 and 555-582: only the index and the next occupied one."""
        fake_matter_lock.users[4] = LockUser(name="Mom")

        response = await _command(
            fake_matter_lock, DoorLock.Commands.GetUser(userIndex=2)
        )

        assert response["userIndex"] == 2
        assert response["userStatus"] is None
        assert response["credentials"] is None
        assert response["nextUserIndex"] == 4

    async def test_an_index_past_the_lock_s_users_is_invalid(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:523-529: InvalidCommand, as the client raises it."""
        with pytest.raises(UnknownError, match=_im(ImStatus.INVALID_COMMAND)):
            await _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=11))

    async def test_adding_a_user_stores_it_and_announces_it(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:1948-2019: the record, this fabric's, and a remote LockUserChange."""
        seen = await _record_events(fake_matter_lock)

        result = await lock_helpers.set_lock_user(
            fake_matter_lock.client, fake_matter_lock.node, user_name="Mom"
        )
        await async_run_until_quiet()

        assert result == {"user_index": 1}
        assert fake_matter_lock.users[1].name == "Mom"
        assert fake_matter_lock.users[1].created_by == HA_FABRIC
        assert seen == [
            (
                DoorLock.Events.LockUserChange.event_id,
                {
                    "lockDataType": DoorLock.Enums.LockDataTypeEnum.kUserIndex,
                    "dataOperationType": ADD,
                    "operationSource": DoorLock.Enums.OperationSourceEnum.kRemote,
                    "userIndex": 1,
                    "fabricIndex": HA_FABRIC,
                    "sourceNode": seen[0][1]["sourceNode"],
                    "dataIndex": 1,
                },
            )
        ]
        assert seen[0][1]["sourceNode"]

    async def test_adding_to_an_occupied_index_fails(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:1964-1967: a Failure carrying the cluster-specific Occupied."""
        fake_matter_lock.users[1] = LockUser(name="Mom")

        with pytest.raises(UnknownError, match=r"^Failure \(code 1; cluster code 3\)$"):
            await _command(fake_matter_lock, _set_user(1, "Dad"))
        assert fake_matter_lock.users[1].name == "Mom"

    @pytest.mark.parametrize(
        ("fake_matter_lock_options", "name", "accepted"),
        [
            ({}, "x" * 10, True),
            ({}, "x" * 11, False),
            ({"max_user_name": 16}, "x" * 11, True),
        ],
        ids=["ten", "eleven", "longer_build"],
    )
    async def test_a_name_longer_than_the_lock_stores_is_invalid(
        self, fake_matter_lock: FakeMatterLock, name: str, accepted: bool
    ) -> None:
        """DLS:454-462: ten characters by the SDK's default; a vendor may build longer."""
        if accepted:
            await _command(fake_matter_lock, _set_user(1, name))
            assert fake_matter_lock.users[1].name == name
            return
        with pytest.raises(UnknownError, match=_im(ImStatus.INVALID_COMMAND)):
            await _command(fake_matter_lock, _set_user(1, name))
        assert fake_matter_lock.users == {}

    async def test_modifying_keeps_the_fields_left_null(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2063-2068: a null field keeps the record's value."""
        fake_matter_lock.users[1] = LockUser(name="Mom", unique_id=7)

        await _command(fake_matter_lock, _set_user(1, "Mum", MODIFY))

        assert fake_matter_lock.users[1].name == "Mum"
        assert fake_matter_lock.users[1].unique_id == 7

    async def test_modifying_an_empty_index_is_invalid(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2041-2045."""
        with pytest.raises(UnknownError, match=_im(ImStatus.INVALID_COMMAND)):
            await _command(fake_matter_lock, _set_user(1, "Mom", MODIFY))

    async def test_another_fabric_s_user_cannot_be_renamed(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2047-2061: only the creating fabric may set the name."""
        fake_matter_lock.program(1, "111111", name="Mom")

        with pytest.raises(UnknownError, match=_im(ImStatus.INVALID_COMMAND)):
            await _command(fake_matter_lock, _set_user(1, "lcm1", MODIFY))
        assert fake_matter_lock.users[1].name == "Mom"

    async def test_clearing_a_user_clears_its_credentials(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2105-2151: the user, its credentials, and a remote event."""
        await _command(fake_matter_lock, _set_credential(3, "333333", None))
        seen = await _record_events(fake_matter_lock)

        await lock_helpers.clear_lock_user(
            fake_matter_lock.client, fake_matter_lock.node, 1
        )
        await async_run_until_quiet()

        assert fake_matter_lock.users == {}
        assert fake_matter_lock.codes == {}
        assert [
            (data["lockDataType"], data["dataOperationType"]) for _, data in seen
        ] == [(DoorLock.Enums.LockDataTypeEnum.kUserIndex, CLEAR)]

    async def test_clearing_every_user_is_one_event(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:637-660: ClearUser 0xFFFE empties the lock and logs one change."""
        fake_matter_lock.program(1, "111111")
        fake_matter_lock.program(2, "222222")
        seen = await _record_events(fake_matter_lock)

        await _command(
            fake_matter_lock, DoorLock.Commands.ClearUser(userIndex=CLEAR_ALL)
        )
        await async_run_until_quiet()

        assert fake_matter_lock.users == {}
        assert [data["userIndex"] for _, data in seen] == [CLEAR_ALL]

    async def test_a_lock_without_user_management_refuses_user_commands(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:518-523: UnsupportedCommand without the USR feature."""
        fake_matter_lock.feature_map = DoorLock.Bitmaps.Feature.kPinCredential

        with pytest.raises(UnknownError, match=_im(ImStatus.UNSUPPORTED_COMMAND)):
            await _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=1))


# ---------------------------------------------------------------------------
# 3. Credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    """SetCredential, GetCredentialStatus and ClearCredential."""

    async def test_a_pin_with_no_user_creates_one(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2200-2247: the first free user index, named in the response."""
        fake_matter_lock.users[1] = LockUser()

        result = await lock_helpers.set_lock_credential(
            fake_matter_lock.client,
            fake_matter_lock.node,
            credential_type="pin",
            credential_data="123456",
            credential_index=5,
        )

        assert result == {
            "credential_index": 5,
            "user_index": 2,
            "next_credential_index": 6,
        }
        assert fake_matter_lock.owner(5) == 2
        assert fake_matter_lock.holds(5) == "123456"

    async def test_a_pin_for_a_user_joins_it(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2249-2316: added to the user's credentials; no user index answered."""
        await _command(fake_matter_lock, _set_user(4, "Mom"))

        response = await _command(fake_matter_lock, _set_credential(2, "123456", 4))

        assert response["status"] == DlStatus.kSuccess
        assert response["userIndex"] is None
        assert fake_matter_lock.users[4].credentials == [(PIN, 2)]

    async def test_a_pin_another_index_holds_is_a_duplicate(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:712-740: Duplicate in band, which Home Assistant raises (HA_LH:765-771)."""
        fake_matter_lock.program(1, "123456")

        with pytest.raises(HomeAssistantError) as err:
            await lock_helpers.set_lock_credential(
                fake_matter_lock.client,
                fake_matter_lock.node,
                credential_type="pin",
                credential_data="123456",
                credential_index=2,
            )

        assert err.value.translation_placeholders == {"status": "duplicate"}
        assert fake_matter_lock.holds(2) is None

    async def test_the_same_pin_at_its_own_index_is_not_a_duplicate(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:722-727: the index being set is skipped."""
        await _command(fake_matter_lock, _set_credential(1, "123456", None))

        response = await _command(
            fake_matter_lock, _set_credential(1, "123456", 1, MODIFY)
        )

        assert response["status"] == DlStatus.kSuccess

    @pytest.mark.parametrize(
        ("setup", "command", "status"),
        [
            (
                lambda lock: lock.program(1, "111111"),
                _set_credential(1, "123456", None),
                DlStatus.kOccupied,
            ),
            (
                lambda lock: None,
                _set_credential(11, "123456", None),
                DlStatus.kInvalidField,
            ),
            (
                lambda lock: None,
                _set_credential(1, "12345", None),
                DlStatus.kInvalidField,
            ),
            (
                lambda lock: None,
                _set_credential(1, "123456", 3),
                DlStatus.kInvalidField,
            ),
            (
                lambda lock: None,
                _set_credential(1, "123456", None, MODIFY),
                DlStatus.kInvalidField,
            ),
            (
                lambda lock: lock.program(1, "111111"),
                _set_credential(1, "123456", 1, MODIFY),
                DlStatus.kInvalidField,
            ),
        ],
        ids=[
            "add_over_occupied",
            "index_out_of_range",
            "too_short",
            "user_that_does_not_exist",
            "modify_empty",
            "modify_another_fabric_s",
        ],
    )
    async def test_refusals_are_answered_in_band(
        self,
        fake_matter_lock: FakeMatterLock,
        setup: Callable[[FakeMatterLock], None],
        command: Any,
        status: int,
    ) -> None:
        """DLS:692-709, 793-818, 2249-2316, 2489-2494: the status, and nothing changed."""
        setup(fake_matter_lock)
        before = dict(fake_matter_lock.codes)

        response = await _command(fake_matter_lock, command)

        assert response["status"] == status
        assert fake_matter_lock.codes == before

    async def test_a_user_holds_no_more_than_the_lock_allows(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:2346-2350: ResourceExhausted past NumberOfCredentialsSupportedPerUser."""
        await _command(fake_matter_lock, _set_user(1, "Mom"))
        for index in (1, 2, 3):
            await _command(fake_matter_lock, _set_credential(index, f"{index}" * 6, 1))

        response = await _command(fake_matter_lock, _set_credential(4, "444444", 1))

        assert response["status"] == DlStatus.kResourceExhausted
        assert fake_matter_lock.holds(4) is None

    async def test_status_names_the_owner_and_the_next_occupied_index(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:858-990: who holds it, and the next index in use after it."""
        await _command(fake_matter_lock, _set_credential(2, "222222", None))
        fake_matter_lock.program(5, "555555", credential_index=7)

        result = await lock_helpers.get_lock_credential_status(
            fake_matter_lock.client,
            fake_matter_lock.node,
            credential_type="pin",
            credential_index=2,
        )
        theirs = await lock_helpers.get_lock_credential_status(
            fake_matter_lock.client,
            fake_matter_lock.node,
            credential_type="pin",
            credential_index=7,
        )

        assert result == {
            "credential_exists": True,
            "user_index": 1,
            "creator_fabric_index": HA_FABRIC,
            "last_modified_fabric_index": HA_FABRIC,
            "next_credential_index": 7,
        }
        assert theirs["user_index"] == 5
        assert theirs["creator_fabric_index"] is None

    async def test_clearing_a_user_s_only_pin_clears_the_user(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:3043-3053: logged as the user's removal."""
        await _command(fake_matter_lock, _set_credential(2, "222222", None))
        seen = await _record_events(fake_matter_lock)

        await lock_helpers.clear_lock_credential(
            fake_matter_lock.client,
            fake_matter_lock.node,
            credential_type="pin",
            credential_index=2,
        )
        await async_run_until_quiet()

        assert fake_matter_lock.users == {}
        assert [(data["lockDataType"], data["userIndex"]) for _, data in seen] == [
            (DoorLock.Enums.LockDataTypeEnum.kUserIndex, 1)
        ]

    async def test_clearing_one_of_several_pins_keeps_the_user(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:3055-3135: the credential leaves the user, and is logged as itself."""
        await _command(fake_matter_lock, _set_user(1, "Mom"))
        await _command(fake_matter_lock, _set_credential(1, "111111", 1))
        await _command(fake_matter_lock, _set_credential(2, "222222", 1))
        seen = await _record_events(fake_matter_lock)

        await _command(fake_matter_lock, _clear_credential(2))
        await async_run_until_quiet()

        assert fake_matter_lock.users[1].credentials == [(PIN, 1)]
        assert [(data["lockDataType"], data["dataIndex"]) for _, data in seen] == [
            (DoorLock.Enums.LockDataTypeEnum.kPin, 2)
        ]

    @pytest.mark.parametrize("index", [0, 11], ids=["zero", "past_the_lock"])
    async def test_clearing_an_index_out_of_range_is_invalid(
        self, fake_matter_lock: FakeMatterLock, index: int
    ) -> None:
        """DLS:2996-3002."""
        with pytest.raises(UnknownError, match=_im(ImStatus.INVALID_COMMAND)):
            await _command(fake_matter_lock, _clear_credential(index))

    async def test_clearing_every_credential_is_not_modeled(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """No silent success for the clear-all form (DLS:1032-1036)."""
        with pytest.raises(NotImplementedError):
            await _command(fake_matter_lock, _clear_credential(CLEAR_ALL))

    async def test_a_lock_without_pin_credentials_refuses_them(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:678-685: the type must be supported; user management is not asked."""
        fake_matter_lock.feature_map = DoorLock.Bitmaps.Feature.kUser

        with pytest.raises(UnknownError, match=_im(ImStatus.UNSUPPORTED_COMMAND)):
            await _command(fake_matter_lock, _set_credential(1, "111111", None))

    async def test_clearing_an_empty_index_succeeds(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """DLS:3005-3021: nothing to clear is not an error, and nothing is logged."""
        seen = await _record_events(fake_matter_lock)

        await _command(fake_matter_lock, _clear_credential(3))

        assert seen == []


# ---------------------------------------------------------------------------
# 4. The lock acting on its own
# ---------------------------------------------------------------------------


class TestLocalChanges:
    """Changes at the keypad or from another admin, and uses of a code."""

    async def test_a_keypad_change_is_logged_with_no_fabric(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """A local change names no fabric or node, and the record no fabric indices."""
        seen = await _record_events(fake_matter_lock)

        fake_matter_lock.program(3, "333333", name="Mom")
        await async_run_until_quiet()

        assert fake_matter_lock.users[3].created_by is None
        [(_, data)] = seen
        assert data["operationSource"] == DoorLock.Enums.OperationSourceEnum.kKeypad
        assert data["fabricIndex"] is None
        assert data["sourceNode"] is None
        assert (data["lockDataType"], data["dataIndex"]) == (
            DoorLock.Enums.LockDataTypeEnum.kPin,
            3,
        )

    async def test_a_keypad_unlock_names_the_user_and_the_pin(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """LockOperation: unlocked from the keypad, by the user holding the PIN."""
        fake_matter_lock.program(4, "444444", credential_index=6)
        seen = await _record_events(fake_matter_lock)

        fake_matter_lock.keypad_unlock(6)
        await async_run_until_quiet()

        assert seen == [
            (
                DoorLock.Events.LockOperation.event_id,
                {
                    "lockOperationType": DoorLock.Enums.LockOperationTypeEnum.kUnlock,
                    "operationSource": DoorLock.Enums.OperationSourceEnum.kKeypad,
                    "userIndex": 4,
                    "fabricIndex": None,
                    "sourceNode": None,
                    "credentials": [{"credentialType": PIN, "credentialIndex": 6}],
                },
            )
        ]

    async def test_a_pin_moved_at_the_keypad_leaves_its_old_user(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """One credential, one holder: the move is a modify of an existing PIN."""
        fake_matter_lock.program(1, "111111")
        await async_run_until_quiet()
        seen = await _record_events(fake_matter_lock)

        fake_matter_lock.program(2, "222222", credential_index=1)
        await async_run_until_quiet()

        assert fake_matter_lock.users[1].credentials == []
        assert fake_matter_lock.owner(1) == 2
        [(_, data)] = seen
        assert data["dataOperationType"] == MODIFY

    async def test_a_keypad_change_to_home_assistant_s_user_keeps_its_creator(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """The record stays Home Assistant's; only the last modifier changes."""
        await _command(fake_matter_lock, _set_user(1, "Mom"))

        fake_matter_lock.program(1, "111111", name="Mum")

        user = fake_matter_lock.users[1]
        assert (user.name, user.created_by, user.modified_by) == (
            "Mum",
            HA_FABRIC,
            None,
        )

    async def test_the_response_arrives_before_the_event(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """The event is a separate message from the server, after the answer."""
        seen = await _record_events(fake_matter_lock)

        await _command(fake_matter_lock, _set_user(1, "Mom"))
        assert seen == []

        await async_run_until_quiet()
        assert len(seen) == 1

    async def test_events_reach_only_subscribers_of_the_node(
        self, fake_matter_lock: FakeMatterLock, matter_client: MagicMock
    ) -> None:
        """MS_CLIENT:960-979: delivery follows the client's own filters."""
        other: list[Any] = []
        matter_client.subscribe_events(
            callback=lambda *args: other.append(args),
            event_filter=EventType.NODE_EVENT,
            node_filter=fake_matter_lock.node.node_id + 1,
        )
        mine = await _record_events(fake_matter_lock)

        fake_matter_lock.program(1, "111111")
        await async_run_until_quiet()

        assert other == []
        assert len(mine) == 1


# ---------------------------------------------------------------------------
# 5. Faults
# ---------------------------------------------------------------------------


class TestFaults:
    """Faults act on the lock's commands, named and shaped as the other fakes'."""

    async def test_fail_raises_what_the_server_raises_for_an_unreachable_node(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """Error code 4, with the server's message (MJS_TYPES:118-119)."""
        fake_matter_lock.fail("GetUser")

        with pytest.raises(NodeNotResolving, match=r"^Node 42 is not resolving$"):
            await _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=1))

    async def test_refuse_answers_a_pin_in_band_and_a_user_by_status(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """A refused SetCredential is a status; a refused SetUser an Interaction Model error."""
        fake_matter_lock.refuse("SetCredential", status=DlStatus.kOccupied)
        fake_matter_lock.refuse("SetUser")

        response = await _command(fake_matter_lock, _set_credential(1, "111111", None))
        with pytest.raises(UnknownError, match=_im(ImStatus.FAILURE)):
            await _command(fake_matter_lock, _set_user(1, "Mom"))

        assert response["status"] == DlStatus.kOccupied
        assert fake_matter_lock.users == {}

    async def test_other_commands_still_get_the_mock_s_answer(
        self, fake_matter_lock: FakeMatterLock, matter_client: MagicMock
    ) -> None:
        """Anything not this node's Door Lock passes through to the mock as before."""
        matter_client.send_command.return_value = {"answered": True}

        assert await matter_client.send_command("server_info") == {"answered": True}

    async def test_ignore_answers_success_and_changes_nothing(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """An acknowledged write that never lands."""
        fake_matter_lock.ignore("SetCredential", 1)

        response = await _command(fake_matter_lock, _set_credential(1, "111111", None))

        assert response["status"] == DlStatus.kSuccess
        assert fake_matter_lock.codes == {}

    async def test_hang_holds_the_command_until_cleared(
        self, hass: HomeAssistant, fake_matter_lock: FakeMatterLock
    ) -> None:
        """The command waits for the fault, then goes on as the lock would."""
        fault = fake_matter_lock.hang("GetUser")
        reading = hass.async_create_task(
            _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=1))
        )

        await fault.async_wait_held()
        fault.clear()

        assert (await reading)["userIndex"] == 1

    async def test_slow_takes_its_time_on_the_clock(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
    ) -> None:
        """A slow command answers once the clock has moved that far."""
        fake_matter_lock.slow("GetUser", seconds=30)
        reading = hass.async_create_task(
            _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=1))
        )
        await async_advance_clock(hass, freezer, 29, settle=False)
        assert not reading.done()

        await async_advance_clock(hass, freezer, 2, settle=False)

        assert reading.done()

    async def test_times_and_index_limit_what_a_fault_meets(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """``times`` spends the fault; ``index`` narrows it to one user or credential."""
        fault = fake_matter_lock.fail("GetUser", 2, times=1)

        await _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=1))
        with pytest.raises(NodeNotResolving):
            await _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=2))
        await _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=2))

        assert fault.hits == 1

    async def test_an_identical_command_in_flight_reaches_the_lock_once(
        self, hass: HomeAssistant, fake_matter_lock: FakeMatterLock
    ) -> None:
        """MJS_CMD:1303-1340: the second waits for the first's answer."""
        fault = fake_matter_lock.hang("GetUser")
        first, second = (
            hass.async_create_task(
                _command(fake_matter_lock, DoorLock.Commands.GetUser(userIndex=1))
            )
            for _ in range(2)
        )
        await fault.async_wait_held()
        fault.clear()

        assert await first == await second
        assert len(fake_matter_lock.sent("GetUser")) == 1

    async def test_an_index_matter_js_has_no_name_for_is_an_unknown_status(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """MJS_SRE:50-56: no name, no code in the message."""
        fake_matter_lock.refuse("ClearUser", status=0x87)

        with pytest.raises(UnknownError, match=r"^Unknown status response$"):
            await _command(fake_matter_lock, DoorLock.Commands.ClearUser(userIndex=1))

    async def test_a_fault_that_cannot_happen_is_refused(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """Only a write can be ignored or refused, as the other fakes rule."""
        with pytest.raises(ValueError, match="writes"):
            fake_matter_lock.ignore("GetUser")

    async def test_a_command_the_fake_does_not_model_fails_loudly(
        self, fake_matter_lock: FakeMatterLock
    ) -> None:
        """No silent canned answer for a command nobody traced."""
        with pytest.raises(NotImplementedError, match="LockDoor"):
            await _command(fake_matter_lock, DoorLock.Commands.LockDoor())


# ---------------------------------------------------------------------------
# 6. Through a loaded entry
# ---------------------------------------------------------------------------


class TestThroughTheProvider:
    """A Lock Code Manager entry on the fake, end to end."""

    async def test_the_entry_writes_its_users_and_their_pins(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """Each slot's PIN lands under a user the provider tags, and reads in sync."""
        await async_advance_sync_ticks(hass, freezer, 6)

        assert sorted(fake_matter_lock.codes.values()) == sorted(
            config["pin"] for config in FAKE_MATTER_SLOTS.values()
        )
        for slot, config in FAKE_MATTER_SLOTS.items():
            assert (
                hass.states.get(
                    in_sync_entity_id(
                        hass,
                        fake_matter_lock_entry,
                        slot,
                        fake_matter_lock_entity.entity_id,
                    )
                ).state
                == STATE_ON
            )
            assert (
                hass.states.get(
                    code_entity_id(
                        hass,
                        fake_matter_lock_entry,
                        slot,
                        fake_matter_lock_entity.entity_id,
                    )
                ).state
                == config["pin"]
            )

    async def test_a_keypad_use_is_credited_to_its_user(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
    ) -> None:
        """A LockOperation reaches Lock Code Manager as a use of that slot's code."""
        await async_advance_sync_ticks(hass, freezer, 6)
        used = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
        index = next(
            i
            for i, pin in fake_matter_lock.codes.items()
            if pin == FAKE_MATTER_SLOTS[2]["pin"]
        )

        fake_matter_lock.keypad_unlock(index)
        await async_advance_sync_ticks(hass, freezer)

        assert [event.data["name"] for event in used] == ["slot2"]
