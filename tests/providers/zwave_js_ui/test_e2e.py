"""Full lifecycle E2E tests for the zwave-js-ui lock provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
import json
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
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
    STATE_ON,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    EVENT_LOCK_STATE_CHANGED,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js_ui import ZWaveJSUILock
from tests.common import code_entity_id, in_sync_entity_id, slot_entity_id
from tests.conftest import async_advance_time

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
# zwave-js UserIDStatus: 0 Available, 1 Enabled.
STATUS_AVAILABLE = 0
STATUS_ENABLED = 1
STATUS_DISABLED = 2

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
    return {"success": True, "message": "", "result": result}


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
        return {"success": True, "message": "", "result": result}


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
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_zui_e2e")
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

    async def test_coordinator_created(
        self, hass: HomeAssistant, zui_lock: ZWaveJSUILock
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert zui_lock.coordinator is not None

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

    async def test_node_subscription_established(
        self, hass: HomeAssistant, zui_lock: ZWaveJSUILock
    ) -> None:
        """The provider subscribes to the node's value tree during setup."""
        assert zui_lock._subscribed_node_topic == ZUI_NODE_TOPIC
        assert zui_lock._push_unsubs


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


class TestPushUpdates:
    """Verify node publications reach the coordinator and the entities."""

    async def test_pushed_user_code_updates_state(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """A userCode publication is a confirmed code, no api round trip needed."""
        fire_zui_node_value(hass, "99/0/userCode/1", "4321")
        await hass.async_block_till_done()

        assert zui_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "4321"
        )
        code = hass.states.get(
            code_entity_id(hass, synced_lcm_config_entry, 1, zui_lock.lock.entity_id)
        )
        assert code is not None
        assert code.state == "4321"

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

        assert (
            zui_lock.coordinator.data.get(pin_address(unconfigured_slot))
            is SlotCredential.empty()
        )

    async def test_stale_available_does_not_unwind_a_synced_slot(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        A lock re-announcing AVAILABLE after a write must not restart sync.

        Some locks send a stale AVAILABLE once a code lands. Believed, it
        marks the slot cleared, sync rewrites it, the lock re-announces, and
        the entry never settles -- so the assertion is that a fully synced
        slot survives one.
        """
        fire_zui_node_value(
            hass, "user_code/endpoint_0/userIdStatus/2", STATUS_AVAILABLE
        )
        await hass.async_block_till_done()

        assert zui_lock.coordinator.data.get(pin_address(2)) == SlotCredential.known(
            E2E_SLOT_PINS[2]
        )

    async def test_a_disabled_slots_code_is_not_a_confirmation(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
        zui_lock: ZWaveJSUILock,
    ) -> None:
        """
        A Disabled slot keeps its digits, and they are not an active code.

        The status and the code are separate retained topics, so the push path
        has to read one against the other or it reports the same slot in sync
        while a poll of it reads unreadable.
        """
        fire_zui_node_value(
            hass, "user_code/endpoint_0/userIdStatus/1", STATUS_DISABLED
        )
        fire_zui_node_value(hass, "user_code/endpoint_0/userCode/1", "9999")
        await hass.async_block_till_done()

        assert zui_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            E2E_SLOT_PINS[1]
        )


class TestKeypadEvents:
    """Verify keypad notifications surface as LCM code slot events."""

    async def test_keypad_unlock_reaches_the_event_entity(
        self,
        hass: HomeAssistant,
        synced_lcm_config_entry: MockConfigEntry,
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
        hass.bus.async_listen(EVENT_LOCK_STATE_CHANGED, events.append)

        fire_zui_node_value(
            hass,
            "notification/endpoint_0/Access_Control/Keypad_unlock_operation",
            {"userId": 1},
        )
        await hass.async_block_till_done()

        assert [event.data[ATTR_CODE_SLOT] for event in events] == [1]
        state = hass.states.get(event_entity_id)
        assert state.state != STATE_UNKNOWN
        assert state.attributes[ATTR_CODE_SLOT] == 1
        assert state.attributes[ATTR_ACTION_TEXT] == "Keypad_unlock_operation"


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
        entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_zui_manual")
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

        assert lock.coordinator is not None
        assert lock.coordinator.update_interval == lock.usercode_scan_interval

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
        entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_zui_mixed")
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

        assert push_lock.coordinator is not None
        assert push_lock.coordinator.update_interval is None
        assert api_only_lock.coordinator is not None
        assert (
            api_only_lock.coordinator.update_interval
            == api_only_lock.usercode_scan_interval
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
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_NAME: "zui", CONF_LOCKS: [zui_lock_discovered.entity_id]},
        )
        assert result["step_id"] == "choose_path"

        await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

        assert result["step_id"] == "code_slot"
        assert result["description_placeholders"]["user_num"] == 1


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
        module = "custom_components.lock_code_manager.providers.zwave_js_ui"
        zui_api_responder.set_result("sendCommand", {"userIdStatus": STATUS_AVAILABLE})
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zui_lock_discovered.entity_id],
                CONF_SLOTS: {1: {CONF_NAME: "slot1", CONF_PIN: "1234"}},
            },
            unique_id="test_zui_cold_boot",
        )
        entry.add_to_hass(hass)

        with patch(f"{module}.resolve_discovery_payload", return_value=None):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

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
