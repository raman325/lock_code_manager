"""Full lifecycle E2E tests for Zigbee2MQTT lock provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
import logging
from typing import Any
from unittest.mock import DEFAULT

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    CONF_NAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_CODE_SLOT,
    CONF_LOCKS,
    CONF_NUM_USERS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential, SyncState
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_configure_flow,
    async_run_until_quiet,
    async_step_clock_until,
    async_tick_until,
    current_entry,
    in_sync_entity_id,
    lcm_issue_ids,
    lcm_records_holding,
    slot_entity_id,
    sync_status,
    unfinished_tasks,
)

from .conftest import (
    Z2M_FULL_TOPIC,
    Z2M_GET_TOPIC,
    Z2M_SET_TOPIC,
    Z2M_TOPIC_NAME,
    async_discover_z2m_lock,
    get_z2m_lock,
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


def _fire_device_payload(hass: HomeAssistant, payload: dict[str, Any]) -> None:
    """Fire a JSON payload on the lock's Zigbee2MQTT device topic."""
    async_fire_mqtt_message(hass, Z2M_FULL_TOPIC, json.dumps(payload))


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Z2M provider."""

    async def test_provider_discovered_as_zigbee2mqtt(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
    ) -> None:
        """Verify LCM discovers the MQTT lock and creates a Zigbee2MQTTLock."""
        lock = get_z2m_lock(lcm_config_entry)
        assert isinstance(lock, Zigbee2MQTTLock)

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
    """Verify MQTT messages flow through to the coordinator."""

    async def test_users_payload_updates_coordinator(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """Firing a users payload on the device topic updates coordinator data."""
        _fire_device_payload(
            hass,
            {"users": {"1": {"status": "enabled", "pin_code": "1234"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "1234"
        )

    async def test_multiple_slots_in_single_message(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """Multiple user slots in one MQTT message all reach the coordinator."""
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
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "1111"
        )
        assert z2m_lock.coordinator.data.get(pin_address(2)) == SlotCredential.known(
            "2222"
        )
        assert (
            z2m_lock.coordinator.data.get(pin_address(3)) is SlotCredential.unreadable()
        )

    async def test_disabled_slot_is_occupied_not_empty(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """A disabled user is one the lock is holding and refusing.

        Reporting it empty tells sync the slot is confirmed cleared and tells
        allocation the credential index is free to hand out.
        """
        _fire_device_payload(
            hass,
            {"users": {"5": {"status": "disabled"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        credential = z2m_lock.coordinator.data.get(pin_address(5))
        assert credential.is_present
        assert not credential.is_readable

    async def test_available_slot_is_empty(
        self,
        hass: HomeAssistant,
        z2m_lock,
    ) -> None:
        """``available`` is the one status that does mean nothing is there."""
        _fire_device_payload(
            hass,
            {"users": {"6": {"status": "available"}}},
        )
        await hass.async_block_till_done()
        await hass.async_block_till_done()

        assert z2m_lock.coordinator.data.get(pin_address(6)) is SlotCredential.empty()


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
        """After set, the coordinator has the optimistic value."""
        await z2m_lock.async_set_usercode(1, "9999")

        assert z2m_lock.coordinator.data.get(pin_address(1)) == SlotCredential.known(
            "9999"
        )

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
        """After clear, the coordinator has SlotCredential.empty()."""
        await z2m_lock.async_clear_usercode(1)

        assert z2m_lock.coordinator.data.get(pin_address(1)) is SlotCredential.empty()


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
    entry = current_entry(
        "test_z2m_bridge",
        {1: "1234", 2: "5678"} if pins is None else pins,
        locks=(lock_entity_id,),
    )
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    await async_step_clock_until(
        hass, freezer, setup.done, max_steps=100, what="entry setup", step=1.0
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

    async def test_a_bridge_that_never_answers_a_read_still_gets_its_writes(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        mqtt_lock_discovered,
        fake_z2m_bridge: FakeZ2MBridge,
    ) -> None:
        """
        PR #1471: a bridge whose reads go unanswered is not an unreachable lock.

        Every read times out, but the writes still go out and land, and
        nothing is suspended or reported offline: silence on reads says
        nothing about whether writes land.
        """
        bridge = fake_z2m_bridge
        bridge.silent = True
        lock_id = mqtt_lock_discovered.entity_id
        async with _bridge_entry(hass, freezer, lock_id) as entry:
            await async_advance_clock(hass, freezer, 120)
            await async_advance_sync_ticks(hass, freezer, 30)

            assert bridge.count("get") > 0
            assert bridge.users[1] == (ENABLED, "1234")
            assert bridge.users[2] == (ENABLED, "5678")
            assert lcm_issue_ids(hass) == []
            for slot in (1, 2):
                status = sync_status(
                    hass, in_sync_entity_id(hass, entry, slot, lock_id)
                )
                assert status != SyncState.SUSPENDED
            assert hass.states.get(lock_id).state != STATE_UNAVAILABLE

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
