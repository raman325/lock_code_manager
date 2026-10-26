"""Full lifecycle E2E tests for the zwave-js-ui lock provider."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from functools import partial
import json
import threading
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_mqtt_message,
)

from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    ATTR_OPERATION,
    ATTR_TARGET,
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
)
from custom_components.lock_code_manager.ha.runtime import (
    lock_codes,
    lock_registry,
)
from custom_components.lock_code_manager.model.credentials import (
    SlotCode,
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.model.events import CredentialOperation
from custom_components.lock_code_manager.model.exceptions import DuplicateCodeError
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    ZWaveJSUILock,
    _published_code,
)
from custom_components.lock_code_manager.sync.slot import LIMIT
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_configure_flow,
    async_set_slot_pin,
    async_tick_until,
    code_entity_id,
    current_entry,
    in_sync_entity_id,
    slot_entity_id,
    sync_status,
)
from tests.conftest import async_advance_time

from ...common import TICK_INTERVAL, user_subentries
from .conftest import (
    ZUI_API_BASE,
    ZUI_NODE_ID,
    ZUI_NODE_TOPIC,
    ZUI_PREFIX,
    ZWaveJSUIApiResponder,
    async_discover_zui_lock,
    fire_zui_node_value,
)

# Door Lock is 98; User Code Command Class is 99.
CC_USER_CODE_ID = 99
# zwave-js UserIDStatus: 0 Available, 1 Enabled, 2 Disabled.
STATUS_AVAILABLE = 0
STATUS_ENABLED = 1
STATUS_DISABLED = 2
# node-zwave-js SupervisionStatus.Fail, as a supervised ``set``/``clear``
# returns it through ``sendCommand``.
SUPERVISION_FAIL = {"status": 2}

E2E_SLOT_PINS = {1: "1234", 2: "5678"}
LOCK_CAPACITY = 20
# Second node on the same gateway, for the mixed push/api-only entry.
API_ONLY_NODE_ID = 21


def _capacity_is_the_node_id(_api_base: str, request: dict[str, Any]) -> dict[str, Any]:
    """
    Answer ``sendCommand`` with a value that names which node was asked.

    Two locks sharing one response wildcard see each other's answers, so a
    correlation test needs answers that are told apart by content. Reporting
    each node's capacity as its own id makes a crossed wire a wrong number.
    """
    target, method, _method_args = request["args"]
    result: Any = None
    if method == "getUsersCount":
        result = target["nodeId"]
    elif method == "get":
        result = {"userIdStatus": STATUS_AVAILABLE}
    return {"success": True, "result": result}


class UserCodeTable:
    """
    A node's User Code Command Class table, driven through ``sendCommand``.

    Every credential operation this provider performs is one ``sendCommand``
    api call naming a driver method, so answering that single api with a real
    table covers the whole lifecycle: what a ``set`` writes is what the next
    ``get`` reads back, which is what makes an end-to-end sync assertion mean
    anything.
    """

    def __init__(self, codes: dict[int, str] | None = None) -> None:
        """Initialize with an optionally pre-programmed set of slots."""
        self.codes: dict[int, str] = dict(codes or {})

    def __call__(self, _api_base: str, request: dict[str, Any]) -> dict[str, Any]:
        """Answer one ``sendCommand`` request against the table."""
        _target, method, method_args = request["args"]
        result: Any = None
        if method == "get":
            (slot_num,) = method_args
            code = self.codes.get(slot_num)
            result = (
                {"userIdStatus": STATUS_ENABLED, "userCode": code}
                if code is not None
                else {"userIdStatus": STATUS_AVAILABLE}
            )
        elif method == "set":
            slot_num, _status, code = method_args
            self.codes[slot_num] = code
        elif method == "clear":
            (slot_num,) = method_args
            self.codes.pop(slot_num, None)
        elif method == "getUsersCount":
            result = LOCK_CAPACITY
        return {"success": True, "result": result}


def held_codes(hass: HomeAssistant, lock_entity_id: str) -> dict[int, str | SlotCode]:
    """Return what the lock's worker last heard about each slot."""
    return lock_codes(hass, lock_entity_id)


def charged(hass: HomeAssistant, lock_entity_id: str, slot_num: int) -> bool:
    """Return whether a failed sync of the slot was charged to it."""
    worker = lock_registry(hass).worker(lock_entity_id)
    assert worker is not None
    slot = worker.slots[pin_address(slot_num)]
    return slot.tries > 0 or slot.hold is not None


def poll_interval(hass: HomeAssistant, lock: ZWaveJSUILock) -> timedelta | None:
    """Return how often the lock's worker reads it unasked."""
    worker = lock_registry(hass).worker(lock.lock.entity_id)
    assert worker is not None
    return min(
        (interval for interval in worker.port._cadence() if interval),
        default=None,
    )


def send_commands(responder: ZWaveJSUIApiResponder) -> list[list[Any]]:
    """Return the argument list of every ``sendCommand`` the gateway received."""
    return [args for _base, name, args in responder.requests if name == "sendCommand"]


def user_code_call(method: str, args: list[Any]) -> list[Any]:
    """Build the ``sendCommand`` arguments a User Code CC call must arrive as."""
    return [
        {"nodeId": ZUI_NODE_ID, "commandClass": CC_USER_CODE_ID, "endpoint": 0},
        method,
        args,
    ]


@pytest.fixture
def user_code_table() -> UserCodeTable:
    """The stand-in node's slot table, empty until LCM programs it."""
    return UserCodeTable()


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    zui_lock_discovered,
    zui_api_responder: ZWaveJSUIApiResponder,
    user_code_table: UserCodeTable,
    mqtt_teardown,
) -> AsyncGenerator[MockConfigEntry]:
    """
    Set up a full LCM config entry over the discovered zwave-js-ui lock.

    Overrides the conftest fixture of the same name -- and with it the
    ``zui_lock`` fixture built on top of it -- because setup itself reads
    every managed slot and programs the ones that differ. The gateway has to
    be answering before the entry is set up, not after it.
    """
    zui_api_responder.set_handler("sendCommand", user_code_table)
    config = {
        CONF_LOCKS: [zui_lock_discovered.entity_id],
        CONF_SLOTS: {
            slot_num: {
                CONF_NAME: f"slot{slot_num}",
                CONF_PIN: pin,
                CONF_ENABLED: True,
            }
            for slot_num, pin in E2E_SLOT_PINS.items()
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="test_zui_e2e",
        subentries_data=user_subentries(config[CONF_SLOTS]),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    yield entry

    if entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.fixture
async def synced_lcm_config_entry(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    user_code_table: UserCodeTable,
) -> MockConfigEntry:
    """
    Drive sync ticks until the lock holds every configured code.

    Sync programs one slot per tick, and whether the first slot goes out on
    the tick setup already scheduled varies, so this runs to the outcome
    rather than for a fixed number of ticks. It deliberately does not assert
    the outcome; the tests below are what say what should have happened.
    """
    for _ in range(len(E2E_SLOT_PINS) + 2):
        if user_code_table.codes == E2E_SLOT_PINS:
            break
        await async_advance_time(hass, TICK_INTERVAL)
    return lcm_config_entry


class TestFullSetupLifecycle:
    """Verify LCM discovers the lock and stands the provider up end to end."""

    async def test_provider_discovered_as_zwave_js_ui(
        self, hass: HomeAssistant, lcm_config_entry: MockConfigEntry
    ) -> None:
        """The mqtt lock entity's device identifier is what picks this provider."""
        locks = lcm_config_entry.runtime_data.locks
        assert [type(lock) for lock in locks.values()] == [ZWaveJSUILock]

    async def test_gateway_resolved_through_discovery(
        self, hass: HomeAssistant, zui_lock: ZWaveJSUILock
    ) -> None:
        """
        Setup bound the gateway out of the lock's own discovery payload.

        Nothing hands the provider an api base: it reads the gateway status
        topic zwave-js-ui wrote into this entity's availability list.
        Everything below this point addresses that base.
        """
        assert zui_lock._api_base == ZUI_API_BASE


class TestInitialSync:
    """Verify configured PINs reach the lock and come back as entity state."""

    async def test_configured_pins_are_written_to_the_lock(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        user_code_table: UserCodeTable,
    ) -> None:
        """Sync programs every configured slot the lock was not already holding."""
        assert user_code_table.codes == E2E_SLOT_PINS

    async def test_writes_hit_the_wire_in_the_send_command_envelope(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Each write is one User Code CC ``set`` addressed to this node.

        The table above proves a code arrived; this proves it arrived as the
        gateway's api expects, which is the half a stand-in cannot vouch for.
        """
        commands = send_commands(zui_api_responder)
        for slot_num, pin in E2E_SLOT_PINS.items():
            assert user_code_call("set", [slot_num, STATUS_ENABLED, pin]) in commands

    async def test_setup_reads_every_managed_slot(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """Setup reads each managed slot rather than writing over it blind."""
        commands = send_commands(zui_api_responder)
        for slot_num in E2E_SLOT_PINS:
            assert user_code_call("get", [slot_num]) in commands

    async def test_disabling_a_slot_clears_it_on_the_lock(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        Turning a slot off takes the code off the lock, not just off the screen.

        The switch is the whole user gesture, so this drives it through the
        service call rather than the provider: a code that outlives being
        disabled still opens the door.
        """
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {
                ATTR_ENTITY_ID: slot_entity_id(
                    hass, SWITCH_DOMAIN, synced_lcm_config_entry, 1, CONF_ENABLED
                )
            },
            blocking=True,
        )
        for _ in range(len(E2E_SLOT_PINS) + 2):
            if 1 not in user_code_table.codes:
                break
            await async_advance_time(hass, TICK_INTERVAL)

        assert user_code_table.codes == {2: E2E_SLOT_PINS[2]}
        assert user_code_call("clear", [1]) in send_commands(zui_api_responder)

    async def test_entities_report_the_synced_state(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        The slot's in-sync sensor and code sensor both settle on the written PIN.

        This is the user-visible end of the round trip: the code sensor shows
        what the lock reports it is holding, not what the configuration asked
        for.
        """
        lock_entity_id = zui_lock.lock.entity_id
        for slot_num, pin in E2E_SLOT_PINS.items():
            in_sync = hass.states.get(
                in_sync_entity_id(
                    hass, synced_lcm_config_entry, slot_num, lock_entity_id
                )
            )
            assert in_sync is not None
            assert in_sync.state == STATE_ON
            code = hass.states.get(
                code_entity_id(hass, synced_lcm_config_entry, slot_num, lock_entity_id)
            )
            assert code is not None
            assert code.state == pin


class RefusingUserCodeTable(UserCodeTable):
    """
    A node that answers the named User Code methods with Supervision Fail.

    zwave-js-ui reports the api call itself as a success either way; the
    refusal is only in the result the driver's command returned, and a
    refused command changes nothing in the table.
    """

    def __init__(self, refused: frozenset[str]) -> None:
        """Refuse every call to one of ``refused``, starting from an empty table."""
        super().__init__()
        self.refused = refused

    def __call__(self, api_base: str, request: dict[str, Any]) -> dict[str, Any]:
        """Refuse the named methods; answer everything else as the table would."""
        _target, method, _method_args = request["args"]
        if method in self.refused:
            return {"success": True, "result": SUPERVISION_FAIL}
        return super().__call__(api_base, request)


class TestRefusedSet:
    """A set the lock refused under Supervision is a failed write."""

    @pytest.fixture
    def user_code_table(self) -> UserCodeTable:
        """The stand-in node refuses every code LCM sends it."""
        return RefusingUserCodeTable(frozenset({"set"}))

    async def test_a_supervision_fail_leaves_the_slot_out_of_sync(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        The api call succeeds and the lock says Fail: the slot is not in sync.

        Reading only the api's success, the code would be pushed as the lock's
        word and the slot would read in sync while the lock held nothing. The
        failure is charged to the slot breaker, as any failed operation is.
        """
        for _ in range(len(E2E_SLOT_PINS) + 2):
            await async_advance_time(hass, TICK_INTERVAL)

        assert user_code_table.codes == {}
        lock_entity_id = zui_lock.lock.entity_id
        for slot_num in E2E_SLOT_PINS:
            assert held_codes(hass, lock_entity_id).get(slot_num) == (SlotCode.EMPTY)
            entity_id = in_sync_entity_id(
                hass, lcm_config_entry, slot_num, lock_entity_id
            )
            in_sync = hass.states.get(entity_id)
            assert in_sync is not None
            assert in_sync.state == STATE_OFF
            assert charged(hass, lock_entity_id, slot_num)

    async def test_refused_writes_suspend_the_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock_discovered: er.RegistryEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        Regression for #1624: a set the lock refuses is a failed write, and suspends.

        zwave-js-ui reports the api call a success and carries the lock's
        Supervision Fail in its result. Read as written, the slot showed in
        sync while the lock held nothing, and repeated refusals never added
        up to a suspended slot.
        """
        lock_entity_id = zui_lock_discovered.entity_id
        issue_registry = ir.async_get(hass)
        issue_ids = {
            slot_num: f"slot_suspended_{lcm_config_entry.entry_id}_{lock_entity_id}_{slot_num}"
            for slot_num in E2E_SLOT_PINS
        }

        await async_tick_until(
            hass,
            freezer,
            lambda: all(
                issue_registry.async_get_issue(DOMAIN, issue_id)
                for issue_id in issue_ids.values()
            ),
            max_ticks=len(E2E_SLOT_PINS) * (LIMIT + 2),
            what="every refused slot suspended",
        )

        assert user_code_table.codes == {}
        for slot_num in E2E_SLOT_PINS:
            in_sync = in_sync_entity_id(
                hass, lcm_config_entry, slot_num, lock_entity_id
            )
            assert hass.states.get(in_sync).state == STATE_OFF
            assert sync_status(hass, in_sync) == "suspended"
        refused = [
            args for args in send_commands(zui_api_responder) if args[1] == "set"
        ]
        assert len(refused) == len(E2E_SLOT_PINS) * LIMIT


class TestRefusedClear:
    """A clear the lock refused under Supervision is a failed write."""

    @pytest.fixture
    def user_code_table(self) -> UserCodeTable:
        """The stand-in node accepts codes and refuses to clear them."""
        return RefusingUserCodeTable(frozenset({"clear"}))

    async def test_a_supervision_fail_keeps_the_code_on_the_slot(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        Disabling a slot whose clear the lock refuses leaves it out of sync.

        A refused clear pushed empty would read as the code being gone while
        it still opens the door.
        """
        lock_entity_id = zui_lock.lock.entity_id
        entity_id = in_sync_entity_id(hass, synced_lcm_config_entry, 1, lock_entity_id)
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {
                ATTR_ENTITY_ID: slot_entity_id(
                    hass, SWITCH_DOMAIN, synced_lcm_config_entry, 1, CONF_ENABLED
                )
            },
            blocking=True,
        )
        for _ in range(len(E2E_SLOT_PINS) + 2):
            await async_advance_time(hass, TICK_INTERVAL)

        assert user_code_table.codes == E2E_SLOT_PINS
        assert held_codes(hass, lock_entity_id).get(1) == (E2E_SLOT_PINS[1])
        in_sync = hass.states.get(entity_id)
        assert in_sync is not None
        assert in_sync.state == STATE_OFF
        assert charged(hass, lock_entity_id, 1)


class TestPushUpdates:
    """Verify node publications reach the coordinator and the entities."""

    async def test_pushed_user_code_updates_state(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        A userCode publication is the lock's word, no api round trip needed.

        On a slot nobody manages it is simply what the lock holds. On a
        managed slot it is a code someone changed at the keypad: believed
        without a ``get``, and answered by writing the configured PIN back.
        """
        lock_entity_id = zui_lock.lock.entity_id
        unmanaged_slot = max(E2E_SLOT_PINS) + 1
        before = len(send_commands(zui_api_responder))

        fire_zui_node_value(hass, f"99/0/userCode/{unmanaged_slot}", "4321")
        await hass.async_block_till_done()

        assert held_codes(hass, lock_entity_id).get(unmanaged_slot) == "4321"
        assert send_commands(zui_api_responder)[before:] == []

        user_code_table.codes[1] = "4321"
        fire_zui_node_value(hass, "99/0/userCode/1", "4321")
        await hass.async_block_till_done()

        # The set goes out on the push alone; the get after it is the
        # write's read-back.
        assert send_commands(zui_api_responder)[before:] == [
            user_code_call("set", [1, STATUS_ENABLED, E2E_SLOT_PINS[1]]),
            user_code_call("get", [1]),
        ]
        assert user_code_table.codes[1] == E2E_SLOT_PINS[1]
        code = hass.states.get(
            code_entity_id(hass, synced_lcm_config_entry, 1, lock_entity_id)
        )
        assert code is not None
        assert code.state == E2E_SLOT_PINS[1]

    async def test_messages_are_taken_in_the_order_they_arrive(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        Each message is taken as it arrives, so none overtakes the one before.

        Taken in the executor instead, a retained code the broker replays
        could land behind the change published after it, and undo it. The
        first message here is slow to take, as a busy executor makes one, so
        a second taken alongside it would overtake it.
        """
        lock_entity_id = zui_lock.lock.entity_id
        unmanaged_slot = max(E2E_SLOT_PINS) + 1
        second_taken = threading.Event()

        def slow_to_take_the_first(value: Any) -> str | None:
            if value == "1111":
                second_taken.wait(0.2)
            code = _published_code(value)
            if value == "2222":
                second_taken.set()
            return code

        with patch(f"{_MODULE}._published_code", slow_to_take_the_first):
            fire_zui_node_value(hass, f"99/0/userCode/{unmanaged_slot}", "1111")
            assert held_codes(hass, lock_entity_id).get(unmanaged_slot) == "1111"
            fire_zui_node_value(hass, f"99/0/userCode/{unmanaged_slot}", "2222")
            assert held_codes(hass, lock_entity_id).get(unmanaged_slot) == "2222"

    async def test_pushed_available_status_updates_state(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        Available is the one status that confirms the slot holds nothing.

        Asserted on a slot the entry configures nothing for, because a slot it
        does expect a code on is the stale-AVAILABLE case below.
        """
        unconfigured_slot = max(E2E_SLOT_PINS) + 1
        fire_zui_node_value(
            hass,
            f"user_code/endpoint_0/userIdStatus/{unconfigured_slot}",
            STATUS_AVAILABLE,
        )
        await hass.async_block_till_done()

        assert held_codes(hass, zui_lock.lock.entity_id).get(unconfigured_slot) == (
            SlotCode.EMPTY
        )

    async def test_a_stale_available_leaves_a_synced_slot_alone(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock_discovered: er.RegistryEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        Regression for #1471: a stale Available on a synced slot is not believed.

        Some locks announce a slot Available once a code lands on it. Taken
        at its word, it unwound the synced slot, and sync wrote the code again.
        """
        lock_entity_id = zui_lock_discovered.entity_id
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 2, lock_entity_id)
        await async_tick_until(
            hass,
            freezer,
            lambda: (
                user_code_table.codes == E2E_SLOT_PINS
                and hass.states.get(in_sync).state == STATE_ON
            ),
            max_ticks=len(E2E_SLOT_PINS) + 2,
            what="every slot synced",
        )
        sets = [args for args in send_commands(zui_api_responder) if args[1] == "set"]

        fire_zui_node_value(
            hass, "user_code/endpoint_0/userIdStatus/2", STATUS_AVAILABLE
        )
        await async_advance_sync_ticks(hass, freezer, 3)

        assert hass.states.get(in_sync).state == STATE_ON
        code = code_entity_id(hass, lcm_config_entry, 2, lock_entity_id)
        assert hass.states.get(code).state == E2E_SLOT_PINS[2]
        assert [
            args for args in send_commands(zui_api_responder) if args[1] == "set"
        ] == sets


class TestSharedLock:
    """A lock two entries share answers for each slot from the entry that owns it."""

    async def test_a_stale_available_on_the_second_entrys_slot_keeps_its_code(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        The second entry's code is wanted on its slot, so a stale Available is ignored.

        The lock and its coordinator belong to the entry that set the lock up.
        Asked only of that entry, a slot the second entry owns wants nothing,
        so the stale Available some locks send after a code is set (#863)
        would empty the slot and sync would write the code again.
        """
        second_slot, second_pin = max(E2E_SLOT_PINS) + 1, "2468"
        second_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [zui_lock.lock.entity_id]},
            unique_id="test_zui_e2e_second",
            subentries_data=user_subentries(
                {
                    second_slot: {
                        CONF_NAME: "second",
                        CONF_PIN: second_pin,
                        CONF_ENABLED: True,
                    }
                }
            ),
        )
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        assert second_entry.runtime_data.locks[zui_lock.lock.entity_id] is zui_lock
        for _ in range(3):
            if user_code_table.codes.get(second_slot) == second_pin:
                break
            await async_advance_time(hass, TICK_INTERVAL)
        assert user_code_table.codes.get(second_slot) == second_pin
        write = user_code_call("set", [second_slot, STATUS_ENABLED, second_pin])
        writes_before = send_commands(zui_api_responder).count(write)

        fire_zui_node_value(
            hass,
            f"user_code/endpoint_0/userIdStatus/{second_slot}",
            STATUS_AVAILABLE,
        )
        await hass.async_block_till_done()
        assert held_codes(hass, zui_lock.lock.entity_id).get(second_slot) == second_pin

        for _ in range(3):
            await async_advance_time(hass, TICK_INTERVAL)
        assert send_commands(zui_api_responder).count(write) == writes_before

        await hass.config_entries.async_unload(second_entry.entry_id)
        await hass.async_block_till_done()


class TestKeypadEvents:
    """Verify keypad notifications surface as LCM code slot events."""

    async def test_keypad_unlock_reaches_the_event_entity(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        A keypad unlock names the slot that opened the lock, all the way out.

        The event entity is what automations subscribe to, so the assertion
        that matters is its state changing off unknown with the slot attached
        -- the bus event alone only proves the provider fired something.
        """
        event_entity_id = slot_entity_id(
            hass, EVENT_DOMAIN, synced_lcm_config_entry, 1, EVENT_CREDENTIAL_USED
        )
        assert hass.states.get(event_entity_id).state == STATE_UNKNOWN
        events: list[Event] = []
        hass.bus.async_listen(BUS_EVENT_CREDENTIAL_USED, events.append)

        fire_zui_node_value(
            hass,
            "notification/endpoint_0/Access_Control/Keypad_unlock_operation",
            {"userId": 1},
        )
        await hass.async_block_till_done()

        assert len(events) == 1
        assert events[0].data[ATTR_OPERATION] == CredentialOperation.UNLOCK
        state = hass.states.get(event_entity_id)
        assert state.state != STATE_UNKNOWN
        assert state.attributes[ATTR_CODE_SLOT] == 1
        assert state.attributes[ATTR_TARGET] == zui_lock.lock.entity_id


class TestApiOnlyManualGateway:
    """A lock whose state topic gives up no node still works over the api."""

    @pytest.fixture
    async def api_only_entry(
        self,
        hass: HomeAssistant,
        mqtt_mock,
        mqtt_teardown,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> AsyncGenerator[MockConfigEntry]:
        """
        Set up LCM over a lock discovered on a MANUAL gateway's custom topic.

        The topic is the user's own naming, so nothing about it names the node
        -- but the availability list still names the gateway, and the device
        identifier still names the node, which is everything the api needs.
        """
        zui_api_responder.set_handler("sendCommand", user_code_table)
        lock_entity = await async_discover_zui_lock(
            hass, state_topic="attic/front_door/state"
        )
        config = {
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: {
                slot_num: {
                    CONF_NAME: f"slot{slot_num}",
                    CONF_PIN: pin,
                    CONF_ENABLED: True,
                }
                for slot_num, pin in E2E_SLOT_PINS.items()
            },
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=config,
            unique_id="test_zui_manual",
            subentries_data=user_subentries(config[CONF_SLOTS]),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        yield entry

        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

    @staticmethod
    def _lock(entry: MockConfigEntry) -> ZWaveJSUILock:
        """Extract the single provider the entry stood up."""
        lock = next(iter(entry.runtime_data.locks.values()))
        assert isinstance(lock, ZWaveJSUILock)
        return lock

    async def test_the_lock_is_claimed_and_bound(
        self, hass: HomeAssistant, api_only_entry: MockConfigEntry
    ) -> None:
        """
        Setup accepts the lock and binds its gateway.

        Requiring the node topic for connectivity refused this lock outright,
        which cost the api half of it to save the push half that never
        worked.
        """
        lock = self._lock(api_only_entry)

        assert lock._api_base == ZUI_API_BASE
        assert await lock.async_is_integration_connected() is True

    async def test_no_node_subscription_is_attempted(
        self, hass: HomeAssistant, api_only_entry: MockConfigEntry
    ) -> None:
        """
        Setup skips the push subscription instead of failing on it.

        There is no topic to subscribe to, which is an ordinary MANUAL-gateway
        configuration rather than an error, so nothing is attempted and
        nothing raises.
        """
        lock = self._lock(api_only_entry)

        assert lock.supports_push is False
        assert lock.supports_code_slot_events is False
        assert lock._subscribed_node_topic is None
        assert not lock._push_unsubs

    async def test_the_coordinator_polls_because_push_is_unavailable(
        self, hass: HomeAssistant, api_only_entry: MockConfigEntry
    ) -> None:
        """
        Polling is the only data path left, so the coordinator has to keep one.

        This is the whole reason ``supports_push`` is derived: advertised as a
        constant True it made the coordinator drop its update interval, and a
        lock with neither push nor polling never reports anything at all.
        """
        lock = self._lock(api_only_entry)

        assert poll_interval(hass, lock) == lock.usercode_scan_interval

    async def test_configured_pins_still_reach_the_lock(
        self,
        hass: HomeAssistant,
        api_only_entry: MockConfigEntry,
        user_code_table: UserCodeTable,
    ) -> None:
        """Reads and writes go through the api exactly as they do with push."""
        for _ in range(len(E2E_SLOT_PINS) + 2):
            if user_code_table.codes == E2E_SLOT_PINS:
                break
            await async_advance_time(hass, TICK_INTERVAL)

        assert user_code_table.codes == E2E_SLOT_PINS

    async def test_unload_releases_the_api_transport(
        self, hass: HomeAssistant, api_only_entry: MockConfigEntry
    ) -> None:
        """
        Teardown reaches the api subscription even though push is off.

        The base only releases the push subscription when ``supports_push``
        says so, and this lock says no -- but its api subscription is live all
        the same and would outlive the unload.
        """
        lock = self._lock(api_only_entry)
        assert lock._api_response_topic is not None

        assert await hass.config_entries.async_unload(api_only_entry.entry_id)
        await hass.async_block_till_done()

        assert api_only_entry.state is ConfigEntryState.NOT_LOADED
        assert lock._api_response_topic is None
        assert lock._api_base is None


class TestMixedPushAndApiOnlyEntry:
    """One entry holding a push lock and an api-only lock on the same gateway."""

    @pytest.fixture
    async def mixed_entry(
        self,
        hass: HomeAssistant,
        mqtt_mock,
        mqtt_teardown,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> AsyncGenerator[MockConfigEntry]:
        """
        Set up LCM over both gateway shapes at once, on one prefix.

        The two locks differ in exactly the thing that decides their whole
        data path: the push one's discovery state topic is the gateway's own
        value topic and yields a node address, and the api-only one's is a
        MANUAL gateway's custom naming and yields nothing. They share a
        broker, a prefix, and a gateway, so everything that keeps their
        traffic apart is per-instance.
        """
        zui_api_responder.set_handler("sendCommand", _capacity_is_the_node_id)
        push_lock = await async_discover_zui_lock(hass)
        api_only_lock = await async_discover_zui_lock(
            hass,
            node_id=API_ONLY_NODE_ID,
            state_topic="attic/side_door/state",
        )
        config = {
            CONF_LOCKS: [push_lock.entity_id, api_only_lock.entity_id],
            CONF_SLOTS: {
                slot_num: {
                    CONF_NAME: f"slot{slot_num}",
                    CONF_PIN: pin,
                    CONF_ENABLED: True,
                }
                for slot_num, pin in E2E_SLOT_PINS.items()
            },
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=config,
            unique_id="test_zui_mixed",
            subentries_data=user_subentries(config[CONF_SLOTS]),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        yield entry

        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

    @staticmethod
    def _locks(entry: MockConfigEntry) -> tuple[ZWaveJSUILock, ZWaveJSUILock]:
        """Return ``(push lock, api-only lock)`` from the entry, by node id."""
        locks = list(entry.runtime_data.locks.values())
        assert len(locks) == 2
        by_node = {lock._require_node()[1]: lock for lock in locks}
        return by_node[ZUI_NODE_ID], by_node[API_ONLY_NODE_ID]

    async def test_each_lock_runs_the_mode_its_own_topic_implies(
        self, hass: HomeAssistant, mixed_entry: MockConfigEntry
    ) -> None:
        """
        Push is derived per lock, so one entry can hold both modes at once.

        A provider-wide constant would have forced them together, and either
        answer strands one of the pair: no push for the lock that has it, or
        no polling for the lock that needs it.
        """
        push_lock, api_only_lock = self._locks(mixed_entry)

        assert push_lock.supports_push is True
        assert push_lock._subscribed_node_topic == ZUI_NODE_TOPIC
        assert push_lock._push_unsubs

        assert api_only_lock.supports_push is False
        assert api_only_lock._subscribed_node_topic is None
        assert not api_only_lock._push_unsubs

    async def test_the_coordinators_run_at_the_cadence_each_mode_needs(
        self, hass: HomeAssistant, mixed_entry: MockConfigEntry
    ) -> None:
        """
        One entry, two cadences, because the coordinator asks each lock.

        The push lock's coordinator runs on no timer -- the node tells it
        when something changed -- while the api-only lock has nothing to tell
        it anything and would report forever-stale data without a poll.
        """
        push_lock, api_only_lock = self._locks(mixed_entry)

        # The push lock is only re-read on the slow drift check, if at all.
        assert poll_interval(hass, push_lock) == push_lock.hard_refresh_interval
        assert poll_interval(hass, push_lock) != push_lock.usercode_scan_interval
        assert (
            poll_interval(hass, api_only_lock) == api_only_lock.usercode_scan_interval
        )

    async def test_one_shared_wildcard_carries_both_locks_api_traffic(
        self, hass: HomeAssistant, mixed_entry: MockConfigEntry
    ) -> None:
        """
        Both locks subscribe to the same prefix wildcard and the same gateway.

        The wildcard spans every client on the prefix, so each lock's
        subscription delivers the other's responses too -- which is the point
        of correlating on a nonce rather than on the topic.
        """
        push_lock, api_only_lock = self._locks(mixed_entry)
        response_topic = f"{ZUI_PREFIX}/_CLIENTS/+/api/+"

        assert push_lock._api_response_topic == response_topic
        assert api_only_lock._api_response_topic == response_topic
        assert push_lock._api_base == api_only_lock._api_base == ZUI_API_BASE

    async def test_overlapping_calls_do_not_cross_wires(
        self,
        hass: HomeAssistant,
        mixed_entry: MockConfigEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Two calls in flight on one wildcard each get their own answer.

        Every response reaches both locks' handlers, so nothing but the
        echoed nonce distinguishes them. Both have to be waiting at once for
        that to be tested at all -- a gateway that answers each request
        before the next goes out never puts two nonces in play -- so the
        stand-in holds both requests, then answers them in the opposite
        order. A handler that resolved whichever call was waiting instead of
        the one the nonce names would hand each lock the other's number.
        """
        push_lock, api_only_lock = self._locks(mixed_entry)
        held: list[tuple[str, dict[str, Any]]] = []

        def _hold(api_base: str, request: dict[str, Any]) -> None:
            """Record the request and stay silent, keeping the call in flight."""
            held.append((api_base, request))
            return

        zui_api_responder.set_handler("sendCommand", _hold)

        capacities = asyncio.gather(
            push_lock.async_get_max_slot(), api_only_lock.async_get_max_slot()
        )
        await hass.async_block_till_done()

        assert len(held) == 2
        assert len(push_lock._pending_api_calls) == 1
        assert len(api_only_lock._pending_api_calls) == 1

        for api_base, request in reversed(held):
            async_fire_mqtt_message(
                hass,
                f"{api_base}/api/sendCommand",
                json.dumps(
                    {
                        "success": True,
                        "message": "",
                        "result": request["args"][0]["nodeId"],
                        "origin": request,
                    }
                ),
            )

        assert await capacities == [ZUI_NODE_ID, API_ONLY_NODE_ID]

    async def test_one_unload_releases_both_locks(
        self, hass: HomeAssistant, mixed_entry: MockConfigEntry
    ) -> None:
        """
        Unloading the entry has to reach a lock whose push lifecycle never ran.

        The base releases the push subscription only for a lock that reports
        ``supports_push``, so the api-only half is released purely because
        teardown reaches for the api transport by name. Miss that and every
        reload orphans a wildcard subscription holding a dead provider.
        """
        push_lock, api_only_lock = self._locks(mixed_entry)

        assert await hass.config_entries.async_unload(mixed_entry.entry_id)
        await hass.async_block_till_done()

        assert mixed_entry.state is ConfigEntryState.NOT_LOADED
        for lock in (push_lock, api_only_lock):
            assert lock._api_response_unsub is None
            assert lock._api_response_topic is None
            assert lock._api_base is None
        assert not push_lock._push_unsubs
        assert push_lock._subscribed_node_topic is None


class TestUnload:
    """Verify unloading the entry releases everything setup established."""

    async def test_unload_releases_the_gateway_and_its_subscriptions(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        Teardown drops the node subscription, the api transport, and the base.

        The resolved base is cached state that outlives nothing: a reload can
        land on a renamed gateway, so anything that ends the push lifecycle
        has to force rediscovery.
        """
        assert await hass.config_entries.async_unload(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        assert lcm_config_entry.state is ConfigEntryState.NOT_LOADED
        assert not zui_lock._push_unsubs
        assert zui_lock._subscribed_node_topic is None
        assert zui_lock._api_response_topic is None
        assert zui_lock._api_base is None


class TestAddingThroughTheUserInterface:
    """The config flow has to read the lock before it can number anybody."""

    async def test_the_flow_gets_as_far_as_naming_the_first_user(
        self,
        hass: HomeAssistant,
        zui_lock_discovered,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Choosing a zwave-js-ui lock and a user count reaches the naming step.

        Between those two submissions the flow builds a provider of its own
        and reads the lock, to find numbers nothing is already using. That
        provider never runs ``async_setup``, gets exactly one call, and is
        dropped -- so a read that defers to "the next attempt" has no next
        attempt: the flow refuses with occupancy_unknown and the lock cannot
        be added at all.
        """
        zui_api_responder.set_result("sendCommand", {"userIdStatus": STATUS_AVAILABLE})

        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]
        result = await async_configure_flow(
            hass,
            flow_id,
            {CONF_NAME: "zui", CONF_LOCKS: [zui_lock_discovered.entity_id]},
        )
        assert result["step_id"] == "choose_path"

        await async_configure_flow(hass, flow_id, {"next_step_id": "ui"})
        result = await async_configure_flow(hass, flow_id, {CONF_NUM_USERS: 2})

        assert result["step_id"] == "code_slot"
        assert result["description_placeholders"]["user_num"] == 1


_MODULE = "custom_components.lock_code_manager.providers.zwave_js_ui"


async def _async_set_up_before_discovery(
    hass: HomeAssistant, lock_entity_id: str, unique_id: str
) -> MockConfigEntry:
    """
    Set up an entry on the lock, slot 1 at 1234, before its discovery data lands.

    With no payload there is no topic prefix, so the lock reports its
    integration as not connected and its setup is deferred; once this
    returns the data is there, for the next connection check to find.
    """
    entry = current_entry(unique_id, {1: "1234"}, locks=(lock_entity_id,))
    entry.add_to_hass(hass)
    with patch(f"{_MODULE}.resolve_discovery_payload", return_value=None):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


class TestLateDiscoveryData:
    """A cold boot where Lock Code Manager is ready before the broker replays."""

    async def test_a_lock_whose_discovery_data_arrives_late_is_still_set_up(
        self,
        hass: HomeAssistant,
        zui_lock_discovered,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Setup deferred for want of discovery data has to be retried by something.

        With no payload there is no topic prefix, so the lock reports its
        integration as not connected and setup is skipped. The retry hung on
        the mqtt entry reaching LOADED, which it did before Lock Code Manager
        even started -- so nothing ever came back, the lock never validated,
        and sync refused to write a code to it for the rest of the run.
        """
        zui_api_responder.set_result("sendCommand", {"userIdStatus": STATUS_AVAILABLE})
        entry = await _async_set_up_before_discovery(
            hass, zui_lock_discovered.entity_id, "test_zui_cold_boot"
        )
        lock = next(iter(entry.runtime_data.locks.values()))
        assert lock.provider_setup_succeeded is False

        # The broker replays the discovery payload, which nothing announces:
        # the periodic connection check is the only thing that can notice.
        await async_advance_time(hass, timedelta(seconds=31))

        assert lock.provider_setup_succeeded is True
        assert lock._api_response_topic == f"{ZUI_PREFIX}/_CLIENTS/+/api/+"
        assert lock._subscribed_node_topic == ZUI_NODE_TOPIC

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


class DroppingUserCodeTable(UserCodeTable):
    """
    A node that drops sets to some slots, and whose reports for others are lost.

    A dropped set is acknowledged like any other and changes nothing. A Get
    whose report never arrives resolves with nothing (node-zwave-js
    Driver.ts:7959-7976), so the gateway answers success with no result.
    """

    def __init__(
        self,
        *,
        drops: frozenset[int] = frozenset(),
        unreadable: frozenset[int] = frozenset(),
    ) -> None:
        """Drop sets to ``drops``; lose the reports of ``unreadable``."""
        super().__init__()
        self.drops = drops
        self.unreadable = unreadable

    def __call__(self, api_base: str, request: dict[str, Any]) -> dict[str, Any]:
        """Drop or refuse the named slots; answer everything else as the table would."""
        _target, method, method_args = request["args"]
        if method == "set" and method_args[0] in self.drops:
            return {"success": True, "result": None}
        if method == "get" and method_args[0] in self.unreadable:
            return {"success": True, "result": None}
        return super().__call__(api_base, request)


class TestApiOnlyUnreadableSlot:
    """A slot a lock reached only through the api (no node topic) never reads back."""

    @pytest.fixture
    async def api_only_entry(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_mock,
        mqtt_teardown,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> AsyncGenerator[MockConfigEntry]:
        """
        Set up LCM over a lock on a MANUAL gateway's custom topic, slots 1 and 2.

        ``freezer`` is requested to freeze the clock before the entry is set
        up, as the tick helpers need.
        """
        zui_api_responder.set_handler("sendCommand", user_code_table)
        lock_entity = await async_discover_zui_lock(
            hass, state_topic="attic/front_door/state"
        )
        config = {
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: {
                slot_num: {
                    CONF_NAME: f"slot{slot_num}",
                    CONF_PIN: pin,
                    CONF_ENABLED: True,
                }
                for slot_num, pin in E2E_SLOT_PINS.items()
            },
        }
        entry = MockConfigEntry(
            domain=DOMAIN,
            data=config,
            unique_id="test_zui_manual_unreadable_slot",
            subentries_data=user_subentries(config[CONF_SLOTS]),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        yield entry

        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

    @staticmethod
    def _in_sync(hass: HomeAssistant, entry: MockConfigEntry, slot_num: int) -> str:
        """Return a slot's in-sync state on the entry's one lock."""
        [lock_entity_id] = entry.data[CONF_LOCKS]
        return hass.states.get(
            in_sync_entity_id(hass, entry, slot_num, lock_entity_id)
        ).state

    @pytest.fixture
    def user_code_table(self, request: pytest.FixtureRequest) -> UserCodeTable:
        """A fresh table from the factory each test names by parameter."""
        return request.param()

    @pytest.mark.parametrize(
        "user_code_table",
        [
            partial(
                DroppingUserCodeTable, drops=frozenset({1}), unreadable=frozenset({1})
            )
        ],
        indirect=True,
        ids=["drops_slot_1"],
    )
    async def test_a_write_it_cannot_confirm_is_trusted_and_shown_as_not_read(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        api_only_entry: MockConfigEntry,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        A write the lock dropped on a slot it never reads back reads in sync.

        A documented limit, not a bug (milestone 5, J): a lock that won't show
        a slot's code, or says nothing of it, is trusted after a write, because
        a healthy write there is indistinguishable from a dropped one. Not
        trusting it would hold every such slot and raise ``slot_suspended``
        for it. What keeps the user from mistaking trust for a reading is the
        label: the gateway answers this slot's read with no result, so the card
        is told the lock withholds the code, and marks the configured PIN as
        configured rather than read.
        """
        await async_advance_sync_ticks(hass, freezer, 15)

        [lock_entity_id] = api_only_entry.data[CONF_LOCKS]
        assert 1 not in user_code_table.codes
        assert self._in_sync(hass, api_only_entry, 1) == STATE_ON
        assert held_codes(hass, lock_entity_id)[1] == SlotCode.UNREADABLE_CODE


class DisabledUserCodeTable(UserCodeTable):
    """
    A node holding some users Disabled: their digits kept, the lock refusing them.

    A Get answers such a slot with status Disabled and its code (User Code
    Report, CC:99); a set with status Enabled re-enables it, and a clear
    frees it.
    """

    def __init__(self, disabled: dict[int, str]) -> None:
        """Hold ``disabled`` (slot -> code) Disabled."""
        super().__init__(disabled)
        self.disabled = set(disabled)

    def __call__(self, api_base: str, request: dict[str, Any]) -> dict[str, Any]:
        """Answer a Disabled slot's Get; re-enable it on a set."""
        _target, method, method_args = request["args"]
        if method == "get" and method_args[0] in self.disabled:
            return {
                "success": True,
                "result": {
                    "userIdStatus": STATUS_DISABLED,
                    "userCode": self.codes[method_args[0]],
                },
            }
        if (method == "set" and method_args[1] == STATUS_ENABLED) or method == "clear":
            self.disabled.discard(method_args[0])
        return super().__call__(api_base, request)


class TestStatusGatedPush:
    """A pushed code counts only while its slot's status says it is enabled."""

    @pytest.fixture
    def user_code_table(self) -> UserCodeTable:
        """Slot 1 holds its configured PIN, Disabled."""
        return DisabledUserCodeTable({1: E2E_SLOT_PINS[1]})

    async def test_a_disabled_slot_s_retained_code_does_not_confirm_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock_discovered: er.RegistryEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: DisabledUserCodeTable,
    ) -> None:
        """
        Regression for PR #1471: a Disabled slot's retained code confirms nothing.

        The lock holds slot 1's PIN but has the user Disabled. The gateway's
        retained values replay the slot's status, then its code; taken as the
        PIN, the code put the slot in sync, and the user was never enabled
        again.
        """
        fire_zui_node_value(hass, "99/0/userIdStatus/1", STATUS_DISABLED)
        fire_zui_node_value(hass, "99/0/userCode/1", E2E_SLOT_PINS[1])
        await hass.async_block_till_done()

        in_sync = in_sync_entity_id(
            hass, lcm_config_entry, 1, zui_lock_discovered.entity_id
        )
        await async_tick_until(
            hass,
            freezer,
            lambda: 1 not in user_code_table.disabled,
            max_ticks=20,
            what="slot 1 enabled again",
        )
        await async_tick_until(
            hass,
            freezer,
            lambda: hass.states.get(in_sync).state == STATE_ON,
            max_ticks=20,
            what="slot 1 in sync",
        )

    async def test_a_code_replayed_before_its_disabled_status_does_not_confirm_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock_discovered: er.RegistryEntry,
        user_code_table: DisabledUserCodeTable,
    ) -> None:
        """
        The broker replays retained topics in no set order, so code first too.

        The code arrives while no status has been seen, which counts as
        enabled; the Disabled status that follows only gates later codes.
        """
        fire_zui_node_value(hass, "99/0/userCode/1", E2E_SLOT_PINS[1])
        fire_zui_node_value(hass, "99/0/userIdStatus/1", STATUS_DISABLED)
        await hass.async_block_till_done()

        await async_advance_sync_ticks(hass, freezer, 20)

        assert 1 not in user_code_table.disabled


class TestLatePush:
    """A lock whose discovery data lands after setup turns out to push."""

    async def test_a_lock_that_turns_out_to_push_is_not_polled(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        zui_lock_discovered: er.RegistryEntry,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: UserCodeTable,
    ) -> None:
        """
        Regression for PR #1471: polling stops once a late lock is found to push.

        Built before its discovery data arrived, the lock kept the cadence of
        a lock with no push, and was read over the api every five minutes for
        the rest of the run.
        """
        zui_api_responder.set_handler("sendCommand", user_code_table)
        entry = await _async_set_up_before_discovery(
            hass, zui_lock_discovered.entity_id, "test_zui_late_push"
        )
        lock = next(iter(entry.runtime_data.locks.values()))
        try:
            # The connection check finds the discovery data and sets the lock up.
            await async_advance_clock(
                hass, freezer, 2 * lock.connection_check_interval.total_seconds()
            )
            await async_advance_sync_ticks(hass, freezer, 10)
            if user_code_table.codes.get(1) != "1234":
                pytest.fail("setup: the late lock was never written to")
            commands = len(send_commands(zui_api_responder))

            # Two polls' worth, and short of the hourly hard refresh, which
            # reads a pushing lock too.
            window = 2 * lock.usercode_scan_interval + timedelta(seconds=30)
            if window >= lock.hard_refresh_interval:
                pytest.fail("setup: the window reaches the hard refresh")
            await async_advance_clock(hass, freezer, window.total_seconds())

            assert len(send_commands(zui_api_responder)) == commands
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


# Access Control notification 15, as zwave-js-ui names it under the node topic.
DUPLICATE_REFUSAL = (
    "notification/endpoint_0/Access_Control/"
    "New_user_code_not_added_due_to_duplicate_code"
)
# The unmanaged slot the lock holds the duplicated PIN on, and that PIN.
HIDDEN_SLOT = 5
HIDDEN_PIN = "5555"


class DuplicateRefusingUserCodeTable(UserCodeTable):
    """
    A User Code lock that refuses a PIN another slot holds, as zwave-js-ui relays it.

    User Code Command Class has no in-band duplicate result. The set is
    acknowledged, the lock keeps the slot's old code, and its only word is
    Access Control notification 15. Meanwhile the driver announces the code
    it wrote, so the gateway publishes it on the slot as if the lock took it.
    Both arrive on the node topic before the api answers. A masking lock
    reads every code back as asterisks. Nothing shows Lock Code Manager the
    PIN on the hidden slot before the lock refuses it.

    A lock that supervises the set answers it with Fail instead, and the
    driver, writing nothing, announces nothing.
    """

    def __init__(self, hass: HomeAssistant, *, masked: bool, parameters: Any) -> None:
        """Hold ``HIDDEN_PIN`` on ``HIDDEN_SLOT``; name ``parameters`` in a refusal."""
        super().__init__({HIDDEN_SLOT: HIDDEN_PIN})
        self.hass = hass
        self.masked = masked
        self.parameters = parameters
        self.supervised = False
        self.refused: list[tuple[int, str]] = []

    def __call__(self, api_base: str, request: dict[str, Any]) -> dict[str, Any]:
        """Refuse a duplicate by notification; answer the rest as the table would."""
        _target, method, method_args = request["args"]
        if method == "set":
            slot_num, _status, code = method_args
            if any(
                held == code for other, held in self.codes.items() if other != slot_num
            ):
                self.refused.append((slot_num, code))
                if not self.supervised:
                    fire_zui_node_value(self.hass, f"99/0/userCode/{slot_num}", code)
                fire_zui_node_value(self.hass, DUPLICATE_REFUSAL, self.parameters)
                return {
                    "success": True,
                    "result": SUPERVISION_FAIL if self.supervised else None,
                }
        response = super().__call__(api_base, request)
        if (
            method == "get"
            and self.masked
            and (code := response["result"].get("userCode"))
        ):
            response["result"]["userCode"] = "*" * len(code)
        return response


class TestDuplicateRefusedByUserCodeLock:
    """
    A User Code lock behind zwave-js-ui refuses a PIN another slot holds.

    The refusal ``tests/providers/zwave_js`` covers for #1651, met over MQTT:
    the slot is turned off with a repair saying why, the PIN is sent once,
    and the code already on the lock stays where it is.
    """

    @pytest.fixture(params=["plain", "masked"])
    def code_display(self, request: pytest.FixtureRequest) -> str:
        """How the lock shows a code it holds."""
        return request.param

    @pytest.fixture(params=[None, {"userId": 0}, {"userId": 2}], ids=["none", "0", "2"])
    def parameters(self, request: pytest.FixtureRequest) -> Any:
        """What the lock names in its refusal."""
        return request.param

    @pytest.fixture
    def user_code_table(
        self, hass: HomeAssistant, code_display: str, parameters: Any
    ) -> DuplicateRefusingUserCodeTable:
        """The stand-in lock, holding the PIN slot 2 is about to be given."""
        return DuplicateRefusingUserCodeTable(
            hass, masked=code_display == "masked", parameters=parameters
        )

    @pytest.mark.parametrize("supervised", [False, True], ids=["ack", "fail"])
    async def test_a_refused_pin_disables_the_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
        user_code_table: DuplicateRefusingUserCodeTable,
        supervised: bool,
    ) -> None:
        """
        The refusal disables the slot, as it does on Z-Wave JS.

        Unheard, the acknowledged set reads as the lock taking the PIN: a
        masking lock's read-back cannot say otherwise, so the slot shows in
        sync with a PIN the lock never took. A lock that shows its codes
        reads back the old one, and the PIN is sent again and again. A
        supervising lock's Fail says only that the set failed, which is
        retried the same way.
        """
        user_code_table.supervised = supervised
        lock_entity_id = zui_lock.lock.entity_id
        entry = lcm_config_entry
        enabled_id = slot_entity_id(hass, SWITCH_DOMAIN, entry, 2, CONF_ENABLED)
        in_sync_id = in_sync_entity_id(hass, entry, 2, lock_entity_id)
        code_id = code_entity_id(hass, entry, 2, lock_entity_id)

        def slot_2() -> tuple[str, str, str]:
            """Return slot 2's enabled switch, in-sync and code states."""
            enabled, in_sync, code = (
                hass.states.get(entity_id).state
                for entity_id in (enabled_id, in_sync_id, code_id)
            )
            return enabled, in_sync, code

        await async_tick_until(
            hass,
            freezer,
            lambda: all(
                hass.states.get(
                    in_sync_entity_id(hass, entry, slot_num, lock_entity_id)
                ).state
                == STATE_ON
                for slot_num in E2E_SLOT_PINS
            ),
            max_ticks=len(E2E_SLOT_PINS) + 3,
            what="both slots in sync",
        )
        used = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
        # Slot 2 as shown from the refusal on.
        seen: list[tuple[str, str, str]] = []

        @callback
        def _record(_event: Event) -> None:
            if user_code_table.refused:
                seen.append(slot_2())

        unsub = async_track_state_change_event(
            hass, [enabled_id, in_sync_id, code_id], _record
        )

        await async_set_slot_pin(hass, entry, 2, HIDDEN_PIN)
        await async_advance_sync_ticks(hass, freezer, 10)
        unsub()

        assert slot_2() == (STATE_OFF, STATE_ON, "")
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"slot_disabled_{entry.entry_id}_2"
        )
        assert issue is not None
        assert "refused" in issue.translation_placeholders["reason"]
        # Sent once and refused once: nothing retries it.
        assert user_code_table.refused == [(2, HIDDEN_PIN)]
        assert [
            args
            for args in send_commands(zui_api_responder)
            if args[1] == "set" and args[2][2] == HIDDEN_PIN
        ] == [user_code_call("set", [2, STATUS_ENABLED, HIDDEN_PIN])]
        # The code already on the lock stays; the disabled slot wants no
        # code, so its old one is cleared.
        assert user_code_table.codes == {1: E2E_SLOT_PINS[1], HIDDEN_SLOT: HIDDEN_PIN}
        # After the refusal, neither the refused PIN nor "in sync" is shown
        # while the slot still wants that PIN.
        assert [state for state in seen if state[2] == HIDDEN_PIN] == []
        assert [state for state in seen if state[:2] == (STATE_ON, STATE_ON)] == []
        # A refusal is not a use of the slot's code.
        assert used == []

    # The slot the refusal names, when it names one, is the slot written.
    @pytest.mark.parametrize(
        "parameters", [None, {"userId": 0}, {"userId": 3}], ids=["none", "0", "3"]
    )
    async def test_a_refused_pin_is_not_what_the_slot_shows(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        The driver's announcement of a refused PIN does not stay on the slot.

        Written straight through the provider, no sync write is in flight to
        drop that announcement as an echo, so it reaches the worker as the
        lock's word. On a slot Lock Code Manager does not manage nothing
        writes over it either: unless the provider reads the slot back before
        raising the refusal, the lock is taken to hold the refused PIN there,
        and every later write of that PIN is refused unsent as a duplicate.
        """
        lock_entity_id = zui_lock.lock.entity_id
        unmanaged_slot = 3
        await async_tick_until(
            hass,
            freezer,
            lambda: lock_registry(hass).worker(lock_entity_id).started,
            max_ticks=3,
            what="worker started",
        )

        with pytest.raises(DuplicateCodeError):
            await zui_lock.async_set_usercode(unmanaged_slot, HIDDEN_PIN)
        await hass.async_block_till_done()

        assert held_codes(hass, lock_entity_id).get(unmanaged_slot) == SlotCode.EMPTY

    # A user that is no user identifier names neither the written slot nor
    # no slot: 3.7 is not slot 3.
    @pytest.mark.parametrize(
        "parameters",
        [{"userId": HIDDEN_SLOT}, {"userId": -1}, {"userId": "x"}, {"userId": 3.7}],
        ids=["5", "negative", "text", "not_whole"],
    )
    async def test_a_refusal_naming_another_slot_is_not_the_writes(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        A refusal naming a slot other than the one written is not pinned on it.

        Writes to a lock run one at a time, but a slot named outright is not
        the written one: refusing the write would guess.
        """
        await async_tick_until(
            hass,
            freezer,
            lambda: lock_registry(hass).worker(zui_lock.lock.entity_id).started,
            max_ticks=3,
            what="worker started",
        )

        assert await zui_lock.async_set_usercode(3, HIDDEN_PIN) == WriteResult.CONFIRMED
