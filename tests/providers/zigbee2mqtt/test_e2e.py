"""Full lifecycle E2E tests for Zigbee2MQTT lock provider."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
import json
import logging
from typing import Any
from unittest.mock import DEFAULT, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState, ConfigFlowResult
from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir

import custom_components.lock_code_manager as lcm
from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    ATTR_IN_SYNC,
    ATTR_OPERATION,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_SLOT,
    CONF_USERS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    SERVICE_HARD_REFRESH_USERCODES,
    SUBENTRY_TYPE_USER,
)
from custom_components.lock_code_manager.ha.adapter import ProviderPort, reading
from custom_components.lock_code_manager.ha.runtime import (
    RE_PAIRED_RELOAD,
    RE_PAIRED_WORK_DROPPED,
    STORE_KEY,
    lock_codes,
    lock_reachable,
    lock_registry,
    locks_that_cannot_report,
)
from custom_components.lock_code_manager.model.config import build_slot_unique_id
from custom_components.lock_code_manager.model.credentials import (
    SlotCode,
    SlotCredential,
    user_from_slot,
)
from custom_components.lock_code_manager.model.events import CredentialOperation
from custom_components.lock_code_manager.model.queries import subentry_id_for_slot
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)
from custom_components.lock_code_manager.repairs import async_create_fix_flow
from custom_components.lock_code_manager.sync.slot import LIMIT, Reading
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_configure_flow,
    async_configure_options,
    async_restart_lcm,
    async_run_until_quiet,
    async_step_clock_until,
    async_tick_until,
    current_entry,
    in_sync_entity_id,
    lcm_issue_ids,
    lcm_records_holding,
    lock_workers_busy,
    slot_entity_id,
    unfinished_tasks,
)

from .conftest import (
    Z2M_FULL_TOPIC,
    Z2M_GET_TOPIC,
    Z2M_IEEE,
    Z2M_SET_TOPIC,
    Z2M_TOPIC_NAME,
    async_discover_z2m_lock,
    z2m_lock_discovery_payload,
)
from .fake_bridge import ENABLED, FakeZ2MBridge

# Full LCM setup briefly holds the coordinator's debounced refresh lock while
# the initial sync runs. Concurrent async_request_refresh calls during that
# window hit the HA Debouncer regression introduced in home-assistant/core
# commit 7203cffbd73 (#153596), which orphans an extra call_later TimerHandle
# that Debouncer.async_shutdown does not cancel. Accept the lingering timer
# until the upstream fix lands.
pytestmark = pytest.mark.parametrize("expected_lingering_timers", [True])


def _published_payloads(mqtt_mock, topic: str) -> list[dict[str, Any]]:
    """
    Decode JSON payloads published to a topic via the HA MQTT client mock.

    The client mock records ``async_publish(topic, payload, qos, retain,
    message_expiry_interval=...)`` calls with topic and payload positional.
    """
    return [
        json.loads(call.args[1])
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == topic
    ]


def _record_pushes(
    hass: HomeAssistant, lock: Zigbee2MQTTLock
) -> Callable[[int], Reading | None]:
    """
    Record what the provider pushes from now on, as the core takes it.

    A push about a slot the lock's worker tracks is recorded as it reaches
    the worker; one about any other slot lands in the port's ``others``.
    Returns a lookup of the latest pushed reading per slot.
    """
    worker = lock_registry(hass).worker(lock.lock.entity_id)
    assert worker is not None
    port = worker.port
    port.others.clear()
    pushed: dict[int, Reading] = {}
    worker.push = pushed.__setitem__  # type: ignore[method-assign]

    def _seen(slot: int) -> Reading | None:
        return pushed.get(slot, port.others.get(slot))

    return _seen


def _fire_device_payload(hass: HomeAssistant, payload: dict[str, Any]) -> None:
    """Fire a JSON payload on the lock's Zigbee2MQTT device topic."""
    async_fire_mqtt_message(hass, Z2M_FULL_TOPIC, json.dumps(payload))


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Z2M provider."""

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert z2m_lock.coordinator is not None

    async def test_mqtt_subscription_established(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """The provider subscribes to the Z2M device topic during setup."""
        assert z2m_lock._subscribed_topic == Z2M_FULL_TOPIC
        assert z2m_lock._push_unsubs


class TestPushUpdatesViaMqtt:
    """Verify MQTT messages flow through to the lock's worker."""

    async def test_multiple_slots_in_single_message(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """Multiple user slots in one MQTT message all reach the core."""
        seen = _record_pushes(hass, z2m_lock)
        _fire_device_payload(
            hass,
            {
                "users": {
                    "1": {"status": "enabled", "pin_code": "1111"},
                    "2": {"status": "enabled", "pin_code": "2222"},
                    "3": {"status": "disabled"},
                }
            },
        )
        await hass.async_block_till_done()

        assert seen(1) == reading(SlotCredential.known("1111"))
        assert seen(2) == reading(SlotCredential.known("2222"))
        # Slot 3 is no slot LCM manages: a code nobody manages.
        assert seen(3) == reading(SlotCredential.unreadable())

    async def test_messages_are_taken_in_the_order_they_arrive(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """
        Each message is taken as it arrives, so none overtakes the one before.

        Taken in the executor instead, a cache republish could land behind
        the lock's answer it was sent ahead of, and undo it.
        """
        seen = _record_pushes(hass, z2m_lock)
        _fire_device_payload(
            hass, {"users": {"1": {"status": "enabled", "pin_code": "1111"}}}
        )
        assert seen(1) == reading(SlotCredential.known("1111"))
        _fire_device_payload(
            hass, {"users": {"1": {"status": "enabled", "pin_code": "2222"}}}
        )
        assert seen(1) == reading(SlotCredential.known("2222"))

    async def test_available_slot_is_empty(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """``available`` is the one status that does mean nothing is there."""
        seen = _record_pushes(hass, z2m_lock)
        _fire_device_payload(
            hass,
            {"users": {"6": {"status": "available"}}},
        )
        await hass.async_block_till_done()

        assert seen(6) == reading(SlotCredential.empty())


class TestSetAndClearUsercodes:
    """Verify set/clear operations publish correct MQTT payloads."""

    async def test_set_usercode_publishes_correct_payload(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """async_set_usercode publishes the correct SET payload."""
        await z2m_lock.async_set_usercode(1, "9999", "TestUser")

        set_publishes = _published_payloads(mqtt_mock, Z2M_SET_TOPIC)
        assert set_publishes
        assert any(
            pub.get("pin_code", {}).get("user") == 1
            and pub.get("pin_code", {}).get("pin_code") == "9999"
            and pub.get("pin_code", {}).get("user_enabled") is True
            for pub in set_publishes
        )

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """After set, the optimistic value is pushed to the worker."""
        seen = _record_pushes(hass, z2m_lock)
        await z2m_lock.async_set_usercode(1, "9999")

        assert seen(1) == reading(SlotCredential.known("9999"))

    async def test_clear_usercode_publishes_disable_payload(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """async_clear_usercode publishes user_enabled=false."""
        await z2m_lock.async_clear_usercode(1)

        set_publishes = _published_payloads(mqtt_mock, Z2M_SET_TOPIC)
        assert any(
            pub.get("pin_code", {}).get("user") == 1
            and pub.get("pin_code", {}).get("user_enabled") is False
            for pub in set_publishes
        )

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """After clear, an empty slot is pushed to the worker."""
        seen = _record_pushes(hass, z2m_lock)
        await z2m_lock.async_clear_usercode(1)

        assert seen(1) == reading(SlotCredential.empty())


class TestGetUsercodes:
    """Verify the full GET request/response cycle."""

    async def test_get_usercodes_publishes_get_requests(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """
        async_get_usercodes publishes GET requests for all managed slots.

        The auto-GET responder answers with disabled slots, so the result
        should contain EMPTY for each slot.
        """
        result = await z2m_lock.async_get_usercodes()

        get_publishes = _published_payloads(mqtt_mock, Z2M_GET_TOPIC)
        requested_slots = {pub["pin_code"]["user"] for pub in get_publishes}
        assert 1 in requested_slots
        assert 2 in requested_slots

        # Auto-responder returns user_enabled=False, so slots are EMPTY
        assert result[1] is SlotCredential.empty()
        assert result[2] is SlotCredential.empty()

    async def test_get_usercodes_with_responses(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """GET requests that receive MQTT responses return the PIN values."""

        def respond_with_pin(topic: str, payload: str, *args: Any, **kwargs: Any):
            if topic == Z2M_GET_TOPIC:
                slot = json.loads(payload)["pin_code"]["user"]
                _fire_device_payload(
                    hass,
                    {
                        "pin_code": {
                            "user": slot,
                            "user_enabled": True,
                            "pin_code": f"PIN{slot}",
                        }
                    },
                )
            return DEFAULT

        mqtt_mock.async_publish.side_effect = respond_with_pin

        result = await z2m_lock.async_get_usercodes()

        assert result[1] == SlotCredential.known("PIN1")
        assert result[2] == SlotCredential.known("PIN2")

    async def test_a_write_back_to_the_old_code_is_read_back_at_once(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """
        Two writes, no read between: only the last decides what is stale.

        The slot goes to another code and straight back. A copy of the entry
        from before the first write now agrees with what was written, so the
        lock's reply -- that same entry -- answers the read-back.
        """
        bridge = FakeZ2MBridge(hass, mqtt_mock, Z2M_FULL_TOPIC)
        bridge.install()
        bridge.set_user(1, "0000")
        assert (await z2m_lock.async_get_users([1]))[0] == user_from_slot(
            1, SlotCredential.known("0000")
        )
        await z2m_lock.async_set_usercode(1, "1111")
        await z2m_lock.async_set_usercode(1, "0000")

        async with asyncio.timeout(1):
            users = await z2m_lock.async_get_users([1])

        assert users == [user_from_slot(1, SlotCredential.known("0000"))]

    async def test_only_the_read_back_waits_past_the_old_entry(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """
        The lock drops a write: its reply is the entry from before it.

        That reply cannot be told from a copy, so the read-back hears nothing
        and times out to silence -- and the read after it takes the reply.
        """
        bridge = FakeZ2MBridge(hass, mqtt_mock, Z2M_FULL_TOPIC, drop_writes=True)
        bridge.install()
        await z2m_lock.async_get_users([1])
        await z2m_lock.async_set_usercode(1, "1111")

        read_back = await _on_the_clock(
            hass, freezer, z2m_lock.async_get_users([1]), "the read-back"
        )
        assert z2m_lock.last_silent_slots == {1}
        assert read_back == [user_from_slot(1, SlotCredential.unreadable())]
        async with asyncio.timeout(1):
            users = await z2m_lock.async_get_users([1])

        assert z2m_lock.last_silent_slots == frozenset()
        assert users == [user_from_slot(1, SlotCredential.empty())]

    async def test_a_masked_slot_written_is_read_back_at_once(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """
        A lock that will not show its codes reads the same before and after.

        A copy of the masked entry from before the write says what the reply
        says, a code that cannot be compared, so the reply answers the
        read-back rather than being waited out as one.
        """
        bridge = FakeZ2MBridge(hass, mqtt_mock, Z2M_FULL_TOPIC, expose_pin=False)
        bridge.install()
        bridge.set_user(1, "0000")
        await z2m_lock.async_get_users([1])
        await z2m_lock.async_set_usercode(1, "1111")

        async with asyncio.timeout(1):
            users = await z2m_lock.async_get_users([1])

        assert users == [user_from_slot(1, SlotCredential.unreadable())]

    async def test_a_republish_ahead_of_a_clears_read_back_answers_nothing(
        self,
        hass: HomeAssistant,
        z2m_lock,
        mqtt_mock,
    ) -> None:
        """A clear's read-back waits past a copy of the code it cleared."""
        bridge = FakeZ2MBridge(hass, mqtt_mock, Z2M_FULL_TOPIC, answer_after=0.1)
        bridge.install()
        bridge.set_user(1, "0000")
        await z2m_lock.async_get_users([1])
        await z2m_lock.async_clear_usercode(1)

        async with asyncio.timeout(1):
            users = await z2m_lock.async_get_users([1])

        assert users == [user_from_slot(1, SlotCredential.empty())]


class TestAddingThroughTheUserInterface:
    """The config flow has to read the lock before it can number anybody."""

    async def test_the_flow_gets_as_far_as_naming_the_first_user(
        self,
        hass: HomeAssistant,
        mqtt_lock_discovered,
        auto_get_responder,
    ) -> None:
        """
        Choosing a Zigbee2MQTT lock and a user count reaches the naming step.

        Between those two submissions the flow builds a provider of its own
        and reads the lock, to find numbers nothing is already using. That
        provider never runs ``async_setup``, gets exactly one call, and is
        dropped, so a read that defers to a later attempt has none: the flow
        refuses with occupancy_unknown and the lock cannot be added at all.
        """
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]
        result = await async_configure_flow(
            hass,
            flow_id,
            {CONF_NAME: "z2m", CONF_LOCKS: [mqtt_lock_discovered.entity_id]},
        )
        assert result["step_id"] == "choose_path"

        await async_configure_flow(hass, flow_id, {"next_step_id": "ui"})
        result = await async_configure_flow(hass, flow_id, {CONF_NUM_USERS: 2})

        assert result["step_id"] == "code_slot"
        assert result["description_placeholders"]["user_num"] == 1


@asynccontextmanager
async def _bridge_entry(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lock_entity_id: str,
    pins: dict[int, str] | None = None,
) -> AsyncIterator[MockConfigEntry]:
    """
    Set up an entry on the bridge's lock, slots 1 and 2 at 1234 and 5678 by default.

    Setup reads the lock, and a read the bridge leaves unanswered waits out
    its timeout on the frozen clock, so the clock is stepped until it is done.
    Unloaded after: stepped too, once whatever a failed test left waiting on
    the frozen clock is cancelled, and the test's own failure is the one
    reported.
    """
    pins = {1: "1234", 2: "5678"} if pins is None else pins
    entry = current_entry("test_z2m_bridge", pins, locks=(lock_entity_id,))
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    ent_reg = er.async_get(hass)
    # The slots' entities come from the entry's first pass, a task of its
    # own that can still be running once setup has returned.
    slot_entities = [
        (platform, build_slot_unique_id(entry.entry_id, slot, key, lock))
        for slot in pins
        for platform, key, lock in (
            (EVENT_DOMAIN, EVENT_CREDENTIAL_USED, None),
            (BINARY_SENSOR_DOMAIN, ATTR_IN_SYNC, lock_entity_id),
        )
    ]
    await async_step_clock_until(
        hass,
        freezer,
        # A setup that failed makes no entities; the assertion below reports it.
        lambda: (
            setup.done()
            and (
                not setup.result()
                or all(
                    ent_reg.async_get_entity_id(platform, DOMAIN, unique_id)
                    for platform, unique_id in slot_entities
                )
            )
        ),
        max_steps=100,
        what="entry setup and its slots' entities",
        step=1.0,
    )
    assert setup.result()
    failed = True
    try:
        yield entry
        failed = False
    finally:
        if failed:
            for task in unfinished_tasks(hass):
                task.cancel()
            await async_run_until_quiet()
        unload = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        try:
            await async_step_clock_until(
                hass, freezer, unload.done, max_steps=100, what="entry unload", step=1.0
            )
        except pytest.fail.Exception:
            if not failed:
                raise
        if not failed:
            assert unload.result()


def _in_sync(
    hass: HomeAssistant, entry: MockConfigEntry, slot_num: int, lock_entity_id: str
) -> str:
    """Return the state of one slot's in-sync entity."""
    return hass.states.get(
        in_sync_entity_id(hass, entry, slot_num, lock_entity_id)
    ).state


class TestBridge:
    """Sync against a stateful Zigbee2MQTT bridge, judged by entities and topics."""

    async def test_lost_read_backs_after_answered_writes_cost_no_try(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        A flaky mesh loses the read-back after every write (#1397 shape).

        The write went through; only its read-back was lost. The slot is
        read again, not charged a try, and in sync on that read -- written
        once, no repair -- instead of rewritten and held after its tries
        with the PIN on the lock all along.
        """
        bridge = fake_z2m_bridge
        bridge.drop_read_backs = True
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id, pins) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)

            assert [len(bridge.sets_for(slot)) for slot in pins] == [1, 1]
            worker = lock_registry(hass).worker(lock_id)
            assert all(worker.slots[slot].tries == 0 for slot in pins)
            _assert_trouble_free(hass, lock_id)

    async def test_reads_answered_through_users_settle_with_one_write_each(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        #1335: a lock that answers reads in ``users`` is written once and settles.

        Zigbee2MQTT answers a ``/get`` only through the ``users`` object, and
        republishes its whole cache on every report, the users entries as
        last read included. Before the fix every read timed out, and the
        stale ``available`` republished after each write unwound it, so the
        slots were read and reprogrammed forever.
        """
        bridge = fake_z2m_bridge
        bridge.report_after_write = True
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await async_tick_until(
                hass,
                freezer,
                lambda: all(
                    _in_sync(hass, entry, slot, lock_id) == STATE_ON for slot in (1, 2)
                ),
                max_ticks=20,
                what="both slots in sync",
            )
            gets = bridge.count("get")
            for _ in range(10):
                bridge.report()
                await async_advance_sync_ticks(hass, freezer)

            assert [len(bridge.sets_for(slot)) for slot in (1, 2)] == [1, 1]
            assert bridge.count("get") == gets
            assert bridge.users[1] == (ENABLED, "1234")
            assert bridge.users[2] == (ENABLED, "5678")
            assert all(
                _in_sync(hass, entry, slot, lock_id) == STATE_ON for slot in (1, 2)
            )

    async def test_a_republish_ahead_of_a_read_back_answers_nothing(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        A cache republish just ahead of a read-back is not its answer.

        Zigbee2MQTT republishes its whole cache, so the users entry for the
        slot just written can arrive as it was before the write, ahead of the
        lock's real reply. Taken as the read-back's answer it shows the slot
        out of sync, and the slot is written again.
        """
        bridge = fake_z2m_bridge
        bridge.answer_after = 3.0
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id, pins) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)

            assert [len(bridge.sets_for(slot)) for slot in pins] == [1, 1]
            _assert_trouble_free(hass, lock_id)

    async def test_a_dropped_write_is_read_back_as_silence_then_read(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        The lock drops every write: its reply is the slot as it was before.

        A reply the same as the pre-write entry cannot be told from a copy of
        it, so the read-back hears nothing and times out. The read after it
        takes the entry, so the slot is written again, held after its tries
        with the lock still reachable, and shown empty, as the lock says.
        """
        bridge = fake_z2m_bridge
        bridge.drop_writes = True
        bridge.answer_after = 3.0
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id, {1: "1234"}) as entry:
            await async_step_clock_until(
                hass,
                freezer,
                lambda: (
                    bool(lcm_issue_ids(hass))
                    and not unfinished_tasks(hass)
                    and not lock_workers_busy(hass)
                ),
                max_steps=900,
                what="the slot held after its tries",
                step=1.0,
            )

            assert len(bridge.sets_for(1)) == LIMIT
            assert _in_sync(hass, entry, 1, lock_id) == STATE_OFF
            assert lock_reachable(hass, lock_id) is not False
            assert lock_codes(hass, lock_id) == {1: SlotCode.EMPTY}

    async def test_a_custom_base_topic_carries_every_command(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_mock,
        mqtt_teardown,
    ) -> None:
        """
        PR #1423: a multi-level base topic gets the reads and writes, nothing else.

        The device topic comes from the lock's discovery data, so a bridge on
        ``home/z2m`` is addressed there and never on the default
        ``zigbee2mqtt`` prefix.
        """
        lock = await async_discover_z2m_lock(hass, base_topic="home/z2m")
        bridge = FakeZ2MBridge(hass, mqtt_mock, f"home/z2m/{Z2M_TOPIC_NAME}")
        bridge.install()
        try:
            async with _bridge_entry(hass, freezer, lock.entity_id) as entry:
                await async_tick_until(
                    hass,
                    freezer,
                    lambda: all(
                        _in_sync(hass, entry, slot, lock.entity_id) == STATE_ON
                        for slot in (1, 2)
                    ),
                    max_ticks=20,
                    what="both slots in sync",
                )
                assert bridge.users[1] == (ENABLED, "1234")
                assert bridge.users[2] == (ENABLED, "5678")
                # The bridge records only what reached home/z2m's device topic.
                assert bridge.count("get") > 0
                assert [len(bridge.sets_for(slot)) for slot in (1, 2)] == [1, 1]
                assert not [
                    call.args[0]
                    for call in mqtt_mock.async_publish.call_args_list
                    if call.args[0].startswith("zigbee2mqtt/")
                ]
        finally:
            bridge.uninstall()

    async def test_no_pin_reaches_a_log_record(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Regression for #917 (PRs #923, #1097), through a provider end to end.

        Sync reads and writes these PINs over MQTT; none of Lock Code
        Manager's own records may carry them. Eight digits, so that no id or
        timestamp holds them by chance.
        """
        caplog.set_level(logging.DEBUG, logger="custom_components.lock_code_manager")
        pins = {1: "73915286", 2: "60482917"}
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id, pins) as entry:
            await async_tick_until(
                hass,
                freezer,
                lambda: all(
                    _in_sync(hass, entry, slot, lock_id) == STATE_ON for slot in (1, 2)
                ),
                max_ticks=20,
                what="both slots in sync",
            )
            if fake_z2m_bridge.users[1] != (ENABLED, pins[1]):
                pytest.fail("setup: the PIN never reached the lock")

        assert lcm_records_holding(caplog, pins.values()) == []


class TestBridgeEvents:
    """Lock events from the bridge, judged by the slot event entities."""

    async def test_a_keypad_unlock_names_its_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """An operation event from the keypad reaches that slot's event entity."""
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            event_1 = slot_entity_id(
                hass, EVENT_DOMAIN, entry, 1, EVENT_CREDENTIAL_USED
            )

            fake_z2m_bridge.operation_event("unlock", 1)
            await hass.async_block_till_done()

            state = hass.states.get(event_1)
            assert state.state != STATE_UNKNOWN
            assert state.attributes[ATTR_CODE_SLOT] == 1
            assert state.attributes[ATTR_OPERATION] == CredentialOperation.UNLOCK

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({"action": ["unlock"], "action_user": 1}, id="list_action"),
            pytest.param({"action": {"unlock": 1}, "action_user": 1}, id="dict_action"),
            pytest.param({"action": "unlock", "action_user": True}, id="bool_user"),
        ],
    )
    async def test_a_malformed_event_names_no_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
        payload: dict[str, Any],
    ) -> None:
        """
        PR #1076: an unhashable ``action`` or a boolean user fires nothing.

        No Zigbee2MQTT converter sends these (``action`` is a string,
        ``action_user`` the ZCL user id); the guards are against a
        malformed payload, so the payload is fired as is. Before the fix
        the first two raised in the MQTT callback and the third named slot
        1. A real keypad unlock afterwards still lands, so the subscription
        survived.
        """
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            event_1 = slot_entity_id(
                hass, EVENT_DOMAIN, entry, 1, EVENT_CREDENTIAL_USED
            )

            _fire_device_payload(hass, payload)
            await hass.async_block_till_done()
            assert hass.states.get(event_1).state == STATE_UNKNOWN

            fake_z2m_bridge.operation_event("unlock", 1)
            await hass.async_block_till_done()
            assert hass.states.get(event_1).state != STATE_UNKNOWN


# A lock that cannot say what its slots hold (#1581) ----------------------------


@pytest.fixture(params=["silent", "cannot_tell"])
def write_only_bridge(
    request: pytest.FixtureRequest, fake_z2m_bridge: FakeZ2MBridge
) -> FakeZ2MBridge:
    """
    A bridge whose lock takes writes and never says what a slot holds.

    ``silent``: no read is ever answered. ``cannot_tell``: every read is
    answered with user status Not Supported, as #1581's converter does.
    """
    setattr(fake_z2m_bridge, request.param, True)
    return fake_z2m_bridge


async def _on_the_clock[T](
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    work: Coroutine[Any, Any, T],
    what: str,
    max_steps: int = 100,
) -> T:
    """Run ``work`` to the end, stepping the clock its silent reads wait on."""
    task = hass.async_create_task(work)
    await async_step_clock_until(
        hass, freezer, task.done, max_steps=max_steps, what=what, step=1.0
    )
    return task.result()


def _read_slots(bridge: FakeZ2MBridge) -> list[int]:
    """Return the slot of every read sent to the bridge, in order."""
    return [body["pin_code"]["user"] for s, body in bridge.requests if s == "get"]


def _settled_on(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    lock_id: str,
    bridge: FakeZ2MBridge,
    pins: dict[int, str],
) -> bool:
    """Return whether each PIN is on the lock and its slot is in sync."""
    return all(
        bridge.users.get(slot) == (ENABLED, pin)
        and _in_sync(hass, entry, slot, lock_id) == STATE_ON
        for slot, pin in pins.items()
    )


async def _until_settled(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    lock_id: str,
    bridge: FakeZ2MBridge,
    pins: dict[int, str],
) -> None:
    """Step the clock until the PINs are on and in sync, and the locks idle."""
    await async_step_clock_until(
        hass,
        freezer,
        lambda: (
            _settled_on(hass, entry, lock_id, bridge, pins)
            and not unfinished_tasks(hass)
            and not lock_workers_busy(hass)
        ),
        max_steps=300,
        what="every PIN on the lock and in sync",
        step=1.0,
    )


async def _hard_refresh(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lock_id: str,
    **data: Any,
) -> None:
    """Call the hard refresh service on ``lock_id``, stepping the clock it waits on."""
    await _on_the_clock(
        hass,
        freezer,
        hass.services.async_call(
            DOMAIN,
            SERVICE_HARD_REFRESH_USERCODES,
            data,
            target={"entity_id": lock_id},
            blocking=True,
        ),
        "hard refresh",
    )


def _assert_trouble_free(hass: HomeAssistant, lock_id: str) -> None:
    """Assert no repair, no lock taken for offline, and the lock still there."""
    assert lcm_issue_ids(hass) == []
    assert lock_reachable(hass, lock_id) is not False
    assert hass.states.get(lock_id).state != STATE_UNAVAILABLE


class TestWriteOnlyLock:
    """
    #1581: a lock that takes writes and cannot say what its slots hold.

    Whether it says nothing or answers that it cannot tell, a slot it could
    not describe is free to number and a write there is trusted. Nothing on
    the lock can be seen to be in the way, so the numbers start at 1 and no
    read goes past them.
    """

    async def test_setup_numbers_from_one_and_a_restart_writes_nothing(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        write_only_bridge: FakeZ2MBridge,
    ) -> None:
        """The flow reads only the numbers it issues; sync then settles for good."""
        bridge = write_only_bridge
        lock_id = mqtt_lock_discovered.entity_id
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]
        await async_configure_flow(
            hass, flow_id, {CONF_NAME: "write only", CONF_LOCKS: [lock_id]}
        )
        await async_configure_flow(hass, flow_id, {"next_step_id": "ui"})
        result = await _on_the_clock(
            hass,
            freezer,
            async_configure_flow(hass, flow_id, {CONF_NUM_USERS: 2}),
            "allocation",
            max_steps=30,
        )
        assert result["step_id"] == "code_slot", result.get("errors")
        # Two numbers, one read each: no walk over the lock's 250.
        assert _read_slots(bridge) == [1, 2]

        pins = {1: "1234", 2: "5678"}
        for slot, pin in pins.items():
            result = await _on_the_clock(
                hass,
                freezer,
                async_configure_flow(
                    hass,
                    flow_id,
                    {CONF_NAME: f"user{slot}", CONF_PIN: pin, CONF_ENABLED: True},
                ),
                "entry creation",
            )
        assert result["type"] == "create_entry"
        # Told on the way out that a keypad code in those slots is replaced.
        assert result["description"] == "cannot_report"
        assert result["description_placeholders"] == {"lock": lock_id, "slots": "1, 2"}
        entry = hass.config_entries.async_get_entry(result["result"].entry_id)
        assert {
            subentry.title: subentry.data[CONF_SLOT]
            for subentry in entry.subentries.values()
        } == {"user1": 1, "user2": 2}

        await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
        _assert_trouble_free(hass, lock_id)
        # The card says which: no answer, or one that it cannot tell.
        label = SlotCode.NOT_REPORTED if bridge.cannot_tell else SlotCode.UNANSWERED
        assert lock_codes(hass, lock_id) == {1: label, 2: label}
        writes = [len(bridge.sets_for(slot)) for slot in pins]
        assert writes == [1, 1]

        await _on_the_clock(
            hass,
            freezer,
            async_restart_lcm(hass, entry, settle=False),
            "restart",
        )
        await async_advance_clock(hass, freezer, 600, settle=False)
        await _until_settled(hass, freezer, entry, lock_id, bridge, pins)

        assert [len(bridge.sets_for(slot)) for slot in pins] == writes
        _assert_trouble_free(hass, lock_id)
        await _on_the_clock(
            hass,
            freezer,
            hass.config_entries.async_unload(entry.entry_id),
            "entry unload",
        )

    async def test_adding_a_user_numbers_the_next_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        write_only_bridge: FakeZ2MBridge,
    ) -> None:
        """The newcomer's number is the only one read, and their PIN lands."""
        bridge = write_only_bridge
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            flow = await hass.config_entries.subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"next_step_id": "one"}
            )
            await _until_settled(
                hass, freezer, entry, lock_id, bridge, {1: "1234", 2: "5678"}
            )
            reads_before = len(_read_slots(bridge))
            result = await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.subentries.async_configure(
                    flow["flow_id"],
                    {CONF_NAME: "user3", CONF_PIN: "9012", CONF_ENABLED: True},
                ),
                "adding a user",
            )

            assert result["type"] == "create_entry", result.get("errors")
            assert result["data"][CONF_SLOT] == 3
            # Told on the way out that a keypad code at 3 is replaced.
            assert result["description"] == "cannot_report"
            assert result["description_placeholders"] == {
                "lock": lock_id,
                "slots": "3",
            }
            assert _read_slots(bridge)[reads_before] == 3
            pins = {1: "1234", 2: "5678", 3: "9012"}
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            assert set(_read_slots(bridge)) == {1, 2, 3}
            assert [len(bridge.sets_for(slot)) for slot in pins] == [1, 1, 1]
            _assert_trouble_free(hass, lock_id)

    async def test_adding_several_users_says_the_lock_cannot_report(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        write_only_bridge: FakeZ2MBridge,
    ) -> None:
        """The block lands at 3 and 4, and the result says a keypad code there goes."""
        bridge = write_only_bridge
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            flow = await hass.config_entries.subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"next_step_id": "several"}
            )
            await _until_settled(
                hass, freezer, entry, lock_id, bridge, {1: "1234", 2: "5678"}
            )
            result = await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.subentries.async_configure(
                    flow["flow_id"],
                    {
                        CONF_USERS: {
                            "user3": {CONF_ENABLED: True, CONF_PIN: "9012"},
                            "user4": {CONF_ENABLED: True, CONF_PIN: "3456"},
                        }
                    },
                ),
                "adding users",
            )

            assert result["type"] == "abort"
            assert result["reason"] == "users_added_cannot_report"
            assert result["description_placeholders"] == {
                "num_users": "2",
                "lock": lock_id,
                "slots": "3, 4",
            }
            pins = {1: "1234", 2: "5678", 3: "9012", 4: "3456"}
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            _assert_trouble_free(hass, lock_id)

    async def test_adding_the_lock_to_an_entry_is_accepted(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        write_only_bridge: FakeZ2MBridge,
    ) -> None:
        """
        #1693 reads an added lock at the entry's numbers; this one cannot say.

        So nothing is seen in the way and the lock is added, the same answer
        allocation gives it. Refusing it with ``occupancy_unknown`` would
        keep it out of every entry for good: it will never say more.
        """
        bridge = write_only_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        entry = current_entry("test_z2m_lockless", pins, locks=())
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        flow = await hass.config_entries.options.async_init(entry.entry_id)
        result = await _on_the_clock(
            hass,
            freezer,
            async_configure_options(hass, flow["flow_id"], {CONF_LOCKS: [lock_id]}),
            "adding the lock",
        )
        # Told first that a keypad code in the entry's slots is replaced.
        assert result["step_id"] == "cannot_report", result.get("errors")
        assert result["description_placeholders"] == {"lock": lock_id, "slots": "1, 2"}
        assert entry.options == {}
        result = await _on_the_clock(
            hass,
            freezer,
            hass.config_entries.options.async_configure(flow["flow_id"], {}),
            "saving the lock",
        )

        assert result["type"] == "create_entry", result.get("errors")
        assert set(_read_slots(bridge)) == {1, 2}
        await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
        _assert_trouble_free(hass, lock_id)
        await _on_the_clock(
            hass,
            freezer,
            hass.config_entries.async_unload(entry.entry_id),
            "entry unload",
        )

    async def test_a_lock_re_paired_under_the_same_entity_id_starts_fresh(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        A lock that could report is replaced by one that cannot, same entity id.

        Re-paired under the same friendly name, the new device gets a new
        registry id and keeps the entity id. What the old one showed -- that
        it can report -- is not the new one's: its Not Supported slots are
        free, not taken, and setup says it cannot report.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id

        async def _set_up_two_users() -> ConfigFlowResult:
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_USER}
            )
            flow_id = result["flow_id"]
            await async_configure_flow(
                hass, flow_id, {CONF_NAME: "re-paired", CONF_LOCKS: [lock_id]}
            )
            await async_configure_flow(hass, flow_id, {"next_step_id": "ui"})
            return await _on_the_clock(
                hass,
                freezer,
                async_configure_flow(hass, flow_id, {CONF_NUM_USERS: 2}),
                "allocation",
                max_steps=30,
            )

        # The old device describes its empty slots.
        result = await _set_up_two_users()
        assert result["step_id"] == "code_slot", result.get("errors")
        hass.config_entries.flow.async_abort(result["flow_id"])

        # Re-paired: the old device's discovery goes, the new one's comes.
        async_fire_mqtt_message(hass, f"homeassistant/lock/{Z2M_IEEE}/lock/config", "")
        await hass.async_block_till_done()
        new_lock = await async_discover_z2m_lock(hass, ieee="0xbeef")
        assert new_lock.entity_id == lock_id
        assert new_lock.id != mqtt_lock_discovered.id
        bridge.cannot_tell = True

        # Its first read is its own: it has described nothing. (Taken for
        # the old device, every slot would be taken, and allocation would
        # walk the lock's 250 slots for nothing.)
        await _on_the_clock(
            hass, freezer, lock_registry(hass).read_once(lock_id, [1]), "a read"
        )
        assert locks_that_cannot_report(hass, [lock_id]) == [lock_id]

        result = await _set_up_two_users()
        assert result["step_id"] == "code_slot", result.get("errors")
        for user, pin in (("user1", "1234"), ("user2", "5678")):
            result = await _on_the_clock(
                hass,
                freezer,
                async_configure_flow(
                    hass,
                    result["flow_id"],
                    {CONF_NAME: user, CONF_PIN: pin, CONF_ENABLED: True},
                ),
                "entry creation",
            )
        assert result["type"] == "create_entry"
        assert result["description"] == "cannot_report"
        assert result["description_placeholders"] == {"lock": lock_id, "slots": "1, 2"}
        await _on_the_clock(
            hass,
            freezer,
            hass.config_entries.async_unload(result["result"].entry_id),
            "entry unload",
        )

    async def test_a_hard_refresh_forgets_that_the_lock_seemed_to_report(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        A lock that once described a slot, and now cannot, is asked again.

        Remembered across a restart as able to report, it would have every
        slot it answers Not Supported for taken, for good: adding users
        would walk the whole lock and fail. A hard refresh asked to forget
        that does, in memory and in the store, and the users land in the
        next slots.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            bridge.cannot_tell = True
            await _on_the_clock(
                hass,
                freezer,
                async_restart_lcm(hass, entry, settle=False),
                "restart",
            )
            await async_advance_clock(hass, freezer, 600, settle=False)
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            store = hass.data[DOMAIN][STORE_KEY]
            key = er.async_get(hass).async_get(lock_id).id
            assert store.reports(key)
            assert locks_that_cannot_report(hass, [lock_id]) == []

            await _hard_refresh(hass, freezer, lock_id, forget_reporting=True)

            assert not store.reports(key)
            assert locks_that_cannot_report(hass, [lock_id]) == [lock_id]
            flow = await hass.config_entries.subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"next_step_id": "several"}
            )
            result = await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.subentries.async_configure(
                    flow["flow_id"],
                    {
                        CONF_USERS: {
                            "user3": {CONF_ENABLED: True, CONF_PIN: "9012"},
                            "user4": {CONF_ENABLED: True, CONF_PIN: "3456"},
                        }
                    },
                ),
                "adding users",
            )

            assert result["reason"] == "users_added_cannot_report"
            assert result["description_placeholders"]["slots"] == "3, 4"
            pins = {**pins, 3: "9012", 4: "3456"}
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            _assert_trouble_free(hass, lock_id)

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            pytest.param("cannot_tell", "silent", id="cannot_tell_then_silent"),
            pytest.param(None, "cannot_tell", id="read_then_cannot_tell"),
        ],
    )
    async def test_a_read_it_answers_or_never_could_is_no_outage(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
        before: str | None,
        after: str,
    ) -> None:
        """
        Silence is an outage only from a lock shown able to answer reads.

        Not Supported is no such showing, so a lock that has only ever
        answered it, and then drops replies, is as it always was. And it is
        an answer: a lock that described its slots before and now answers
        Not Supported to every read has not gone. The hard refresh fails on
        a read either rule calls an outage.
        """
        bridge = fake_z2m_bridge
        if before is not None:
            setattr(bridge, before, True)
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            setattr(bridge, after, True)
            reads_before = len(_read_slots(bridge))

            await _hard_refresh(hass, freezer, lock_id)
            await async_advance_clock(hass, freezer, 120, settle=False)
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)

            assert len(_read_slots(bridge)) > reads_before
            assert [len(bridge.sets_for(slot)) for slot in pins] == [1, 1]
            _assert_trouble_free(hass, lock_id)


@pytest.fixture
def free_slots_only_bridge(fake_z2m_bridge: FakeZ2MBridge) -> FakeZ2MBridge:
    """A bridge whose lock reports which slots are free, and Not Supported for the rest."""
    fake_z2m_bridge.cannot_tell_codes = True
    return fake_z2m_bridge


class TestLockThatReportsOnlyFreeSlots:
    """
    A lock that answers available for an empty slot and Not Supported otherwise.

    It has shown it can report, so a slot it will not describe holds a code:
    nothing is numbered there. Sync still trusts its own writes, which read
    back Not Supported too.
    """

    async def test_setup_never_numbers_an_occupied_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        free_slots_only_bridge: FakeZ2MBridge,
    ) -> None:
        """Keypad codes at 1 and 3 push two users to 2 and 4, across two reads."""
        bridge = free_slots_only_bridge
        bridge.set_user(1, "1111")
        bridge.set_user(3, "3333")
        lock_id = mqtt_lock_discovered.entity_id
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]
        await async_configure_flow(
            hass, flow_id, {CONF_NAME: "free slots", CONF_LOCKS: [lock_id]}
        )
        await async_configure_flow(hass, flow_id, {"next_step_id": "ui"})
        result = await _on_the_clock(
            hass,
            freezer,
            async_configure_flow(hass, flow_id, {CONF_NUM_USERS: 2}),
            "allocation",
            max_steps=30,
        )
        assert result["step_id"] == "code_slot", result.get("errors")
        # Slot 3 is read by a later pass than the slot that showed the lock
        # can report: what that showed outlives the read that showed it.
        assert _read_slots(bridge) == [1, 2, 3, 4]

        for user, pin in (("user1", "1234"), ("user2", "5678")):
            result = await _on_the_clock(
                hass,
                freezer,
                async_configure_flow(
                    hass,
                    flow_id,
                    {CONF_NAME: user, CONF_PIN: pin, CONF_ENABLED: True},
                ),
                "entry creation",
            )
        assert result["type"] == "create_entry"
        # It can report: nothing to warn of.
        assert result["description"] is None
        entry = hass.config_entries.async_get_entry(result["result"].entry_id)
        assert {
            subentry.title: subentry.data[CONF_SLOT]
            for subentry in entry.subentries.values()
        } == {"user1": 2, "user2": 4}

        pins = {2: "1234", 4: "5678"}
        await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
        _assert_trouble_free(hass, lock_id)
        assert [len(bridge.sets_for(slot)) for slot in pins] == [1, 1]
        assert bridge.sets_for(1) == bridge.sets_for(3) == []
        assert bridge.users[1] == (ENABLED, "1111")
        assert bridge.users[3] == (ENABLED, "3333")
        await _on_the_clock(
            hass,
            freezer,
            hass.config_entries.async_unload(entry.entry_id),
            "entry unload",
        )

    async def test_adding_a_user_steps_over_a_keypad_code(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        free_slots_only_bridge: FakeZ2MBridge,
    ) -> None:
        """The newcomer skips the code at 3 for the free slot at 4."""
        bridge = free_slots_only_bridge
        bridge.set_user(3, "3333")
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            flow = await hass.config_entries.subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"next_step_id": "one"}
            )
            await _until_settled(
                hass, freezer, entry, lock_id, bridge, {1: "1234", 2: "5678"}
            )
            result = await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.subentries.async_configure(
                    flow["flow_id"],
                    {CONF_NAME: "user4", CONF_PIN: "9012", CONF_ENABLED: True},
                ),
                "adding a user",
            )

            assert result["type"] == "create_entry", result.get("errors")
            assert result["data"][CONF_SLOT] == 4
            # It can report: nothing to warn of.
            assert result["description"] is None
            pins = {1: "1234", 2: "5678", 4: "9012"}
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            assert bridge.sets_for(3) == []
            assert bridge.users[3] == (ENABLED, "3333")
            _assert_trouble_free(hass, lock_id)

    async def test_adding_the_lock_over_a_keypad_code_is_refused(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        free_slots_only_bridge: FakeZ2MBridge,
    ) -> None:
        """#1693: the code at the entry's slot 2 is in the way, not unknown."""
        bridge = free_slots_only_bridge
        bridge.set_user(2, "2222")
        lock_id = mqtt_lock_discovered.entity_id
        entry = current_entry("test_z2m_lockless", {1: "1234", 2: "5678"}, locks=())
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        flow = await hass.config_entries.options.async_init(entry.entry_id)
        result = await _on_the_clock(
            hass,
            freezer,
            async_configure_options(hass, flow["flow_id"], {CONF_LOCKS: [lock_id]}),
            "adding the lock",
        )

        assert result["errors"] == {"base": "existing_codes"}
        assert result["description_placeholders"]["slots"] == "2"
        assert bridge.sets_for(2) == []

    @pytest.mark.parametrize("between", ["restart", "hard_refresh"])
    async def test_the_lock_is_still_known_to_report(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        free_slots_only_bridge: FakeZ2MBridge,
        between: str,
    ) -> None:
        """
        After a restart or a hard refresh, its own slots read Not Supported.

        They show nothing, so only what was seen before -- the lock
        describing a free slot, remembered across a restart and kept by a
        plain hard refresh -- keeps the keypad code at 3 from being taken as
        free by the next user added.
        """
        bridge = free_slots_only_bridge
        bridge.set_user(3, "3333")
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            if between == "restart":
                await _on_the_clock(
                    hass,
                    freezer,
                    async_restart_lcm(hass, entry, settle=False),
                    "restart",
                )
            else:
                await _hard_refresh(hass, freezer, lock_id)
            await async_advance_clock(hass, freezer, 600, settle=False)
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)

            flow = await hass.config_entries.subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"next_step_id": "one"}
            )
            result = await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.subentries.async_configure(
                    flow["flow_id"],
                    {CONF_NAME: "user4", CONF_PIN: "9012", CONF_ENABLED: True},
                ),
                "adding a user",
            )

            assert result["type"] == "create_entry", result.get("errors")
            assert result["data"][CONF_SLOT] == 4
            # Nor is it said to be unable to report.
            assert result["description"] is None
            await _until_settled(
                hass, freezer, entry, lock_id, bridge, {**pins, 4: "9012"}
            )
            assert bridge.sets_for(3) == []
            assert bridge.users[3] == (ENABLED, "3333")
            _assert_trouble_free(hass, lock_id)


# A lock re-paired under its entity id while an entry holds it ------------------


def _re_pair(hass: HomeAssistant) -> Coroutine[Any, Any, er.RegistryEntry]:
    """
    Replace the bridge's lock by another device under the same entity id.

    The old device's discovery goes and the new one's comes in one go, as a
    Zigbee2MQTT lock re-joined under the same friendly name: the new device
    has a new registry id.
    """
    async_fire_mqtt_message(hass, f"homeassistant/lock/{Z2M_IEEE}/lock/config", "")
    return async_discover_z2m_lock(hass, ieee="0xbeef")


def _re_pair_unawaited(hass: HomeAssistant) -> None:
    """``_re_pair``, not waiting for Home Assistant to be done: something isn't."""
    async_fire_mqtt_message(hass, f"homeassistant/lock/{Z2M_IEEE}/lock/config", "")
    async_fire_mqtt_message(
        hass,
        "homeassistant/lock/0xbeef/lock/config",
        json.dumps(z2m_lock_discovery_payload(ieee="0xbeef")),
    )


def _spy_on_unloads(hass: HomeAssistant) -> Any:
    """Count the config entries Home Assistant is asked to unload."""
    return patch.object(
        hass.config_entries, "async_unload", wraps=hass.config_entries.async_unload
    )


async def _owe_a_clear_through_an_unseen_re_pair(
    hass: HomeAssistant, entry: MockConfigEntry, lock_id: str, bridge: FakeZ2MBridge
) -> Any:
    """
    Owe a clear of slot 2 to the old device, and re-pair it while the entry unloads.

    No entry holds the lock to rebuild it then, and the clear keeps the old
    worker running past the unload: return it.
    """
    old = lock_registry(hass).worker(lock_id)
    # The old device stops answering: the clear is sent, never seen done.
    bridge.silent = True
    hass.config_entries.async_remove_subentry(entry, subentry_id_for_slot(entry, 2))
    await async_run_until_quiet()
    assert old.slots[2].release
    assert old.slots[2].hold is None

    re_paired = asyncio.Event()
    unload_platforms = hass.config_entries.async_unload_platforms

    async def _unload_platforms(*args: Any) -> bool:
        await re_paired.wait()
        return await unload_platforms(*args)

    with patch.object(hass.config_entries, "async_unload_platforms", _unload_platforms):
        unload = hass.async_create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        await async_run_until_quiet()
        assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
        _re_pair_unawaited(hass)
        await async_run_until_quiet()
        re_paired.set()
        assert await unload
    await async_run_until_quiet()
    # Kept for its clear, on the old device, and nothing says so.
    assert lock_registry(hass).worker(lock_id) is old
    assert old.port.provider.lock.id != er.async_get(hass).async_get(lock_id).id
    assert lcm_issue_ids(hass) == []
    return old


class TestRePairedLock:
    """
    A lock re-paired under its entity id is a different device.

    The worker's provider is bound to the old one's registry entry, so the
    entries holding the lock set up again on the new one, and its slots are
    written to it.
    """

    async def test_the_new_device_is_set_up_and_its_slots_written(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """Not offline, the PINs written to it, and taken as it reports."""
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            old = lock_registry(hass).worker(lock_id)
            old_id = old.port.provider.lock.id
            store = hass.data[DOMAIN][STORE_KEY]
            assert store.reports(old_id)
            assert locks_that_cannot_report(hass, [lock_id]) == []

            # The new device holds nothing, and cannot say what it holds.
            assert [len(bridge.sets_for(slot)) for slot in pins] == [1, 1]
            bridge.users.clear()
            bridge.cannot_tell = True

            changed: list[bool] = []

            @callback
            def _old_worker_changes(event: Event) -> None:
                # The old worker finishing an exchange once the new device is
                # in: what it remembers stays under the old registry id.
                if event.data == {"action": "create", "entity_id": lock_id}:
                    old.on_change()
                    changed.append(old_id in store._locks)
                    # And the new entity updated as its rebuild starts: one
                    # rebuild still.
                    er.async_get(hass).async_update_entity(lock_id, name="Front door")

            unsub = hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED, _old_worker_changes
            )
            with _spy_on_unloads(hass) as unload:
                new_lock = await _re_pair(hass)
                unsub()
                assert new_lock.entity_id == lock_id
                assert changed == [True]
                assert not store.reports(new_lock.id)
                await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            assert unload.call_count == 1

            worker = lock_registry(hass).worker(lock_id)
            assert worker is not old
            assert worker.port.provider.lock.id == new_lock.id
            # Each written to the new device once.
            assert [len(bridge.sets_for(slot)) for slot in pins] == [2, 2]
            _assert_trouble_free(hass, lock_id)
            # What was kept of the old device is not the new one's, and goes
            # with its worker.
            assert not store.reports(new_lock.id)
            assert old_id not in store._locks
            assert locks_that_cannot_report(hass, [lock_id]) == [lock_id]

            # Allocation numbers the next slots, and says the lock cannot report.
            flow = await hass.config_entries.subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
            )
            flow = await hass.config_entries.subentries.async_configure(
                flow["flow_id"], {"next_step_id": "several"}
            )
            result = await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.subentries.async_configure(
                    flow["flow_id"],
                    {
                        CONF_USERS: {
                            "user3": {CONF_ENABLED: True, CONF_PIN: "9012"},
                            "user4": {CONF_ENABLED: True, CONF_PIN: "3456"},
                        }
                    },
                ),
                "adding users",
            )
            assert result["reason"] == "users_added_cannot_report"
            assert result["description_placeholders"]["slots"] == "3, 4"
            pins = {**pins, 3: "9012", 4: "3456"}
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            _assert_trouble_free(hass, lock_id)

    async def test_a_clear_owed_to_the_old_device_is_dropped(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A slot given up and still being cleared when the device is replaced.

        The clear was the old device's, which is gone: it does not keep the
        old worker running, and the new device's slot is not cleared.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            old = lock_registry(hass).worker(lock_id)
            # The old device stops answering: the clear is sent, never seen done.
            bridge.silent = True
            hass.config_entries.async_remove_subentry(
                entry, subentry_id_for_slot(entry, 2)
            )
            # Not settled: the old worker is waiting on the clock for the lock.
            await async_run_until_quiet()
            assert old.slots[2].release
            assert old.slots[2].hold is None

            # The new device answers, and has a code of its own in slot 2.
            bridge.silent = False
            bridge.users = {2: (ENABLED, "0000")}
            bridge.requests.clear()
            _re_pair_unawaited(hass)
            await async_step_clock_until(
                hass,
                freezer,
                lambda: lock_registry(hass).worker(lock_id) not in (None, old),
                max_steps=100,
                what="the lock's worker built again",
                step=1.0,
            )
            await _until_settled(hass, freezer, entry, lock_id, bridge, {1: "1234"})

            worker = lock_registry(hass).worker(lock_id)
            assert (
                worker.port.provider.lock.id == er.async_get(hass).async_get(lock_id).id
            )
            assert 2 not in worker.slots
            assert bridge.sets_for(2) == []
            assert bridge.users[2] == (ENABLED, "0000")
            assert (
                "cleared or written (a one-off PIN) when the device was replaced"
                in caplog.text
            )

            # The user is told to check the slot: it may be the same lock.
            issue_id = f"{RE_PAIRED_WORK_DROPPED}_{lock_id}"
            assert lcm_issue_ids(hass) == [issue_id]
            issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
            assert issue.translation_placeholders == {
                "lock_entity_id": lock_id,
                "slots": "2",
            }
            flow = await async_create_fix_flow(hass, issue_id, None)
            flow.hass = hass
            assert (await flow.async_step_init({}))["type"] == "create_entry"
            ir.async_delete_issue(hass, DOMAIN, issue_id)  # as Home Assistant does
            assert bridge.sets_for(2) == []
            _assert_trouble_free(hass, lock_id)

    async def test_a_second_re_pair_still_names_the_first_ones_slots(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        Re-paired twice, a clear owed each time, the first repair not acknowledged.

        The repair names both slots: the second re-pair does not replace the
        first one's slot, whose removed user's code may still be on the door.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678", 3: "9012"}
        issue_id = f"{RE_PAIRED_WORK_DROPPED}_{lock_id}"
        async with _bridge_entry(hass, freezer, lock_id, pins) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            for slot, ieee, gone in ((2, "0xbeef", Z2M_IEEE), (3, "0xcafe", "0xbeef")):
                old = lock_registry(hass).worker(lock_id)
                # A clear the device never answers, then another device.
                bridge.silent = True
                hass.config_entries.async_remove_subentry(
                    entry, subentry_id_for_slot(entry, slot)
                )
                await async_run_until_quiet()
                assert old.slots[slot].release
                bridge.silent = False
                async_fire_mqtt_message(
                    hass, f"homeassistant/lock/{gone}/lock/config", ""
                )
                async_fire_mqtt_message(
                    hass,
                    f"homeassistant/lock/{ieee}/lock/config",
                    json.dumps(z2m_lock_discovery_payload(ieee=ieee)),
                )
                await async_step_clock_until(
                    hass,
                    freezer,
                    lambda old=old: (
                        lock_registry(hass).worker(lock_id) not in (None, old)
                    ),
                    max_steps=100,
                    what="the lock's worker built again",
                    step=1.0,
                )
                await _until_settled(hass, freezer, entry, lock_id, bridge, {1: "1234"})

            issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
            assert issue.translation_placeholders == {
                "lock_entity_id": lock_id,
                "slots": "2, 3",
            }
            assert lcm_issue_ids(hass) == [issue_id]

    async def test_a_re_pair_while_the_entry_unloads_is_caught_at_its_next_setup(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        No entry is loaded to rebuild for: the old worker stops as a lock leaving.

        It remembered its slots after the new device came in. Taken for the
        new device's, the new worker would trust them and write nothing to a
        lock that cannot say what it holds: they are kept under the old
        device's registry id, and go with its worker.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            old = lock_registry(hass).worker(lock_id)
            store = hass.data[DOMAIN][STORE_KEY]
            bridge.users.clear()
            bridge.cannot_tell = True

            # The entry's unload paused before it lets go of the lock, and
            # the device re-paired meanwhile.
            re_paired = asyncio.Event()
            unload_platforms = hass.config_entries.async_unload_platforms

            async def _unload_platforms(*args: Any) -> bool:
                await re_paired.wait()
                return await unload_platforms(*args)

            with patch.object(
                hass.config_entries, "async_unload_platforms", _unload_platforms
            ):
                unload = hass.async_create_task(
                    hass.config_entries.async_unload(entry.entry_id)
                )
                await async_run_until_quiet()
                assert entry.state is ConfigEntryState.UNLOAD_IN_PROGRESS
                _re_pair_unawaited(hass)
                await async_run_until_quiet()
                new_lock = er.async_get(hass).async_get(lock_id)
                assert new_lock.id != old.port.provider.lock.id
                old_id = old.port.provider.lock.id
                old.on_change()  # still holding its slots: under its own id
                assert store.wrote(old_id, 1) is not None
                assert store.wrote(new_lock.id, 1) is None
                re_paired.set()
                assert await unload
            await hass.async_block_till_done()
            assert lock_registry(hass).worker(lock_id) is None
            # What the old device was written goes with its worker.
            assert old_id not in store._locks

            setup = hass.async_create_task(
                hass.config_entries.async_setup(entry.entry_id)
            )
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            assert await setup
            worker = lock_registry(hass).worker(lock_id)
            assert worker.port.provider.lock.id == new_lock.id
            assert [len(bridge.sets_for(slot)) for slot in pins] == [2, 2]
            _assert_trouble_free(hass, lock_id)

    async def test_a_clear_owed_through_a_re_pair_no_rebuild_saw_is_dropped_at_setup(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        The entry, set up again, takes back the old worker a clear kept: rebuilt.

        The clear is dropped with the old device, and named for the user to
        check; the new device is written every PIN, and its own code in the
        slot is not cleared.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(
                hass, freezer, entry, lock_id, bridge, {1: "1234", 2: "5678"}
            )
            old = await _owe_a_clear_through_an_unseen_re_pair(
                hass, entry, lock_id, bridge
            )

            # The new device answers, and has a code of its own in slot 2.
            bridge.silent = False
            bridge.users = {2: (ENABLED, "0000")}
            bridge.requests.clear()
            setup = hass.async_create_task(
                hass.config_entries.async_setup(entry.entry_id)
            )
            await _until_settled(hass, freezer, entry, lock_id, bridge, {1: "1234"})
            assert await setup

            worker = lock_registry(hass).worker(lock_id)
            assert worker is not old
            assert (
                worker.port.provider.lock.id == er.async_get(hass).async_get(lock_id).id
            )
            assert len(bridge.sets_for(1)) == 1
            assert bridge.sets_for(2) == []
            assert bridge.users[2] == (ENABLED, "0000")
            issue_id = f"{RE_PAIRED_WORK_DROPPED}_{lock_id}"
            assert lcm_issue_ids(hass) == [issue_id]
            issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
            assert issue.translation_placeholders == {
                "lock_entity_id": lock_id,
                "slots": "2",
            }

    async def test_a_clear_owed_through_a_re_pair_no_rebuild_saw_is_named_at_stop(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """Home Assistant stops before the entry sets up: the dropped clear is named."""
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(
                hass, freezer, entry, lock_id, bridge, {1: "1234", 2: "5678"}
            )
            await _owe_a_clear_through_an_unseen_re_pair(hass, entry, lock_id, bridge)
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await async_step_clock_until(
                hass,
                freezer,
                lambda: not unfinished_tasks(hass),
                max_steps=100,
                what="the old worker stopped",
                step=1.0,
            )
            assert lock_registry(hass).worker(lock_id) is None
            issue_id = f"{RE_PAIRED_WORK_DROPPED}_{lock_id}"
            assert lcm_issue_ids(hass) == [issue_id]
            issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
            assert issue.translation_placeholders == {
                "lock_entity_id": lock_id,
                "slots": "2",
            }

    async def test_a_re_pair_while_the_entry_sets_up_is_caught_once_it_holds_the_lock(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        Re-paired between its worker's build and the entry holding it.

        Not yet the entry's lock when the event comes, so the entry, once it
        holds the lock, finds the worker on the old device and rebuilds it.
        What the stale worker kept meanwhile is not trusted for the new device.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            store = hass.data[DOMAIN][STORE_KEY]
            bridge.users.clear()
            bridge.cannot_tell = True

            port_setup = ProviderPort.async_setup
            hold = lcm.async_hold_lock

            created = asyncio.Event()
            stale = []

            @callback
            def _created(event: Event) -> None:
                if event.data["action"] == "create":
                    created.set()

            async def _setup(port: ProviderPort, config_entry: Any) -> None:
                if not created.is_set():
                    unsub = hass.bus.async_listen(
                        er.EVENT_ENTITY_REGISTRY_UPDATED, _created
                    )
                    _re_pair_unawaited(hass)
                    await created.wait()
                    unsub()
                    assert lock_id not in config_entry.runtime_data.locks
                await port_setup(port, config_entry)

            async def _hold(*args: Any) -> Any:
                provider = await hold(*args)
                stale.append(worker := lock_registry(hass).worker(lock_id))
                if len(stale) == 1:
                    # Its slots remembered after the re-pair, before the check:
                    # under the old device's registry id, not the new one's.
                    worker.on_change()
                    assert store.wrote(worker.port.provider.lock.id, 1) is not None
                    new_id = er.async_get(hass).async_get(lock_id).id
                    assert store.wrote(new_id, 1) is None
                return provider

            with (
                patch.object(ProviderPort, "async_setup", _setup),
                patch.object(lcm, "async_hold_lock", _hold),
                _spy_on_unloads(hass) as unload,
            ):
                await _on_the_clock(
                    hass,
                    freezer,
                    hass.config_entries.async_reload(entry.entry_id),
                    "reloading",
                )
                await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            new_id = er.async_get(hass).async_get(lock_id).id
            assert stale[0].port.provider.lock.id != new_id
            # The reload's unload, then the rebuild's: once.
            assert unload.call_count == 2
            worker = lock_registry(hass).worker(lock_id)
            assert worker.port.provider.lock.id == new_id
            assert stale[1:] == [worker]  # the rebuild's hold: on the new device
            assert [len(bridge.sets_for(slot)) for slot in pins] == [2, 2]
            _assert_trouble_free(hass, lock_id)

    async def test_a_read_that_keeps_the_old_worker_asks_for_a_reload(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A one-off read holds the lock through its rebuild: the old worker stays.

        The entries take it back, so the lock is still on the old device: the
        user is told to reload, and the repair goes once the reload is done.
        """
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        issue_id = f"{RE_PAIRED_RELOAD}_{lock_id}"
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            old = lock_registry(hass).worker(lock_id)
            bridge.silent = True
            read = hass.async_create_task(lock_registry(hass).read_once(lock_id, [1]))
            await async_run_until_quiet()
            bridge.users.clear()
            with _spy_on_unloads(hass) as unload:
                _re_pair_unawaited(hass)
                await async_step_clock_until(
                    hass,
                    freezer,
                    lambda: issue_id in lcm_issue_ids(hass),
                    max_steps=10,
                    what="the rebuild giving up on the old worker",
                    step=1.0,
                )
                assert not read.done()
                assert lock_registry(hass).worker(lock_id) is old
                assert "reload Lock Code Manager to use the new device" in caplog.text

                bridge.silent = False
                await async_step_clock_until(
                    hass, freezer, read.done, max_steps=100, what="the read", step=1.0
                )
                assert read.exception() is not None  # the old device is gone
                await _until_settled(hass, freezer, entry, lock_id, bridge, {})
            # Set up again on the old worker, the entry is not rebuilt again:
            # the read would keep it again. The reload asked for is the user's.
            assert unload.call_count == 1
            assert lock_registry(hass).worker(lock_id) is old
            await _on_the_clock(
                hass,
                freezer,
                hass.config_entries.async_reload(entry.entry_id),
                "reloading",
            )
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            worker = lock_registry(hass).worker(lock_id)
            assert worker.port.provider.lock.id != old.port.provider.lock.id
            _assert_trouble_free(hass, lock_id)

    async def test_a_rename_is_no_rebuild(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """The same registry id under another entity id is the same device."""
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        entities = er.async_get(hass)
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            old = lock_registry(hass).worker(lock_id)
            with _spy_on_unloads(hass) as unload:
                entities.async_update_entity(lock_id, new_entity_id="lock.renamed")
                await hass.async_block_till_done()
                # And back, so the entry finds its lock to unload.
                entities.async_update_entity("lock.renamed", new_entity_id=lock_id)
                await hass.async_block_till_done()
            unload.assert_not_called()
            assert lock_registry(hass).worker(lock_id) is old

    async def test_a_lock_removed_is_left_as_it_was(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """Gone and not re-created: its worker, the entry and the store as before."""
        bridge = fake_z2m_bridge
        lock_id = mqtt_lock_discovered.entity_id
        pins = {1: "1234", 2: "5678"}
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            old = lock_registry(hass).worker(lock_id)
            store = hass.data[DOMAIN][STORE_KEY]
            kept = dict(store._locks)
            with _spy_on_unloads(hass) as unload:
                async_fire_mqtt_message(
                    hass, f"homeassistant/lock/{Z2M_IEEE}/lock/config", ""
                )
                await hass.async_block_till_done()
            assert er.async_get(hass).async_get(lock_id) is None
            unload.assert_not_called()
            assert lock_registry(hass).worker(lock_id) is old
            assert entry.state is ConfigEntryState.LOADED
            assert lock_id in entry.runtime_data.locks
            # Its record stays until the next start drops it (M5a).
            assert store._locks == kept

            # A new device under its entity id, later: that is a re-pair.
            bridge.users.clear()
            await async_discover_z2m_lock(hass, ieee="0xbeef")
            await _until_settled(hass, freezer, entry, lock_id, bridge, pins)
            assert lock_registry(hass).worker(lock_id) is not old
            _assert_trouble_free(hass, lock_id)
