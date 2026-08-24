"""Full lifecycle E2E tests for the zwave-js-ui lock provider."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
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
    ZWaveJSUIApiResponder,
    async_discover_zui_lock,
)

# Door Lock is 98; User Code Command Class is 99.
CC_USER_CODE_ID = 99
# zwave-js UserIDStatus: 0 Available, 1 Enabled.
STATUS_AVAILABLE = 0
STATUS_ENABLED = 1

E2E_SLOT_PINS = {1: "1234", 2: "5678"}
LOCK_CAPACITY = 20


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


def fire_node_value(hass: HomeAssistant, suffix: str, value: Any) -> None:
    """Publish a value under this lock's node topic in the gateway's envelope."""
    async_fire_mqtt_message(
        hass,
        f"{ZUI_NODE_TOPIC}/{suffix}",
        json.dumps({"time": 1700000000000, "value": value}),
    )


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
        fire_node_value(hass, "99/0/userCode/1", "4321")
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
        """Available is the one status that confirms the slot holds nothing."""
        fire_node_value(hass, "user_code/endpoint_0/userIdStatus/2", STATUS_AVAILABLE)
        await hass.async_block_till_done()

        assert zui_lock.coordinator.data.get(pin_address(2)) is SlotCredential.empty()


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

        fire_node_value(
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
