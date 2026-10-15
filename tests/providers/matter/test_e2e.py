"""Full lifecycle E2E tests for Matter lock provider."""

from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from matter_server.client.exceptions import InvalidState
from matter_server.client.models.node import MatterNode
from matter_server.common.models import EventType, MatterNodeEvent
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.components.matter import lock_helpers
from homeassistant.config_entries import SOURCE_USER, ConfigEntriesFlowManager
from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.lock_code_manager.const import (
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_SLOT,
    CONF_USERS,
    DOMAIN,
)
from custom_components.lock_code_manager.core.slot import Seen
from custom_components.lock_code_manager.core.worker import RETRY_AFTER
from custom_components.lock_code_manager.domain.credentials import (
    WriteResult,
)
from custom_components.lock_code_manager.domain.lock_runtime import (
    lock_registry,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._util import parse_tag
from custom_components.lock_code_manager.providers.matter import MatterLock

# Module path where lock_helpers functions are imported in the provider
from tests.common import (
    async_add_users,
    async_advance_clock,
    async_advance_sync_ticks,
    async_configure_options,
    async_set_slot_pin,
    async_switch_slot,
    async_tick_until,
    code_entity_id,
    flow_users,
    in_sync_entity_id,
    lcm_issue_ids,
    user_subentries,
)
from tests.conftest import async_advance_time, async_initial_tick

from ...common import TICK_INTERVAL
from .conftest import FAKE_MATTER_SLOTS, matter_helpers_patched
from .fake_node import DlStatus, FakeMatterLock, ImStatus

_PROVIDER_MODULE = "custom_components.lock_code_manager.providers.matter"
WORKER = "custom_components.lock_code_manager.core.worker"


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Matter provider."""

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_matter_lock.coordinator is not None


class TestSetAndClearUsercodes:
    """Verify set/clear operations call the correct Matter helpers via base orchestration."""

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """
        After set, the provider's push tells the core the slot holds a masked code.

        Matter PINs are write-only so the push carries an unreadable code
        instead of the actual PIN value. The push comes from
        async_set_credential; slot 4 is managed by no entry, so the push
        lands with the lock's untracked codes.
        """
        matter_mock_helpers["set_lock_credential"].reset_mock()
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
        ):
            result = await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert result is WriteResult.CONFIRMED
        assert matter_mock_helpers["set_lock_credential"].call_count >= 1
        worker = lock_registry(hass).worker(e2e_matter_lock.lock.entity_id)
        assert worker is not None
        assert worker.port.others.get(4) is Seen.MASKED

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """After clear, the provider pushes an empty slot 2 to the lock's worker."""
        worker = lock_registry(hass).worker(e2e_matter_lock.lock.entity_id)
        assert worker is not None
        matter_mock_helpers["get_lock_users"].return_value = _TAGGED_USERS
        pushed: list[tuple[int, object]] = []
        with (
            patch.object(
                worker, "push", lambda slot, value: pushed.append((slot, value))
            ),
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_user",
                matter_mock_helpers["clear_lock_user"],
            ),
        ):
            await e2e_matter_lock.async_clear_usercode(2)

        assert (2, Seen.EMPTY) in pushed


class TestGetUsercodes:
    """Verify reading usercodes from the Matter lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        lock_entity: er.RegistryEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """
        Get usercodes returns slot occupancy from Matter.

        The base async_get_usercodes projects async_get_users output onto
        the managed-slot map. Slot 1 is occupied (unreadable), slot 2 is
        empty when the mock reports only a user on slot 1.
        """
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "credentials": [
                            {
                                "type": "pin",
                                "index": 1,
                            }
                        ],
                    },
                ],
            }
        )

        with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
            codes = await e2e_matter_lock.async_get_usercodes()

        assert codes[1] is SlotCredential.unreadable()
        assert codes[2] is SlotCredential.empty()


# =============================================================================
# The #1538 symptom, end to end on a real coordinator
# =============================================================================

_TAGGED_USERS = {
    "max_users": 10,
    "users": [
        {
            "user_index": 1,
            "user_name": "lcm:1:slot1",
            "credentials": [{"type": "pin", "index": 1}],
        },
        {
            "user_index": 2,
            "user_name": "lcm:2:slot2",
            "credentials": [{"type": "pin", "index": 2}],
        },
    ],
}
_AFTER_USER_ONE_DELETED = {"max_users": 10, "users": _TAGGED_USERS["users"][1:]}


def _user_cleared_event(node_id: int, user_index: int) -> MatterNodeEvent:
    """The LockUserChange a lock emits for ClearUser (a user record, cleared)."""
    return MatterNodeEvent(
        node_id=node_id,
        endpoint_id=1,
        cluster_id=257,
        event_id=4,
        event_number=0,
        priority=1,
        timestamp=0,
        timestamp_type=0,
        data={
            "lockDataType": 2,
            "dataOperationType": 1,
            "dataIndex": user_index,
            "userIndex": user_index,
        },
    )


class TestOutOfBandDeletion:
    """A credential deleted through Manage access is noticed and put back."""

    @pytest.fixture
    def matter_mock_helpers(
        self, matter_mock_helpers: dict[str, AsyncMock]
    ) -> dict[str, AsyncMock]:
        """LCM-tagged users, as the provider itself would have written them."""
        matter_mock_helpers["get_lock_users"].return_value = _TAGGED_USERS
        return matter_mock_helpers

    async def test_clear_user_flips_in_sync_off_and_sync_recreates_the_pin(
        self,
        hass: HomeAssistant,
        matter_client: MagicMock,
        matter_node: MatterNode,
        lock_entity: er.RegistryEntry,
        lcm_config_entry: MockConfigEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Issue #1538 on a Matter lock: the deletion is seen at once, not hourly.

        Manage access sends ClearUser, so the lock no longer lists the user
        when the event arrives; the slot it anchored is resolved from the
        provider's last read. The in-sync sensor goes off on the event, the
        worker writes the PIN back for the same user, and the sensor
        returns to on once the write's read-back lists the user again.
        """
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 1, lock_entity.entity_id)
        await async_initial_tick(hass, in_sync)
        for _ in range(3):
            if hass.states.get(in_sync).state == STATE_ON:
                break
            await async_advance_time(hass, TICK_INTERVAL)
        assert hass.states.get(in_sync).state == STATE_ON

        lock = lcm_config_entry.runtime_data.locks[lock_entity.entity_id]
        # Home Assistant's own Matter lock entity subscribes to the same node
        # events; take the provider's subscription, not the platform's.
        deliver = next(
            call.kwargs["callback"]
            for call in matter_client.subscribe_events.call_args_list
            if call.kwargs.get("event_filter") == EventType.NODE_EVENT
            and call.kwargs.get("node_filter") == matter_node.node_id
            and getattr(call.kwargs["callback"], "__self__", None) is lock
        )

        matter_mock_helpers["get_lock_users"].return_value = _AFTER_USER_ONE_DELETED
        set_credential = matter_mock_helpers["set_lock_credential"]
        set_credential.reset_mock()
        write = set_credential.side_effect

        async def _write_back(*args: Any, **kwargs: Any) -> Any:
            # The lock lists the user again once the PIN is written: the
            # write's read-back sees it.
            result = await write(*args, **kwargs)
            matter_mock_helpers["get_lock_users"].return_value = _TAGGED_USERS
            return result

        set_credential.side_effect = _write_back
        states: list[str] = []
        unsub = async_track_state_change_event(
            hass, in_sync, lambda event: states.append(event.data["new_state"].state)
        )
        deliver(EventType.NODE_EVENT, _user_cleared_event(matter_node.node_id, 1))
        await hass.async_block_till_done(wait_background_tasks=True)
        unsub()
        # The deletion turned the sensor off, then the write-back on again.
        assert states[:1] == [STATE_OFF], states

        writes = matter_mock_helpers["set_lock_credential"].await_args_list
        assert any(
            call.kwargs.get("credential_data") == "1234"
            and call.kwargs.get("user_index") == 1
            for call in writes
        ), [call.kwargs for call in writes]
        assert hass.states.get(in_sync).state == STATE_ON


class TestAddingThroughTheUserInterface:
    """An allocation that finishes at once goes straight on, never to progress.

    Allocation reads a Matter lock's users in one call (the indices writing
    would adopt), and these helpers answer it without waiting, so it finishes
    the moment it starts. Shown as progress anyway, the dialog hears nothing:
    Home Assistant announces the step moved on before the dialog has
    subscribed to hear it, and setup sits on the progress screen for good
    (issue #1639). Each test checks that one read was all the lock was asked.
    """

    @pytest.fixture(autouse=True)
    def _helpers_patched(
        self, matter_mock_helpers: dict[str, AsyncMock]
    ) -> Iterator[None]:
        """Keep the helpers patched for the entry the flow creates and sets up."""
        with matter_helpers_patched(matter_mock_helpers):
            yield

    async def _start_flow(
        self,
        hass: HomeAssistant,
        lock_entity_id: str,
        helpers: dict[str, AsyncMock],
    ) -> str:
        """Start setup on the Matter lock and return the flow at its path menu."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "matter", CONF_LOCKS: [lock_entity_id]}
        )
        assert result["step_id"] == "choose_path"
        for helper in helpers.values():
            helper.reset_mock()
        return flow_id

    async def test_the_user_count_goes_straight_to_naming_the_first_user(
        self,
        hass: HomeAssistant,
        lock_entity: er.RegistryEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Submitting the count shows the first user's form, never progress."""
        flow_id = await self._start_flow(
            hass, lock_entity.entity_id, matter_mock_helpers
        )
        await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 3}
        )

        assert result["type"] == "form"
        assert result["step_id"] == "code_slot"
        assert result["description_placeholders"] == {"user_num": 1, "num_users": 3}
        assert _asked(matter_mock_helpers) == ["get_lock_users"]

        for num in (1, 2, 3):
            result = await hass.config_entries.flow.async_configure(
                flow_id,
                {CONF_NAME: f"User {num}", CONF_ENABLED: True, CONF_PIN: f"{num}" * 4},
            )

        assert result["type"] == "create_entry"
        assert flow_users(result) == {
            f"User {num}": {CONF_ENABLED: True, CONF_PIN: f"{num}" * 4}
            for num in (1, 2, 3)
        }
        await hass.config_entries.async_unload(result["result"].entry_id)

    async def test_a_block_of_users_goes_straight_to_creating_the_entry(
        self,
        hass: HomeAssistant,
        lock_entity: er.RegistryEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Submitting the users block creates the entry, never progress."""
        flow_id = await self._start_flow(
            hass, lock_entity.entity_id, matter_mock_helpers
        )
        await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "yaml"}
        )
        users = {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}}
        # Setting the new entry up asks the lock plenty; what counts is what
        # was asked before the entry was handed over to be saved.
        asked_before_saving: list[list[str]] = []
        finish_flow = ConfigEntriesFlowManager.async_finish_flow

        async def _finish_flow(manager, flow, result):
            asked_before_saving.append(_asked(matter_mock_helpers))
            return await finish_flow(manager, flow, result)

        with patch.object(ConfigEntriesFlowManager, "async_finish_flow", _finish_flow):
            result = await hass.config_entries.flow.async_configure(
                flow_id, {CONF_USERS: users}
            )

        assert result["type"] == "create_entry"
        assert flow_users(result) == users
        assert asked_before_saving == [["get_lock_users"]]
        await hass.config_entries.async_unload(result["result"].entry_id)


class TestAddingTheLockToAnEntry:
    """
    Regression for #1684: an added Matter lock's own users are not taken over.

    Writing slot n on a Matter lock adopts an untagged user holding a PIN at
    credential index n, renaming them and writing over their PIN. The
    options flow reads the lock for those before letting it join.
    """

    async def _async_add(
        self,
        hass: HomeAssistant,
        lock: er.RegistryEntry,
        slots: dict[int, dict[str, Any]] = FAKE_MATTER_SLOTS,
    ) -> dict[str, Any]:
        """Add ``lock`` to an entry on no lock whose users hold ``slots``."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: []},
            subentries_data=user_subentries(slots),
        )
        entry.add_to_hass(hass)
        started = await hass.config_entries.options.async_init(entry.entry_id)
        return await async_configure_options(
            hass, started["flow_id"], {CONF_LOCKS: [lock.entity_id]}
        )

    async def test_a_user_at_one_of_the_entry_s_indices_keeps_the_lock_out(
        self,
        hass: HomeAssistant,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """Mom, added through Home Assistant's own Matter actions, is named."""
        await _async_add_user(fake_matter_lock, "Mom", credential_index=1)

        result = await self._async_add(hass, fake_matter_lock_entity)

        assert result["type"] == "form"
        assert result["errors"] == {"base": "existing_codes"}
        assert result["description_placeholders"] == {
            "error_lock": fake_matter_lock_entity.entity_id,
            "slots": "1",
        }
        assert fake_matter_lock.holds(1) == "246810"

    @pytest.mark.parametrize(
        ("name", "credential_index"), [("lcm:1:Old", 1), ("Mom", 3)]
    )
    async def test_a_tagged_user_or_one_elsewhere_lets_the_lock_in(
        self,
        hass: HomeAssistant,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
        name: str,
        credential_index: int,
    ) -> None:
        """Lock Code Manager's own user, or one at no index of the entry's, is not in the way."""
        await _async_add_user(fake_matter_lock, name, credential_index=credential_index)

        result = await self._async_add(hass, fake_matter_lock_entity)

        assert result["type"] == "create_entry"
        assert result["data"][CONF_LOCKS] == [fake_matter_lock_entity.entity_id]

    async def test_a_lock_another_entry_holds_is_read_in_its_turn(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """Shared with an entry on slots 1 and 2, Mom at index 3 is still named."""
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        await _async_add_user(fake_matter_lock, "Mom", credential_index=3)

        result = await self._async_add(
            hass,
            fake_matter_lock_entity,
            {3: {CONF_NAME: "slot3", CONF_PIN: "333333", CONF_ENABLED: True}},
        )

        assert result["errors"] == {"base": "existing_codes"}
        assert result["description_placeholders"]["slots"] == "3"


async def _async_add_user(
    fake: FakeMatterLock, name: str, *, credential_index: int
) -> None:
    """Put a user and its PIN on the lock through Home Assistant's own Matter actions."""
    added = await lock_helpers.set_lock_credential(
        fake.client,
        fake.node,
        credential_type="pin",
        credential_data="246810",
        credential_index=credential_index,
    )
    await lock_helpers.set_lock_user(
        fake.client, fake.node, user_index=added["user_index"], user_name=name
    )


def _asked(helpers: dict[str, AsyncMock]) -> list[str]:
    """Return the Matter helpers awaited since they were last reset."""
    return sorted(name for name, helper in helpers.items() if helper.await_count)


# ---------------------------------------------------------------------------
# Regressions on the stateful lock (``fake_node.py``): driven and checked only
# through Home Assistant and what the fake lock holds.
# ---------------------------------------------------------------------------


def _states(
    hass: HomeAssistant, entry: MockConfigEntry, lock: er.RegistryEntry, slot: int
) -> tuple[str, str]:
    """Return a slot's in-sync and code sensor states on the lock."""
    return (
        hass.states.get(in_sync_entity_id(hass, entry, slot, lock.entity_id)).state,
        hass.states.get(code_entity_id(hass, entry, slot, lock.entity_id)).state,
    )


async def _async_settle(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    lock: er.RegistryEntry,
    slots: Iterable[int] = FAKE_MATTER_SLOTS,
) -> None:
    """Tick until every slot named reads in sync."""
    await async_tick_until(
        hass,
        freezer,
        lambda: all(_states(hass, entry, lock, slot)[0] == STATE_ON for slot in slots),
        max_ticks=10,
        what="every slot in sync",
    )


def _index_of(fake: FakeMatterLock, pin: str) -> int:
    """Return the credential index the lock holds ``pin`` at."""
    return next(index for index, held in fake.codes.items() if held == pin)


@asynccontextmanager
async def _entry_on(
    hass: HomeAssistant,
    lock: er.RegistryEntry,
    slots: dict[int, dict[str, Any]] = FAKE_MATTER_SLOTS,
    unique_id: str = "batch5",
) -> AsyncIterator[MockConfigEntry]:
    """Set up an entry on the fake lock with ``slots``; unload it however the test ends."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [lock.entity_id]},
        subentries_data=user_subentries(slots),
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        yield entry
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


class TestKeypadAndSwitch:
    """Uses of a code at the lock, and turning a user off (#984)."""

    async def test_a_keypad_use_updates_the_user_s_event_entity(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #984 (PRs #985, #1051): a PIN used at the keypad is recorded.

        The LockOperation never reached Lock Code Manager on real devices.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        event_entity = next(
            state.entity_id
            for state in hass.states.async_all("event")
            if state.attributes.get("code_slot") == 2
        )
        before = hass.states.get(event_entity).state
        used = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)

        fake_matter_lock.keypad_unlock(
            _index_of(fake_matter_lock, FAKE_MATTER_SLOTS[2][CONF_PIN])
        )
        await async_advance_sync_ticks(hass, freezer)

        assert hass.states.get(event_entity).state != before
        assert [event.data["name"] for event in used] == ["slot2"]

    async def test_turning_a_user_off_takes_its_pin_off_the_lock(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #984 (PR #1045): a code that is turned off leaves the lock.

        The clear was called in a way that failed, so the code stayed.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )

        await async_switch_slot(hass, fake_matter_lock_entry, 2, SERVICE_TURN_OFF)
        await async_advance_sync_ticks(hass, freezer, 5)

        assert FAKE_MATTER_SLOTS[2][CONF_PIN] not in fake_matter_lock.codes.values()
        assert FAKE_MATTER_SLOTS[1][CONF_PIN] in fake_matter_lock.codes.values()
        assert _states(hass, fake_matter_lock_entry, fake_matter_lock_entity, 2) == (
            STATE_ON,
            "",
        )


class TestStartup:
    """A lock that is not ready yet, and one that goes away (#1257)."""

    async def test_a_lock_not_ready_at_startup_is_retried_not_disabled(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1257 (PRs #1258, #1286): what a lock not ready says is retried.

        Right after a restart the lock answered SetCredential with status 133
        (InvalidField, which Home Assistant names ``unknown(133)``), and the
        client, not yet connected, raised ``InvalidState`` (MS_CLIENT:771-772):
        the slots were disabled or suspended for good. Here the lock refuses
        every write for its first stretch of time; each slot waits longer
        before each try again, so its last try lands once the lock is ready.
        """
        not_ready_for = RETRY_AFTER[0] + RETRY_AFTER[1] / 2
        refused = fake_matter_lock.refuse(
            "SetCredential", status=DlStatus.kInvalidField
        )
        not_connected = fake_matter_lock.fail(
            "SetUser", error=InvalidState("Not connected")
        )

        # The spacing that ships (imported before the harness zeroes it).
        with patch(f"{WORKER}.RETRY_AFTER", RETRY_AFTER):
            await self._a_lock_not_ready_for(
                hass,
                freezer,
                fake_matter_lock_entity,
                refused,
                not_connected,
                not_ready_for,
            )

    async def _a_lock_not_ready_for(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock_entity: er.RegistryEntry,
        refused: Any,
        not_connected: Any,
        not_ready_for: float,
    ) -> None:
        async with _entry_on(hass, fake_matter_lock_entity) as entry:
            await async_advance_clock(hass, freezer, not_ready_for)
            assert refused.hits + not_connected.hits > 0
            refused.clear()
            not_connected.clear()
            await async_tick_until(
                hass,
                freezer,
                lambda: all(
                    _states(hass, entry, fake_matter_lock_entity, slot)[0] == STATE_ON
                    for slot in FAKE_MATTER_SLOTS
                ),
                max_ticks=int(RETRY_AFTER[1]),
                what="every slot in sync",
            )

            assert lcm_issue_ids(hass, "slot_disabled") == []
            assert lcm_issue_ids(hass, "slot_suspended") == []

    async def test_a_pin_held_back_by_an_outage_reaches_the_lock_when_it_returns(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1257 (PR #1289): a node-only outage recovers within seconds.

        The PIN is changed as the lock drops off, and the outage outlasts the
        30-second connection check, so Lock Code Manager sees the lock gone
        while the Matter entry stays loaded. Once it was back, the PIN reached
        it only minutes later.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        unreachable = fake_matter_lock.fail()
        await async_set_slot_pin(hass, fake_matter_lock_entry, 1, "246802")
        fake_matter_lock.set_available(False)
        await async_advance_sync_ticks(hass, freezer, 30)
        assert "246802" not in fake_matter_lock.codes.values()

        unreachable.clear()
        fake_matter_lock.set_available(True)
        await async_tick_until(
            hass,
            freezer,
            lambda: "246802" in fake_matter_lock.codes.values(),
            max_ticks=15,
            what="the new PIN on the lock",
        )


class TestRestartsAndRenames:
    """What a reload or a rename does to users already on the lock."""

    async def test_a_reload_with_users_on_the_lock_disables_nothing(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #1044: after a restart the PINs already there are kept.

        Written again, each was refused as a duplicate of itself and the slot
        disabled, on every restart.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        codes = dict(fake_matter_lock.codes)

        assert await hass.config_entries.async_reload(fake_matter_lock_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)

        assert lcm_issue_ids(hass, "slot_disabled") == []
        assert fake_matter_lock.codes == codes
        for slot in FAKE_MATTER_SLOTS:
            assert (
                _states(hass, fake_matter_lock_entry, fake_matter_lock_entity, slot)[0]
                == STATE_ON
            )

    async def test_each_user_on_the_lock_carries_its_slot_s_tag(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #1046: the user's name is written, and names its slot.

        ``set_lock_user`` was called with an argument it does not take, so no
        name was ever set.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )

        tagged = {
            parse_tag(user.name)[0]: user_index
            for user_index, user in fake_matter_lock.users.items()
        }

        assert set(tagged) == set(FAKE_MATTER_SLOTS)
        for slot, config in FAKE_MATTER_SLOTS.items():
            index = _index_of(fake_matter_lock, config[CONF_PIN])
            assert fake_matter_lock.owner(index) == tagged[slot]


class TestRefusedUserUpdates:
    """A lock that refuses every update of a user it already holds."""

    async def test_a_refused_user_update_still_writes_the_pin(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #1245: a refused name update is not a failed sync.

        Each PIN write first updates the user's name; this lock refuses every
        such update as InvalidCommand (0x85), whatever the name. That
        suspended the slot though the PIN itself was taken.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        refused = fake_matter_lock.refuse("SetUser", status=ImStatus.INVALID_COMMAND)

        await async_set_slot_pin(hass, fake_matter_lock_entry, 1, "246802")
        await async_advance_sync_ticks(hass, freezer, 5)

        assert refused.hits
        assert "246802" in fake_matter_lock.codes.values()
        assert lcm_issue_ids(hass, "slot_suspended") == []
        assert _states(hass, fake_matter_lock_entry, fake_matter_lock_entity, 1) == (
            STATE_ON,
            "246802",
        )


class TestOtherUsersOnTheLock:
    """Changes Lock Code Manager did not make."""

    async def test_a_user_removed_without_an_event_is_put_back(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #1049: the hourly read sees a change no event announced.

        Another controller's change reaches no subscriber here; without the
        drift read, the slot showed in sync while the lock held nothing.
        """
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        index = _index_of(fake_matter_lock, FAKE_MATTER_SLOTS[1][CONF_PIN])
        fake_matter_lock.forget(fake_matter_lock.owner(index))

        await async_advance_clock(hass, freezer, 65 * 60)
        await async_advance_sync_ticks(hass, freezer, 5)

        assert FAKE_MATTER_SLOTS[1][CONF_PIN] in fake_matter_lock.codes.values()

    async def test_a_turned_off_user_leaves_a_homeowner_s_code_alone(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Neither turning a user off nor deleting it removes a code Lock Code Manager did not write.

        Slot 3's user is turned off, so nothing was ever written for it, and
        the homeowner's own user holds a PIN at credential index 3. Regression
        for #1518: PR #1522 kept the release from adopting that untagged
        user; the turned-off slot's clear still adopted it by the old
        index-equals-slot rule and cleared the homeowner's credential before
        any release ran. And for #1695: the slot read the homeowner's code as
        its own, so the clear that leaves it alone never brought it in sync,
        and it was suspended with a repair nothing could resolve.
        """
        fake_matter_lock.program(7, "777777", name="Mom", credential_index=3)
        slots = {
            **FAKE_MATTER_SLOTS,
            3: {CONF_NAME: "slot3", CONF_PIN: "333333", CONF_ENABLED: False},
        }
        async with _entry_on(
            hass, fake_matter_lock_entity, slots, "batch5_release"
        ) as entry:
            await _async_settle(hass, freezer, entry, fake_matter_lock_entity, slots)
            await async_advance_sync_ticks(hass, freezer, 5)

            assert _states(hass, entry, fake_matter_lock_entity, 3) == (STATE_ON, "")
            assert lcm_issue_ids(hass, "slot_suspended") == []
            worker = lock_registry(hass).worker(fake_matter_lock_entity.entity_id)
            assert worker is not None
            # Reported as a code nobody here manages.
            assert worker.port.others[3] is Seen.MASKED
            assert fake_matter_lock.holds(3) == "777777"

            subentry = next(
                (s for s in entry.subentries.values() if s.data.get(CONF_SLOT) == 3),
                None,
            )
            if subentry is None:
                pytest.fail("slot 3's user is not a subentry")
            if not hass.config_entries.async_remove_subentry(
                entry, subentry.subentry_id
            ):
                pytest.fail("slot 3's user could not be removed")
            await hass.async_block_till_done()
            await async_advance_sync_ticks(hass, freezer, 3)

            assert fake_matter_lock.holds(3) == "777777"
            assert fake_matter_lock.users[7].name == "Mom"
            assert lcm_issue_ids(hass, "slot_suspended") == []

    async def test_a_turned_on_slot_still_adopts_a_user_at_its_index(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        An untagged user at a slot's index is the slot's to adopt when it wants a PIN.

        How Lock Code Manager 2.0 left its users: the write tags the user and
        puts the slot's PIN on it, instead of leaving it holding the old one
        (#1695 reads that user as someone else's code only while the slot is off).
        """
        await _async_add_user(fake_matter_lock, "Old", credential_index=3)
        user_index = fake_matter_lock.owner(3)
        slots = {
            **FAKE_MATTER_SLOTS,
            3: {CONF_NAME: "slot3", CONF_PIN: "333333", CONF_ENABLED: True},
        }
        async with _entry_on(
            hass, fake_matter_lock_entity, slots, "batch5_adopt"
        ) as entry:
            await _async_settle(hass, freezer, entry, fake_matter_lock_entity, slots)

            assert fake_matter_lock.holds(3) == "333333"
            assert fake_matter_lock.owner(3) == user_index
            assert user_index is not None
            assert parse_tag(fake_matter_lock.users[user_index].name)[0] == 3

    async def test_a_user_at_a_slot_s_index_the_lock_will_not_rename_is_named(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        No PIN goes on a user the slot cannot tag, and the repair says why.

        Slot 3 is on, and the homeowner's user, made by another admin, holds
        a PIN at credential index 3. Writing the slot adopts that user, but
        Matter lets only the fabric that made a user rename it. A PIN written
        onto a user left untagged could never be cleared (#1518), so none is
        written; the slot was then held with "the lock did not take the code"
        and advice about the Thread link, which pointed nowhere near the cause.
        """
        fake_matter_lock.program(7, "777777", name="Mom", credential_index=3)
        slots = {
            **FAKE_MATTER_SLOTS,
            3: {CONF_NAME: "slot3", CONF_PIN: "333333", CONF_ENABLED: True},
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [fake_matter_lock_entity.entity_id]},
            subentries_data=user_subentries(slots),
            unique_id="batch5_foreign_on",
        )
        entry.add_to_hass(hass)
        # Not settled here: the slot's tries are paced by the clock.
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            await async_tick_until(
                hass,
                freezer,
                lambda: bool(lcm_issue_ids(hass, "slot_suspended")),
                max_ticks=20,
                what="slot 3 held",
            )

            assert fake_matter_lock.holds(3) == "777777"
            assert fake_matter_lock.users[7].name == "Mom"
            assert "333333" not in fake_matter_lock.codes.values()
            issue = ir.async_get(hass).async_get_issue(
                DOMAIN,
                f"slot_suspended_{entry.entry_id}_{fake_matter_lock_entity.entity_id}_3",
            )
            assert issue is not None
            assert issue.translation_placeholders is not None
            reason = issue.translation_placeholders["reason"]
            assert reason.startswith("User 7 on the lock holds a PIN at this slot's")
            assert "may not let Lock Code Manager rename it" in reason
            assert "If this keeps happening, check that user" in reason
            assert "lock maker's app" in reason
            # Advice about the Thread link points away from the cause.
            assert "Thread" not in reason
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

    async def test_a_node_it_cannot_reach_is_not_a_user_to_remove(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A rename that never reached the lock does not send anyone to the vendor's app.

        Lock Code Manager 2.0's own user for slot 3 is untagged at index 3, so
        the write adopts it. The server answering that it cannot reach the
        node is no refusal of the name: blaming the user at the index would
        have the homeowner remove a code that is Lock Code Manager's own.
        """
        fake_matter_lock.program(7, "333333", name="slot3", credential_index=3)
        fake_matter_lock.fail("SetUser")
        slots = {
            **FAKE_MATTER_SLOTS,
            3: {CONF_NAME: "slot3", CONF_PIN: "444444", CONF_ENABLED: True},
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [fake_matter_lock_entity.entity_id]},
            subentries_data=user_subentries(slots),
            unique_id="batch5_adopt_unreachable",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        try:
            await async_tick_until(
                hass,
                freezer,
                lambda: bool(lcm_issue_ids(hass, "slot_suspended")),
                max_ticks=40,
                what="slot 3 held",
            )

            assert fake_matter_lock.users[7].name == "slot3"
            issue = ir.async_get(hass).async_get_issue(
                DOMAIN,
                f"slot_suspended_{entry.entry_id}_{fake_matter_lock_entity.entity_id}_3",
            )
            assert issue is not None
            assert issue.translation_placeholders is not None
            reason = issue.translation_placeholders["reason"]
            # Unexplained: the generic reason, as any transport failure.
            assert reason.startswith(
                "the lock did not take the code after several tries"
            )
            assert "lock maker's app" not in reason
        finally:
            await hass.config_entries.async_unload(entry.entry_id)


class TestOpenBugs:
    """Open Matter bugs, each pinned by a strict xfail on its own assertion."""

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "open: a user index freed at the lock without an event and reused "
            "by a new user's create leaves the old slot showing its stale code"
        ),
    )
    async def test_an_index_reused_after_a_silent_deletion_is_not_the_old_slot_s(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """Slot 1, whose user was removed out of band, goes out of sync when its index is reused."""
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        index = _index_of(fake_matter_lock, FAKE_MATTER_SLOTS[1][CONF_PIN])
        fake_matter_lock.forget(fake_matter_lock.owner(index))

        result = await async_add_users(
            hass,
            fake_matter_lock_entry,
            {"slot3": {CONF_ENABLED: True, CONF_PIN: "333333"}},
        )
        if result.get("reason") != "users_added":
            pytest.fail(f"the user was not added: {result}")
        await async_advance_sync_ticks(hass, freezer, 5)
        if fake_matter_lock.owner(index) is None:
            pytest.fail("the new user's PIN did not reuse the freed index")

        assert _states(hass, fake_matter_lock_entry, fake_matter_lock_entity, 1)[0] != (
            STATE_ON
        )

    async def test_nothing_reaches_the_lock_after_the_entry_unloads(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_matter_lock: FakeMatterLock,
        fake_matter_lock_entry: MockConfigEntry,
        fake_matter_lock_entity: er.RegistryEntry,
    ) -> None:
        """A change announced just before unloading sends nothing once the entry is gone."""
        await _async_settle(
            hass, freezer, fake_matter_lock_entry, fake_matter_lock_entity
        )
        # The worker gives the exchange it is in no time to finish once it stops.
        lock_registry(hass)._grace = 0
        held = fake_matter_lock.hang("GetUser")
        fake_matter_lock.program(9, "999999", name="Mom", credential_index=9)
        await held.async_wait_held()

        if not await hass.config_entries.async_unload(fake_matter_lock_entry.entry_id):
            pytest.fail("the entry did not unload")
        sent = len(fake_matter_lock.commands)
        held.clear()
        # No sync tick is scheduled once the entry is gone; move the clock.
        await async_advance_clock(hass, freezer, 30)

        assert fake_matter_lock.commands[sent:] == []
