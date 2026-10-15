"""Test binary sensor platform."""

import asyncio
import copy
from datetime import timedelta
import itertools
import logging
import time
from unittest.mock import AsyncMock, PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    EVENT_STATE_CHANGED,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    ATTR_PIN_IN_SYNC,
    ATTR_SYNC_STATUS,
    CONF_LOCKS,
    CONF_SLOT,
    CONF_SLOTS,
    DOMAIN,
    SERVICE_HARD_REFRESH_USERCODES,
    SUBENTRY_TYPE_USER,
)
from custom_components.lock_code_manager.core.slot import LIMIT, Holds, Repair, Status
from custom_components.lock_code_manager.core.worker import (
    BACKOFF_AFTER,
    BACKOFF_MIN,
    LockWorker,
)
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.exceptions import (
    DuplicateCodeError,
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.lock_runtime import (
    lock_registry,
)
from custom_components.lock_code_manager.domain.locks import async_create_lock_instance
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._base import MIN_OPERATION_DELAY

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_IN_SYNC_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_PIN_ENTITY,
    TICK_INTERVAL,
    TWO_SLOTS_ON_LOCK_1,
    MockLCMLock,
    MockLCMPushLock,
    async_advance_clock,
    async_advance_sync_ticks,
    async_disable_and_reload,
    async_enable_and_reload,
    async_set_slot_pin,
    async_settle_background,
    async_setup_lcm_entry,
    async_switch_slot,
    async_tick_until,
    code_entity_id,
    in_sync_entity_id,
    mock_lock,
    pin_in_sync_entity_id,
    slot_entity_id,
    sync_status,
    write_entry_config,
)
from .conftest import (
    async_advance_time,
    async_initial_tick,
    async_trigger_sync_tick,
    get_in_sync_entity_obj,
)

_LOGGER = logging.getLogger(__name__)


def _worker(hass: HomeAssistant, lock_entity_id: str = LOCK_1_ENTITY_ID) -> LockWorker:
    """Return the worker that owns ``lock_entity_id``."""
    worker = lock_registry(hass).worker(lock_entity_id)
    assert worker is not None, f"no worker for {lock_entity_id}"
    return worker


async def _async_read_lock(
    hass: HomeAssistant, lock_entity_id: str = LOCK_1_ENTITY_ID
) -> None:
    """Have the lock's worker read it now, as a hard refresh does, then settle."""
    await _worker(hass, lock_entity_id).read()
    await hass.async_block_till_done()


async def test_binary_sensor_entity(
    hass: HomeAssistant,
    mock_calendars,
    lock_code_manager_config_entry,
):
    """Walk through calendar, usage, enable/disable, and PIN updates for slot 2."""
    # Initial calendar/active state should be off
    calendar_1, _ = mock_calendars
    state = hass.states.get("calendar.test_1")
    assert state
    assert state.state == STATE_OFF

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    # Create an event to activate the slot and verify it toggles on
    now = dt_util.utcnow()
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    cal_event = calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Removing the event should turn the slot off
    calendar_1.delete_event(cal_event.uid)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    # Adding another event turns it back on
    calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Disable/enable the slot via switch
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Change PIN and ensure provider receives the update
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    service_calls = lock_provider.service_calls
    initial_set_calls = list(service_calls.get("set_usercode", []))

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # The slot is active, so the new PIN goes to the lock as soon as it is set.
    assert service_calls.get("set_usercode", []) == [
        *initial_set_calls,
        (2, "0987", "test2"),
    ]

    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][2][CONF_CONDITION] = "calendar.test_2"

    write_entry_config(hass, lock_code_manager_config_entry, new_config)
    await hass.async_block_till_done()

    # Changing to a different calendar should deactivate the slot
    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF


async def test_startup_no_code_flapping_when_synced(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that codes aren't unnecessarily cleared/set on startup when already synced."""
    entry = lock_code_manager_config_entry
    await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    # Slot 1 is active with "1234", which every lock already holds: reading
    # it is all the start does.
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    for lock_entity_id in BASE_CONFIG[CONF_LOCKS]:
        lock = mock_lock(entry, lock_entity_id)
        assert lock.codes[1] == "1234"
        assert [
            call for call in lock.service_calls["set_usercode"] if call[0] == 1
        ] == []
        assert lock.service_calls["clear_usercode"].count((1,)) == 0


_EVERY_SLOT_ON_EVERY_LOCK = [
    (slot_num, lock_entity_id)
    for slot_num in BASE_CONFIG[CONF_SLOTS]
    for lock_entity_id in BASE_CONFIG[CONF_LOCKS]
]


async def _async_set_up_base_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up an entry with the base configuration, as the fixture does."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=copy.deepcopy(BASE_CONFIG), unique_id="Mock Title"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _async_tick_until_every_slot_in_sync(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry
) -> None:
    """Advance sync ticks until every slot reports in sync on every lock."""
    in_syncs = [
        in_sync_entity_id(hass, entry, slot_num, lock_entity_id)
        for slot_num, lock_entity_id in _EVERY_SLOT_ON_EVERY_LOCK
    ]
    await async_tick_until(
        hass,
        freezer,
        lambda: all(hass.states.get(in_sync).state == STATE_ON for in_sync in in_syncs),
        max_ticks=3,
        what="every slot in sync",
    )


@pytest.mark.parametrize("restart", [False, True], ids=["first_start", "restart"])
async def test_codes_already_on_the_lock_are_neither_cleared_nor_set_on_start(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_calendars,
    restart: bool,
):
    """
    Regression for #101: starting up leaves the codes a lock already holds alone.

    Every start cleared each slot and set it again before the slot's state
    had loaded, though the lock held exactly the codes configured. The slot
    sensors report a state throughout, never unavailable, which is how
    #358's sensors acting before they were added showed.
    """
    calendar_1, _ = mock_calendars
    now = dt_util.utcnow()
    # Slot 2's condition: active through the whole test.
    calendar_1.create_event(
        dtstart=now - timedelta(days=1), dtend=now + timedelta(days=1), summary="on"
    )
    written: list[State] = []

    @callback
    def _record(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            written.append(state)

    hass.bus.async_listen(EVENT_STATE_CHANGED, _record)
    entry = await _async_set_up_base_entry(hass)
    if restart:
        await _async_tick_until_every_slot_in_sync(hass, freezer, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        # Unloading leaves every entity unavailable; the restart starts there.
        written.clear()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    await _async_tick_until_every_slot_in_sync(hass, freezer, entry)
    await async_advance_sync_ticks(hass, freezer, 3)

    for lock_entity_id in BASE_CONFIG[CONF_LOCKS]:
        lock = mock_lock(entry, lock_entity_id)
        assert lock.codes == {1: "1234", 2: "5678"}
        assert lock.service_calls["clear_usercode"] == []
        assert lock.service_calls["set_usercode"] == []
    slot_sensors = {
        entity_id
        for slot_num, lock_entity_id in _EVERY_SLOT_ON_EVERY_LOCK
        for entity_id in (
            in_sync_entity_id(hass, entry, slot_num, lock_entity_id),
            code_entity_id(hass, entry, slot_num, lock_entity_id),
        )
    }
    assert [
        state
        for state in written
        if state.entity_id in slot_sensors and state.state == STATE_UNAVAILABLE
    ] == []

    await hass.config_entries.async_unload(entry.entry_id)


async def test_startup_detects_out_of_sync_code(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that out-of-sync codes are detected on startup and automatically corrected."""
    # Lock has "1234" but config wants "9999"
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "9999", CONF_ENABLED: True},
        },
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Out of Sync"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
    lock_provider = mock_lock(config_entry)

    # The worker reads the slot, finds "1234", and writes "9999" once.
    assert lock_provider.service_calls["set_usercode"] == [(1, "9999", "test1")]
    assert lock_provider.codes[1] == "9999"
    assert hass.states.get(in_sync_entity).state == STATE_ON

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_startup_out_of_sync_slots_sync_once(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Ensure out-of-sync slots sync once each without extra operations."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "9999", CONF_ENABLED: True},
            2: {CONF_NAME: "test2", CONF_PIN: "0000", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Multi Sync", title="Test LCM"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_syncs = [in_sync_entity_id(hass, config_entry, slot) for slot in (1, 2)]
    lock_provider = mock_lock(config_entry)

    # Both slots synced exactly once, at start.
    assert sorted(lock_provider.service_calls["set_usercode"]) == [
        (1, "9999", "test1"),
        (2, "0000", "test2"),
    ]
    assert all(hass.states.get(in_sync).state == STATE_ON for in_sync in in_syncs)

    # Asking again once in sync issues nothing more.
    for in_sync in in_syncs:
        await async_trigger_sync_tick(hass, in_sync)
    assert len(lock_provider.service_calls["set_usercode"]) == 2

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_entities_track_availability(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that entities react to lock availability and what the lock has said."""
    active_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_ACTIVE_ENTITY)
    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    assert active_entity_obj.available
    assert in_sync_entity_obj.available

    # Make lock 1 unavailable and verify per-lock entity follows
    hass.states.async_set(LOCK_1_ENTITY_ID, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert active_entity_obj.available
    assert not in_sync_entity_obj.available

    # When all locks are unavailable, the shared entity also becomes unavailable
    hass.states.async_set(LOCK_2_ENTITY_ID, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert not active_entity_obj.available

    # Restoring the lock state should re-enable availability
    hass.states.async_set(LOCK_1_ENTITY_ID, "locked")
    hass.states.async_set(LOCK_2_ENTITY_ID, "locked")
    await hass.async_block_till_done()

    assert active_entity_obj.available
    assert in_sync_entity_obj.available


_SLOT_1_CODE_ENTITY = "sensor.mock_title_test1_test_1_pin"


@pytest.mark.parametrize(
    ("status", "in_sync", "sync_attr", "code"),
    [
        pytest.param(Status.IN_SYNC, STATE_ON, "in_sync", "1234", id="in_sync"),
        pytest.param(
            Status.OUT_OF_SYNC, STATE_OFF, "out_of_sync", "9999", id="out_of_sync"
        ),
        # Being checked: the reading was set aside to read the slot again.
        pytest.param(Status.UNKNOWN, STATE_OFF, None, STATE_UNKNOWN, id="checking"),
        pytest.param(Status.SUSPENDED, STATE_OFF, "suspended", "9999", id="held"),
    ],
)
async def test_per_lock_entities_stay_available_for_every_status(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    status: Status,
    in_sync: str,
    sync_attr: str | None,
    code: str,
) -> None:
    """
    Once the lock has answered, a slot's entities show its status, never unavailable.

    A slot being checked reads off (not known in sync, as pending) with an
    unknown code: the card shows it pending, not "?" (#1700's follow-up).
    """
    await hass.async_block_till_done()
    worker = _worker(hass)
    slot = worker.slots[1]
    assert worker.answered
    if status is Status.UNKNOWN:
        slot.seen = None
    elif status is not Status.IN_SYNC:
        slot.seen = Holds("9999")
    if status is Status.SUSPENDED:
        slot.hold = Repair.CANT_SET
    assert worker.status(1)[0] is status
    worker.on_change()
    await hass.async_block_till_done()

    in_sync_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert in_sync_state.state == in_sync
    assert in_sync_state.attributes.get(ATTR_SYNC_STATUS) == sync_attr
    assert hass.states.get(_SLOT_1_CODE_ENTITY).state == code


async def test_per_lock_entities_are_unavailable_before_the_lock_answers(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A slot never read, on a lock that has not answered yet, has nothing to show."""
    await hass.async_block_till_done()
    worker = _worker(hass)
    worker.slots[1].seen = None
    with patch.object(
        LockWorker, "answered", new_callable=PropertyMock, return_value=False
    ):
        worker.on_change()
        await hass.async_block_till_done()

        assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_UNAVAILABLE
        assert hass.states.get(_SLOT_1_CODE_ENTITY).state == STATE_UNAVAILABLE


def _ticks_through_the_backoff(backoff_seconds: float) -> int:
    """
    Return the sync ticks to wait for an offline lock's next read, with headroom.

    The lock is read again on its backoff, ``backoff_seconds`` for the one it
    is in; a read failing on the way doubles it, so the budget covers that
    doubled step too.
    """
    return int(2 * backoff_seconds / TICK_INTERVAL.total_seconds()) + 3


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(
            lambda hass, entry: async_set_slot_pin(hass, entry, 1, "9999"),
            "9999",
            id="set",
        ),
        pytest.param(
            lambda hass, entry: async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF),
            None,
            id="clear",
        ),
    ],
)
async def test_a_write_a_disconnected_lock_missed_is_retried_once_it_returns(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    change,
    expected: str | None,
):
    """
    Regression for #102: a set or clear that failed on a disconnected lock is retried.

    The failed write was the last word: the slot stayed out of sync with
    the old code on the lock, however long the lock had been back.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_ON

    outage = lock.fail(None, error=LockDisconnected)
    await change(hass, entry)
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_OFF
    assert lock.codes[1] == "1234"

    outage.clear()
    # Back, the lock is read again on its poll, and the write goes out.
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=_ticks_through_the_backoff(BACKOFF_MIN),
        what="slot 1 in sync again",
    )
    assert lock.codes.get(1) == expected


async def test_an_offline_lock_holds_back_only_itself(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Regression for #1061: one unreachable lock does not disable the slot on every lock.

    A write failing on one lock switched the slot off for the whole entry,
    so the lock that was up lost the code too. The slot stays enabled, the
    lock that is up gets the new PIN, the one that is down waits out of
    sync -- an outage holds no slot -- and it gets the PIN once it is back.
    """
    entry = lock_code_manager_config_entry
    lock_1, lock_2 = (mock_lock(entry, lock_id) for lock_id in BASE_CONFIG[CONF_LOCKS])
    in_sync_1, in_sync_2 = (
        in_sync_entity_id(hass, entry, 1, lock_id)
        for lock_id in BASE_CONFIG[CONF_LOCKS]
    )
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync_2).state == STATE_ON

    outage = lock_2.fail(None, error=LockDisconnected)
    await async_set_slot_pin(hass, entry, 1, "9999")
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync_1).state == STATE_ON,
        max_ticks=3,
        what="slot 1 in sync on the lock that is up",
    )
    await async_advance_sync_ticks(hass, freezer, 3)

    assert lock_1.codes[1] == "9999"
    assert lock_2.codes[1] == "1234"
    assert sync_status(hass, in_sync_2) == Status.OUT_OF_SYNC.value
    assert hass.states.get(SLOT_1_ENABLED_ENTITY).state == STATE_ON
    assert hass.states.get(SLOT_1_ACTIVE_ENTITY).state == STATE_ON
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"slot_disabled_{entry.entry_id}_1") is None
    assert (
        issues.async_get_issue(
            DOMAIN, f"slot_suspended_{entry.entry_id}_{LOCK_2_ENTITY_ID}_1"
        )
        is None
    )

    outage.clear()
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync_2).state == STATE_ON,
        max_ticks=_ticks_through_the_backoff(2 * BACKOFF_MIN),
        what="slot 1 in sync on the lock that came back",
    )
    assert lock_2.codes[1] == "9999"
    assert lock_1.service_calls["set_usercode"] == [(1, "9999", "test1")]


async def test_coordinator_update_triggers_sync_on_external_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A refresh the provider asks for finds a code changed on the lock and puts it back.

    Someone changed the code at the lock; the provider asks its coordinator
    to refresh (as it does after a lock event or a reconnect), the worker
    reads the lock, and the sync restores the configured code.
    """
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Coordinator Sync",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
    assert hass.states.get(in_sync_entity).state == STATE_ON

    lock_provider = mock_lock(config_entry)
    service_calls = lock_provider.service_calls
    service_calls["set_usercode"].clear()

    # Simulate external change: someone changed the code on the lock to "9999"
    lock_provider.codes[1] = "9999"
    await lock_provider.coordinator.async_request_refresh()
    await hass.async_block_till_done()

    assert service_calls["set_usercode"] == [(1, "1234", "test1")]
    assert lock_provider.codes[1] == "1234"
    assert hass.states.get(in_sync_entity).state == STATE_ON

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_condition_added_to_a_slot_on_the_lock_takes_its_pin_off(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #59: a condition added to a slot already on the lock clears it.

    The user edits the slot and gives it a condition that is off. The PIN
    stayed on the lock, opening the door the condition was meant to close.
    """
    hass.states.async_set("input_boolean.access", STATE_OFF)
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True}},
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Add Condition")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    active = slot_entity_id(hass, BINARY_SENSOR_DOMAIN, entry, 1, ATTR_ACTIVE)
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_ON
    assert lock.codes[1] == "1234"

    [subentry] = entry.subentries.values()
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"],
        {
            CONF_NAME: "test1",
            CONF_PIN: "1234",
            CONF_ENABLED: True,
            CONF_CONDITION: "input_boolean.access",
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert hass.states.get(active).state == STATE_OFF

    await async_tick_until(
        hass,
        freezer,
        lambda: 1 not in lock.codes,
        max_ticks=3,
        what="slot 1 cleared",
    )
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_ON
    assert lock.service_calls["clear_usercode"] == [(1,)]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_condition_entity_subscription_updates_on_config_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that condition entity subscription updates when config entry changes."""
    # Create two input_booleans to use as condition entities
    hass.states.async_set("input_boolean.access_1", STATE_ON)
    hass.states.async_set("input_boolean.access_2", STATE_OFF)
    await hass.async_block_till_done()

    # Set up a slot with the first input_boolean as condition
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "test1",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_CONDITION: "input_boolean.access_1",
            },
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Condition Subscription",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    active_entity = slot_entity_id(
        hass, BINARY_SENSOR_DOMAIN, config_entry, 1, ATTR_ACTIVE
    )

    # Initial state: access_1 is ON, so slot should be active
    state = hass.states.get(active_entity)
    assert state.state == STATE_ON, "Slot should be active when condition entity is ON"

    # Turn off access_1 - slot should become inactive
    hass.states.async_set("input_boolean.access_1", STATE_OFF)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should be inactive when condition entity is OFF"
    )

    # Turn it back on
    hass.states.async_set("input_boolean.access_1", STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_ON, "Slot should be active when condition entity is ON"

    # Now update the config entry to use a different condition entity
    new_config = copy.deepcopy(config)
    new_config[CONF_SLOTS][1][CONF_CONDITION] = "input_boolean.access_2"

    write_entry_config(hass, config_entry, new_config)
    await hass.async_block_till_done()

    # Now the slot should be inactive because access_2 is OFF
    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should be inactive after switching to condition entity that is OFF"
    )

    # The slot should now react to access_2, NOT access_1
    # Turn on access_2 - slot should become active
    hass.states.async_set("input_boolean.access_2", STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_ON, "Slot should react to new condition entity"

    # Turn off access_2
    hass.states.async_set("input_boolean.access_2", STATE_OFF)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should react to new condition entity being OFF"
    )

    # Verify the slot does NOT react to the old condition entity anymore
    # Turn on access_1 - slot should stay inactive
    hass.states.async_set("input_boolean.access_1", STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should NOT react to old condition entity after config change"
    )

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_disables_slot_on_duplicate_code(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that sync disables slot and notifies when duplicate code is detected."""
    assert hass.states.get(SLOT_1_ACTIVE_ENTITY).state == STATE_ON
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON

    lock_provider = mock_lock(lock_code_manager_config_entry)

    # The lock refuses the new PIN as a duplicate of another slot's code.
    with patch.object(
        lock_provider,
        "async_set_usercode",
        AsyncMock(
            side_effect=DuplicateCodeError(
                code_slot=1,
                conflicting_slot=5,
                conflicting_slot_managed=False,
                lock_entity_id=LOCK_1_ENTITY_ID,
            )
        ),
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            TEXT_SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: "9999"},
            target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Slot should be disabled (enabled switch turned off)
    assert hass.states.get(SLOT_1_ENABLED_ENTITY).state == STATE_OFF
    # The "9999" code never reached the lock.
    assert lock_provider.codes.get(1) != "9999"


async def test_sync_attempts_exceeded_suspends_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A PIN the lock acknowledges but never keeps suspends the slot after the limit."""
    lock_provider = mock_lock(lock_code_manager_config_entry)
    fault = lock_provider.ignore("set", 1)

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Each write was read back and found not to have landed: LIMIT tries,
    # then the slot is held. The lock itself is still reachable.
    assert fault.hits == LIMIT
    state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert state.state == STATE_OFF
    assert state.attributes[ATTR_SYNC_STATUS] == Status.SUSPENDED.value
    assert _worker(hass).reachable
    assert lock_provider.codes.get(1) == "1234"
    fault.clear()


async def test_strikes_against_an_old_pin_do_not_suspend_the_next_one(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Regression for PR #1050: failures on a PIN that landed are not charged to the next PIN.

    Two writes of a PIN fail and the third lands. One failure of the next
    PIN then made three, and the slot was suspended for a code that had
    failed once -- reported as failing three times in a row. The sync resets
    the breaker in three places that back each other up, so only removing
    all three fails this: it guards the outcome, not any one reset.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    await async_advance_sync_ticks(hass, freezer)

    old_pin_failures = lock.fail("set", 1, times=LIMIT - 1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=2 * LIMIT + 3,
        what="the old PIN in sync",
    )
    assert old_pin_failures.hits == LIMIT - 1
    assert lock.codes[1] == "4321"

    new_pin_failure = lock.fail("set", 1, times=1)
    await async_set_slot_pin(hass, entry, 1, "5555")
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=8,
        what="the new PIN in sync",
    )

    assert new_pin_failure.hits == 1
    assert lock.codes[1] == "5555"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_unexpected_error_during_sync_suspends_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """An unexpected provider error is charged to the slot, which is suspended."""
    lock_provider = mock_lock(lock_code_manager_config_entry)

    with patch.object(
        lock_provider,
        "async_set_usercode",
        AsyncMock(side_effect=ValueError("Unexpected programming error")),
    ) as failing_set:
        await hass.services.async_call(
            TEXT_DOMAIN,
            TEXT_SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: "9999"},
            target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

    # The slot is suspended, without the lock being taken for unreachable.
    assert failing_set.await_count == LIMIT
    assert sync_status(hass, SLOT_1_IN_SYNC_ENTITY) == Status.SUSPENDED.value
    assert _worker(hass).reachable


async def test_slots_stored_under_string_keys_and_one_added_later_reach_the_lock(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for PR #955: slots stored as text, and a user added later, are synced.

    Stored configuration keys its slots by text, as JSON does, while the
    lock's codes are keyed by number. The sync looked a slot up by the text
    key, never found it, and sat showing out of sync without ever writing.
    A user added once the entry is running goes the same way. What guards
    this now is the conversion of stored text keys to numbers in
    ``domain/slot_assignment.py``; the ``int()`` casts in the sync manager
    and slot coordinator sit behind it and never see text.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                "1": {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
                "2": {CONF_NAME: "test2", CONF_PIN: "2468", CONF_ENABLED: True},
            },
        },
        unique_id="Text Keys",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)

    def _in_sync(slot_nums: list[int]) -> bool:
        return all(
            hass.states.get(in_sync_entity_id(hass, entry, slot_num)).state == STATE_ON
            for slot_num in slot_nums
        )

    await async_tick_until(
        hass, freezer, lambda: _in_sync([1, 2]), max_ticks=7, what="slots 1, 2 in sync"
    )
    assert lock.codes == {1: "1234", 2: "2468"}

    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "one"}
    )
    added = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_NAME: "test3", CONF_ENABLED: True, CONF_PIN: "3690"}
    )
    await hass.async_block_till_done()
    slot_num = added["data"][CONF_SLOT]

    await async_tick_until(
        hass,
        freezer,
        lambda: _in_sync([1, 2, slot_num]),
        max_ticks=7,
        what="the added user in sync",
    )
    assert lock.codes == {1: "1234", 2: "2468", slot_num: "3690"}

    await hass.config_entries.async_unload(entry.entry_id)


# ---------------------------------------------------------------------------
# Adversarial integration tests — exercise real component boundaries
# ---------------------------------------------------------------------------


async def test_coordinator_poll_detects_external_change_and_syncs(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Full round-trip: external code change, the lock's poll reads it, the sync sets the code back."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test External Change Poll",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
    assert hass.states.get(in_sync_entity).state == STATE_ON, "Should start in sync"

    lock_provider = mock_lock(config_entry)
    service_calls = lock_provider.service_calls
    service_calls["set_usercode"].clear()

    # Simulate external change: someone changed the code on the lock
    lock_provider.codes[1] = "9999"

    # Only the poll, at the provider's scan interval, can see it.
    await async_tick_until(
        hass,
        freezer,
        lambda: bool(service_calls["set_usercode"]),
        max_ticks=int(lock_provider.usercode_scan_interval / TICK_INTERVAL) + 2,
        what="the poll putting the code back",
    )

    assert service_calls["set_usercode"] == [(1, "1234", "test1")]
    assert lock_provider.codes[1] == "1234"
    assert hass.states.get(in_sync_entity).state == STATE_ON

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_push_update_triggers_sync_state_change_on_binary_sensor(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A pushed code that differs takes the sensor out of sync, and the sync puts it right."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Push Update",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
    assert hass.states.get(in_sync_entity).state == STATE_ON, "Should start in sync"

    lock_provider = mock_lock(config_entry)
    service_calls = lock_provider.service_calls
    service_calls["set_usercode"].clear()
    seen: list[tuple[str, str | None]] = []

    @callback
    def _record(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            seen.append((state.state, state.attributes.get(ATTR_SYNC_STATUS)))

    unsub = async_track_state_change_event(hass, in_sync_entity, _record)
    # Someone changed the code at the lock, and the lock pushes it.
    lock_provider.codes[1] = "wrong"
    lock_provider._push_credential_update(1, SlotCredential.known("wrong"))
    await hass.async_block_till_done()
    unsub()

    assert (STATE_OFF, "syncing") in seen, seen
    assert service_calls["set_usercode"] == [(1, "1234", "test1")]
    assert lock_provider.codes[1] == "1234"
    assert hass.states.get(in_sync_entity).state == STATE_ON

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_slot_suspension_isolated_from_other_slots(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A generic error on one slot suspends only that slot, not its siblings or the lock."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "test2", CONF_PIN: "2222", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Suspension Isolation",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    real_set = MockLCMLock.async_set_usercode

    async def _set(self: MockLCMLock, code_slot: int, usercode: str, *args, **kwargs):
        # Only slot 2's write hits an unexpected error.
        if code_slot == 2:
            raise ValueError("Unexpected hardware error")
        return await real_set(self, code_slot, usercode, *args, **kwargs)

    with patch.object(MockLCMLock, "async_set_usercode", _set):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    in_sync_slot_1 = in_sync_entity_id(hass, config_entry, 1)
    in_sync_slot_2 = in_sync_entity_id(hass, config_entry, 2)
    # Slot 2 is suspended; slot 1 and the lock itself are unaffected.
    assert sync_status(hass, in_sync_slot_2) == Status.SUSPENDED.value
    assert hass.states.get(in_sync_slot_1).state == STATE_ON
    assert _worker(hass).reachable

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_drift_check_detects_external_change_and_triggers_sync(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A hard refresh detects an out-of-band code change, and the sync re-syncs it."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Drift Check",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
    assert hass.states.get(in_sync_entity).state == STATE_ON, "Should start in sync"

    lock_provider = mock_lock(config_entry)
    service_calls = lock_provider.service_calls
    service_calls["set_usercode"].clear()

    # Simulate external change (keypad programming)
    lock_provider.codes[1] = "5555"

    await hass.services.async_call(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert service_calls["set_usercode"] == [(1, "1234", "test1")]
    assert lock_provider.codes[1] == "1234"
    assert hass.states.get(in_sync_entity).state == STATE_ON

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_code_changed_where_only_the_drift_check_sees_it_is_put_back(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
):
    """
    Regression for #737: a change the drift check reads off the lock is synced back.

    A push lock that never pushed the change is only read again by its drift
    check. What that read found reached the slot's code sensor, but nothing
    told the sync, so a slot in sync stayed in sync with the wrong code on
    the lock.
    """
    drift_interval = timedelta(minutes=1)
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True}},
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Drift Check")
    entry.add_to_hass(hass)
    with (
        patch.dict(
            "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
            {"test": MockLCMPushLock},
        ),
        patch.object(
            MockLCMPushLock,
            "hard_refresh_interval",
            new_callable=PropertyMock,
            return_value=drift_interval,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        lock = mock_lock(entry)
        in_sync = in_sync_entity_id(hass, entry, 1)
        code = code_entity_id(hass, entry, 1)
        await async_tick_until(
            hass,
            freezer,
            lambda: hass.states.get(in_sync).state == STATE_ON,
            max_ticks=3,
            what="slot 1 in sync",
        )

        # Programmed at the keypad, and the lock pushes nothing about it.
        lock.codes[1] = "5555"
        await async_advance_sync_ticks(hass, freezer, 3)
        assert hass.states.get(code).state == "1234"
        in_sync_states: list[str] = []

        @callback
        def _record(event: Event[EventStateChangedData]) -> None:
            if (state := event.data["new_state"]) is not None:
                in_sync_states.append(state.state)

        unsub = async_track_state_change_event(hass, in_sync, _record)
        reads_before = len(lock.service_calls["get_usercodes"])
        # The drift check and the sync it sets off can land within one tick.
        await async_tick_until(
            hass,
            freezer,
            lambda: bool(lock.service_calls["set_usercode"]),
            max_ticks=int(drift_interval / TICK_INTERVAL) + 1,
            what="the change put back",
        )
        await async_advance_sync_ticks(hass, freezer)
        unsub()

        assert STATE_OFF in in_sync_states
        assert hass.states.get(in_sync).state == STATE_ON
        assert lock.codes[1] == "1234"
        assert lock.service_calls["set_usercode"] == [(1, "1234", "test1")]
        assert len(lock.service_calls["get_usercodes"]) > reads_before

        await hass.config_entries.async_unload(entry.entry_id)


async def test_async_advance_time_drains_background_tick_chain(
    hass: HomeAssistant,
    mock_lock_config_entry,
) -> None:
    """``async_advance_time`` settles a background tick whose mocks yield.

    ``async_track_time_interval`` (used by the sync tick and coordinator
    update) runs its callback as a background task. The bare
    ``async_fire_time_changed + async_block_till_done`` pair does not
    wait for background tasks, so any tick whose chain spans more than
    one event-loop turn can outlive the wait. This is masked today only
    because the default mock returns synchronously; the moment a mock
    or a real provider yields, the bare pattern stops settling. This
    test forces the issue by patching the connection check to yield,
    and asserts ``async_advance_time`` still settles correctly.
    """
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {1: {CONF_NAME: "t1", CONF_PIN: "1234", CONF_ENABLED: True}},
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="advance_time_test", title="Test"
    )
    config_entry.add_to_hass(hass)

    # Reproducing the race requires several mock methods to yield so the
    # tick chain spans more than one event-loop turn -- a single yield
    # happens to slip through the bare ``block_till_done``.
    real_set = MockLCMLock.async_set_usercode
    real_get = MockLCMLock.async_get_usercodes
    real_connected = MockLCMLock.async_is_integration_connected

    async def _yielding_set(self: MockLCMLock, *args, **kwargs) -> bool:
        await asyncio.sleep(0)
        return await real_set(self, *args, **kwargs)

    async def _yielding_get(self: MockLCMLock, slots=None) -> dict:
        await asyncio.sleep(0)
        return await real_get(self, slots)

    async def _yielding_connected(self: MockLCMLock) -> bool:
        await asyncio.sleep(0)
        return await real_connected(self)

    with (
        patch.object(MockLCMLock, "async_set_usercode", _yielding_set),
        patch.object(MockLCMLock, "async_get_usercodes", _yielding_get),
        patch.object(
            MockLCMLock, "async_is_integration_connected", _yielding_connected
        ),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
        await async_initial_tick(hass, in_sync_entity)

        lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        lock_provider.service_calls.get("set_usercode", []).clear()
        lock_provider.codes[1] = "9999"
        await lock_provider.coordinator.async_request_refresh()
        await hass.async_block_till_done()

        await async_advance_time(hass, TICK_INTERVAL * 2)

        assert len(lock_provider.service_calls["set_usercode"]) == 1

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_manager_handles_code_sensor_unknown_state_on_startup(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    The in-sync sensor waits, unavailable, while the lock's reads fail.

    It settles, in sync, on the first read that answers.
    """
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    real_get = MockLCMLock.async_get_usercodes

    async def _failing_get_usercodes(self_lock, slots=None):
        raise LockDisconnected("Connection failed")

    with patch.object(MockLCMLock, "async_get_usercodes", _failing_get_usercodes):
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=config,
            unique_id="Test Unknown State",
            title="Test LCM",
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    in_sync_entity = in_sync_entity_id(hass, config_entry, 1)
    lock_provider = mock_lock(config_entry)

    # Nothing has been heard about the slot: no status, nothing written, and
    # the lock is backed off.
    assert hass.states.get(in_sync_entity).state == STATE_UNAVAILABLE
    assert lock_provider.service_calls["set_usercode"] == []
    assert not _worker(hass).reachable

    # The lock answers again: the probe after the backoff reads it.
    assert MockLCMLock.async_get_usercodes is real_get
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync_entity).state == STATE_ON,
        max_ticks=_ticks_through_the_backoff(BACKOFF_MIN),
        what="slot 1 in sync once the lock answers",
    )

    # The lock already holds the right code.
    assert hass.states.get(in_sync_entity).state == STATE_ON
    assert lock_provider.service_calls["set_usercode"] == []

    await hass.config_entries.async_unload(config_entry.entry_id)


# ---------------------------------------------------------------------------
# Race condition and timing tests
# ---------------------------------------------------------------------------


async def test_push_update_during_sync_operation_does_not_corrupt_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A push about the slot while its write is out does not undo the sync.

    The lock reports a different code while the write is in flight; the
    report is older than the write and is dropped, and the slot ends in
    sync with the written code.
    """
    lock_provider = mock_lock(lock_code_manager_config_entry)
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    fault = lock_provider.hang("set", 1)

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await fault.async_wait_held()
    assert sync_status(hass, SLOT_1_IN_SYNC_ENTITY) == "syncing"

    lock_provider._push_credential_update(1, SlotCredential.known("7777"))
    assert sync_status(hass, SLOT_1_IN_SYNC_ENTITY) == "syncing"

    fault.clear()
    await hass.async_block_till_done()

    assert lock_provider.codes[1] == "9999"
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    assert lock_provider.service_calls["set_usercode"][-1] == (1, "9999", "test1")


async def test_sync_manager_stop_during_active_sync_does_not_raise(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Unloading while a write never answers does not raise, and does not hang.

    The lock's worker gives the write its grace, then cuts it off; the
    entry unloads cleanly and the code on the lock is left as it was.
    """
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Unload During Sync"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await async_settle_background(hass, freezer)

    lock_provider = mock_lock(config_entry)
    fault = lock_provider.hang("set", 1)
    await async_set_slot_pin(hass, config_entry, 1, "9999")
    await fault.async_wait_held()

    unload = hass.async_create_task(
        hass.config_entries.async_unload(config_entry.entry_id)
    )
    await async_advance_clock(hass, freezer, 60, settle=False)

    assert unload.done()
    assert unload.result() is True
    assert not fault.held
    assert lock_provider.codes[1] == "1234"


async def test_pin_change_during_sync_uses_snapshot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A PIN changed while the old one is being written ends with the new one.

    The write in flight carries the PIN wanted when it was sent; once it is
    back, the slot is read and the new PIN written.
    """
    lock_provider = mock_lock(lock_code_manager_config_entry)
    fault = lock_provider.hang("set", 1, times=1)

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "5555"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await fault.async_wait_held()

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "4321"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    fault.clear()
    await hass.async_block_till_done()

    assert lock_provider.service_calls["set_usercode"] == [
        (1, "5555", "test1"),
        (1, "4321", "test1"),
    ]
    assert lock_provider.codes[1] == "4321"
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON


# Full LCM setup briefly holds the coordinator's debounced refresh lock while
# the initial sync runs; the HA Debouncer regression from core commit
# 7203cffbd73 (#153596) then orphans an extra call_later TimerHandle that
# Debouncer.async_shutdown does not cancel. Accept the lingering timer for
# this single full-setup test until the upstream fix lands.
async def test_multiple_slots_sync_sequentially_not_concurrently(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Two slots out of sync never have their writes at the lock at once.

    The lock's worker owns every exchange with it, one at a time.
    """
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "9999", CONF_ENABLED: True},
            2: {CONF_NAME: "test2", CONF_PIN: "8888", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Sequential Sync",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)

    # Track the order and overlap of set_usercode calls
    call_log = []
    active_calls = {"count": 0, "max_concurrent": 0}
    original_set = MockLCMLock.async_set_usercode

    async def tracking_set_usercode(self, code_slot, usercode, *args, **kwargs):
        """Track call ordering and concurrency."""
        active_calls["count"] += 1
        active_calls["max_concurrent"] = max(
            active_calls["max_concurrent"], active_calls["count"]
        )
        call_log.append(("start", code_slot))
        # Yield to allow potential concurrent scheduling
        await asyncio.sleep(0)
        result = await original_set(self, code_slot, usercode, *args, **kwargs)
        call_log.append(("end", code_slot))
        active_calls["count"] -= 1
        return result

    with patch.object(MockLCMLock, "async_set_usercode", tracking_set_usercode):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert active_calls["max_concurrent"] <= 1, (
        f"Expected at most 1 concurrent call, got {active_calls['max_concurrent']}"
    )

    # Both slots should have been synced, one after the other
    assert len(call_log) == 4  # start/end for each slot
    assert [event for event, _ in call_log] == ["start", "end", "start", "end"], (
        f"Expected sequential execution, but call_log was: {call_log}"
    )
    assert mock_lock(config_entry).codes == {1: "9999", 2: "8888"}

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.paced
async def test_pins_changed_while_the_lock_is_offline_reach_it_once_each_paced(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #405: an offline lock is left alone, then written once per slot, paced.

    Two slots out of sync went to the lock back to back with no delay
    between them, and kept being retried against a lock that was not
    there. With the providers' real delay, the attempts on a lock that does
    not answer stop once it backs off, and then come only on its backoff;
    each slot is written once when it returns, and every operation is at
    least the delay after the one before it.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="Paced")
    entry.add_to_hass(hass)
    # Setup itself waits out the delay between its reads.
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    await async_advance_clock(hass, freezer, 5 * MIN_OPERATION_DELAY, settle=False)
    assert setup.done()
    assert setup.result()
    lock = mock_lock(entry)
    in_syncs = [in_sync_entity_id(hass, entry, slot_num) for slot_num in (1, 2)]

    def _both(state: str) -> bool:
        return all(sync_status(hass, in_sync) == state for in_sync in in_syncs)

    # Settling waits a fraction of a second at most, and the delay is longer.
    await async_tick_until(
        hass,
        freezer,
        lambda: _both(Status.IN_SYNC.value),
        max_ticks=5,
        what="both slots in sync",
        settle=False,
    )

    outage = lock.fail(None, error=LockDisconnected)
    tried_at: list[float] = []
    reach = outage.async_before

    async def _recorded(operation, slot) -> None:
        tried_at.append(time.monotonic())
        await reach(operation, slot)

    outage.async_before = _recorded  # type: ignore[method-assign]
    went_offline = time.monotonic()
    await async_set_slot_pin(hass, entry, 1, "1111")
    await async_set_slot_pin(hass, entry, 2, "2222")
    # Long enough for the backoff to try the lock once, and double.
    await async_advance_clock(hass, freezer, 4 * BACKOFF_MIN, settle=False)
    # An outage holds no slot: both wait, out of sync.
    assert _both(Status.OUT_OF_SYNC.value)
    assert lock.service_calls["set_usercode"] == []
    offline = [at - went_offline for at in tried_at]
    # BACKOFF_AFTER attempts go unanswered, paced; then the lock is backed off
    # and only probed: after the backoff, then after twice it.
    assert len(offline) == BACKOFF_AFTER + 2, offline
    assert offline[BACKOFF_AFTER - 1] - offline[0] < BACKOFF_MIN, offline
    assert offline[BACKOFF_AFTER] - offline[BACKOFF_AFTER - 1] >= BACKOFF_MIN, offline
    assert offline[BACKOFF_AFTER + 1] - offline[BACKOFF_AFTER] >= 2 * BACKOFF_MIN, (
        offline
    )

    outage.clear()
    await async_tick_until(
        hass,
        freezer,
        lambda: _both(Status.IN_SYNC.value),
        max_ticks=_ticks_through_the_backoff(4 * BACKOFF_MIN),
        what="both slots back in sync",
        settle=False,
    )
    await async_advance_sync_ticks(hass, freezer, 3, settle=False)

    assert lock.codes == {1: "1111", 2: "2222"}
    assert sorted(lock.service_calls["set_usercode"]) == [
        (1, "1111", "test1"),
        (2, "2222", "test2"),
    ]
    # The loop may fire the worker's pacing sleep a hair early (its clock
    # resolution), so the gap is checked to within a millisecond.
    assert all(
        later - earlier >= MIN_OPERATION_DELAY - 1e-3
        for earlier, later in itertools.pairwise(lock.run_times)
    ), lock.run_times

    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await async_advance_clock(hass, freezer, 5 * MIN_OPERATION_DELAY, settle=False)
    assert unload.done()


async def test_slot_disabled_during_sync_resolves_correctly(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Disabling a slot clears its code, sets nothing, and costs the slot no try."""
    lock_provider = mock_lock(lock_code_manager_config_entry)
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    assert lock_provider.codes.get(1) == "1234"
    lock_provider.service_calls["set_usercode"].clear()
    lock_provider.service_calls["clear_usercode"].clear()

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: SLOT_1_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert lock_provider.service_calls["set_usercode"] == []
    assert lock_provider.service_calls["clear_usercode"] == [(1,)]
    assert 1 not in lock_provider.codes
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    assert _worker(hass).slots[1].tries == 0


async def test_rapid_coordinator_updates_coalesce(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Many pushes in a row do not cause problems.

    The slot is put back once, to the configured PIN, whatever the lock
    reported last.
    """
    lock_provider = mock_lock(lock_code_manager_config_entry)
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    lock_provider.service_calls["set_usercode"].clear()

    # Fire 10 rapid pushes with different codes, the lock holding each.
    for i in range(10):
        lock_provider.codes[1] = f"{i:04d}"
        lock_provider._push_credential_update(1, SlotCredential.known(f"{i:04d}"))
    await hass.async_block_till_done()

    set_calls = lock_provider.service_calls["set_usercode"]
    assert set_calls == [(1, "1234", "test1")], (
        f"Expected exactly 1 set_usercode call after rapid updates, got {set_calls}"
    )
    assert lock_provider.codes[1] == "1234"
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON


async def test_availability_subscription_scoped_to_locks(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Availability subscription is scoped to the lock entities, and re-scopes on lock changes.

    Regression guard for the previous ``TrackStates(True, ...)`` all-states
    subscription, which fired the availability handler on every state change in
    the entire instance. The subscription must track only the lock entities and
    be re-scoped when locks are added/removed at runtime.
    """
    entity = get_in_sync_entity_obj(hass, SLOT_1_ACTIVE_ENTITY)
    tracker = entity._available_state_tracker
    assert tracker is not None

    # Scoped to the configured locks, NOT every state in the instance.
    assert tracker._last_track_states.all_states is False
    assert tracker._last_track_states.entities == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}

    # Removing a lock re-scopes the subscription to the remaining locks.
    entity._handle_remove_lock(LOCK_1_ENTITY_ID)
    assert tracker._last_track_states.all_states is False
    assert tracker._last_track_states.entities == {LOCK_2_ENTITY_ID}

    # Re-adding the lock re-scopes the subscription to include it again.
    re_added_lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    entity._handle_add_locks([re_added_lock])
    assert tracker._last_track_states.all_states is False
    assert tracker._last_track_states.entities == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}


async def test_handle_locks_updated_noop_before_tracker_exists(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A lock-added/removed callback firing before the availability tracker exists is a no-op.

    ``_register_callbacks`` (which wires ``_handle_add_locks`` /
    ``_handle_remove_lock``) runs slightly before
    ``_available_state_tracker`` is assigned in ``async_added_to_hass``.
    ``_async_handle_locks_updated`` guards that window by returning early
    when the tracker is still ``None`` instead of crashing on
    ``tracker.async_update_listeners``.
    """
    entity = get_in_sync_entity_obj(hass, SLOT_1_ACTIVE_ENTITY)
    assert entity._available_state_tracker is not None
    entity._available_state_tracker = None

    # Must not raise despite the tracker being unset.
    entity._handle_remove_lock(LOCK_1_ENTITY_ID)

    # The locks list is still updated even though the re-scope was skipped.
    assert LOCK_1_ENTITY_ID not in [lock.lock.entity_id for lock in entity.locks]


async def test_handle_available_state_update_ignores_unrelated_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A stray state-changed event for an entity outside the tracked lock set is ignored."""
    entity = get_in_sync_entity_obj(hass, SLOT_1_ACTIVE_ENTITY)
    initial_available = entity._attr_available

    stray_event = Event(
        "state_changed",
        {"entity_id": "lock.unrelated", "old_state": None, "new_state": None},
    )
    entity._handle_available_state_update(stray_event)

    assert entity._attr_available == initial_available


async def test_handle_available_state_update_skips_recompute_between_available_states(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A lock transition between two non-unavailable states skips the availability recompute.

    Both endpoints of the transition are already known-available, so
    ``_handle_available_state_update`` short-circuits before calling
    ``_is_available()`` again -- verified here by counting calls rather than
    by the (unchanged either way) resulting ``available`` value.
    """
    entity = get_in_sync_entity_obj(hass, SLOT_1_ACTIVE_ENTITY)
    assert entity.available

    call_count = [0]
    original_is_available = entity._is_available

    def _counting_is_available():
        call_count[0] += 1
        return original_is_available()

    with patch.object(entity, "_is_available", _counting_is_available):
        # LOCK_1 starts "unlocked" (MockLockEntity's default) -- moving to
        # "locked" is a real state change between two non-unavailable states.
        hass.states.async_set(LOCK_1_ENTITY_ID, "locked")
        await hass.async_block_till_done()

    assert call_count[0] == 0
    assert entity.available


async def test_add_code_slot_entity_skipped_when_lock_has_no_coordinator(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A lock whose provider setup has not finished (no coordinator yet) is skipped.

    ``add_code_slot_entities`` is invoked via the lock-slot-adder callback
    registry once a lock has been set up. This exercises the defensive
    ``if coordinator is None: return`` for a lock instance that has not
    completed ``async_setup_internal`` -- the in-sync sensor is simply not
    created rather than crashing on a ``None`` coordinator.
    """
    entry = lock_code_manager_config_entry
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    fresh_lock = async_create_lock_instance(
        hass, dev_reg, ent_reg, entry, LOCK_1_ENTITY_ID
    )
    assert fresh_lock.coordinator is None

    entities_before = set(hass.states.async_entity_ids("binary_sensor"))
    entry.runtime_data.callbacks.invoke_lock_slot_adders(fresh_lock, 1, ent_reg)
    await hass.async_block_till_done()

    assert set(hass.states.async_entity_ids("binary_sensor")) == entities_before


async def test_add_standard_entity_for_slot_without_coordinator_logs_warning(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Adding an entity for a slot with no registered SlotEntityCoordinator logs a warning.

    The normal flow (``async_update_listener``) always creates the slot
    coordinator before adding entities for a new slot -- see the comment
    above the loop that creates them "BEFORE setting up new locks". This
    covers the defensive fallback for a slot number added out of band
    (``invoke_standard_adders`` called directly for a slot the coordinator
    registry has never seen), verifying entity creation degrades gracefully
    with a warning rather than raising.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    assert 99 not in runtime_data.slot_coordinators

    ent_reg = er.async_get(hass)
    caplog.set_level(logging.WARNING)
    runtime_data.callbacks.invoke_standard_adders(99, ent_reg)
    await hass.async_block_till_done()

    assert "No slot coordinator for slot 99" in caplog.text

    # The active binary sensor entity was still created for the slot.
    slot_99_entities = [
        entry
        for entry in er.async_entries_for_config_entry(
            ent_reg, lock_code_manager_config_entry.entry_id
        )
        if "|99|" in entry.unique_id
    ]
    assert slot_99_entities


async def test_a_lock_that_withholds_its_contents_is_cleared_once(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A disabled slot the lock will not report on must not be cleared forever.

    Some locks answer "occupied" without returning the code -- masked PINs on
    Z-Wave, an enabled status with no value on Zigbee. The clear lands, but
    the read can never confirm it, so the slot reads out of sync on every
    tick and each tick issues another clear at a lock that has already done
    as it was asked.

    Counted at the provider boundary, not on the mock's own bookkeeping: the
    mock records nothing for a clear that finds no code, which is exactly
    what every clear after the first one looks like.
    """
    lock_provider = mock_lock(lock_code_manager_config_entry)

    # From here the lock reports slot 1 occupied and will not say what it
    # holds -- including after the clear lands.
    lock_provider.write_only = {1}

    with patch.object(
        lock_provider,
        "async_clear_usercode",
        wraps=lock_provider.async_clear_usercode,
    ) as clears:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            target={ATTR_ENTITY_ID: SLOT_1_ENABLED_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        for _ in range(4):
            await _async_read_lock(hass)
            await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    assert clears.await_count == 1, (
        f"The clear was reissued on every tick ({clears.await_count} times)"
    )
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON


async def _async_turn_slot_1(hass: HomeAssistant, service: str) -> None:
    """Enable or disable slot 1 through its switch."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        target={ATTR_ENTITY_ID: SLOT_1_ENABLED_ENTITY},
        blocking=True,
    )


def _slot_1_clears(lock: MockLCMLock) -> int:
    """Count the clears of slot 1 that reached the lock, ones finding nothing too."""
    return [
        *lock.service_calls["clear_usercode"],
        *lock.service_calls["noop_clear_usercode"],
    ].count((1,))


async def test_a_disabled_slot_the_lock_will_not_report_on_is_cleared_once(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Regression for #1437: a disabled slot the lock withholds is cleared once.

    The lock reports the slot occupied without saying what it holds, after
    the clear lands too. Nothing could confirm the clear, so the slot read
    out of sync on every tick and every tick cleared it again.
    """
    lock = mock_lock(lock_code_manager_config_entry)
    in_sync = in_sync_entity_id(hass, lock_code_manager_config_entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_ON
    lock.write_only = {1}

    await _async_turn_slot_1(hass, SERVICE_TURN_OFF)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=3,
        what="slot 1 cleared",
    )
    await async_advance_sync_ticks(hass, freezer, 5)

    assert hass.states.get(in_sync).state == STATE_ON
    assert _slot_1_clears(lock) == 1
    assert 1 not in lock.codes


async def test_a_set_that_lands_but_raises_leaves_the_next_disable_clearing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Regression for #1437: a set that raises may have landed, so it voids the clear.

    The slot's earlier clear is what lets a lock that withholds its codes
    read cleared. A set whose reply never came may still have put the code
    on the lock, so disabling the slot again must clear it, not report it
    cleared on the strength of the old clear while the code still opens
    the door.
    """
    lock = mock_lock(lock_code_manager_config_entry)
    in_sync = in_sync_entity_id(hass, lock_code_manager_config_entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    lock.write_only = {1}
    await _async_turn_slot_1(hass, SERVICE_TURN_OFF)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=3,
        what="slot 1 cleared",
    )
    assert _slot_1_clears(lock) == 1

    # The set lands, and its reply never arrives: it raises once let go.
    fault = lock.drop_reply("set", 1, times=1)
    await _async_turn_slot_1(hass, SERVICE_TURN_ON)
    await async_advance_sync_ticks(hass, freezer, settle=False)
    await fault.async_wait_held()
    await _async_turn_slot_1(hass, SERVICE_TURN_OFF)
    fault.clear()
    assert lock.codes[1] == "1234"

    await async_tick_until(
        hass,
        freezer,
        lambda: _slot_1_clears(lock) == 2,
        max_ticks=5,
        what="slot 1 cleared again",
    )
    assert 1 not in lock.codes


async def test_a_clear_that_changed_nothing_is_not_evidence(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Only a clear that actually changed something may settle the slot.

    A provider that found nothing to clear -- no user owns the credential,
    say -- has said nothing about what the slot holds. Treating the call as
    proof would report a slot cleared that this integration was never able
    to touch. Staying out of sync is the honest answer: the clear keeps being
    attempted and the user can see it never lands.
    """
    lock_provider = mock_lock(lock_code_manager_config_entry)

    # The lock reports the slot occupied, but there is nothing here to clear.
    lock_provider.codes.pop(1, None)
    lock_provider.write_only = {1}

    with patch.object(
        lock_provider,
        "async_clear_usercode",
        wraps=lock_provider.async_clear_usercode,
    ) as clears:
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            target={ATTR_ENTITY_ID: SLOT_1_ENABLED_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        for _ in range(2):
            await _async_read_lock(hass)
            await async_trigger_sync_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    assert clears.await_count > 1, "A no-op clear was taken as proof the slot is clear"
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_OFF


async def test_the_next_confirmation_read_settles_a_pending_write_without_a_second_set(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A write whose read-back fails is settled by the next read, with no second set.

    Nothing has seen the write land when the read-back fails, so the slot
    is read again; that read finds the code and the slot is in sync without
    the PIN being sent again.
    """
    lock_provider = mock_lock(lock_code_manager_config_entry)
    read_back = lock_provider.fail("get", 1, error=LockDisconnected, times=1)

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert read_back.hits == 1
    assert lock_provider.codes[1] == "9999"
    assert lock_provider.service_calls["set_usercode"] == [(1, "9999", "test1")]
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON
    assert _worker(hass).slots[1].hold is None


async def test_disabling_the_in_sync_sensor_does_not_stop_the_sync(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The sensors are views: the lock's worker syncs whether or not anyone watches it."""
    entry = lock_code_manager_config_entry
    aggregate_id = in_sync_entity_id(hass, entry, 1)
    await async_disable_and_reload(hass, entry, aggregate_id)

    await async_set_slot_pin(hass, entry, 1, "9999")
    await hass.async_block_till_done()

    assert mock_lock(entry).codes[1] == "9999"


@pytest.mark.parametrize("key", [ATTR_IN_SYNC, ATTR_PIN_IN_SYNC])
async def test_an_in_sync_sensor_added_without_a_slot_coordinator_reads_unknown(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    key: str,
) -> None:
    """With no coordinator to fold, the sensor has nothing to say and says so."""
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    assert 99 not in runtime_data.slot_coordinators
    ent_reg = er.async_get(hass)
    # Registered enabled ahead of time, so the platform adds the sensor
    # whatever its class default.
    unique_id = build_slot_unique_id(entry.entry_id, 99, key, LOCK_1_ENTITY_ID)
    ent_reg.async_get_or_create(
        BINARY_SENSOR_DOMAIN, DOMAIN, unique_id, config_entry=entry, disabled_by=None
    )

    runtime_data.callbacks.invoke_lock_slot_adders(
        runtime_data.locks[LOCK_1_ENTITY_ID], 99, ent_reg
    )
    await hass.async_block_till_done()

    entity_id = ent_reg.async_get_entity_id(BINARY_SENSOR_DOMAIN, DOMAIN, unique_id)
    assert entity_id is not None
    # Added without raising; with no manager behind it there is nothing to
    # fold, and no credential at that slot to be available for.
    entity_obj = get_in_sync_entity_obj(hass, entity_id)
    assert entity_obj.is_on is None
    assert entity_obj.extra_state_attributes == {}
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == STATE_UNAVAILABLE


async def test_pin_in_sync_is_registered_but_disabled_by_default(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The per-credential sensor exists for whoever wants the split, off until asked."""
    entity_id = pin_in_sync_entity_id(hass, lock_code_manager_config_entry, 1)
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


async def test_pin_in_sync_mirrors_the_manager_once_enabled(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Enabled, the PIN sensor reads exactly what the aggregate reads for a PIN-only user."""
    entry = lock_code_manager_config_entry
    pin_entity_id = pin_in_sync_entity_id(hass, entry, 1)
    await async_enable_and_reload(hass, entry, pin_entity_id)

    aggregate_id = in_sync_entity_id(hass, entry, 1)
    await async_initial_tick(hass, aggregate_id)
    aggregate = hass.states.get(aggregate_id)
    pin = hass.states.get(pin_entity_id)
    assert aggregate is not None and pin is not None
    assert pin.state == aggregate.state == STATE_ON
    assert pin.attributes[ATTR_SYNC_STATUS] == aggregate.attributes[ATTR_SYNC_STATUS]

    # A change the worker publishes reaches both views: the code changed at
    # the lock, and the write putting it back fails.
    lock_provider = mock_lock(entry)
    with lock_provider.fail("set", 1):
        lock_provider.codes[1] = "0000"
        await _async_read_lock(hass)
        aggregate = hass.states.get(aggregate_id)
        pin = hass.states.get(pin_entity_id)
    assert aggregate is not None and pin is not None
    assert pin.state == aggregate.state == STATE_OFF
    assert pin.attributes[ATTR_SYNC_STATUS] == aggregate.attributes[ATTR_SYNC_STATUS]


async def test_a_user_turned_off_with_no_pin_settles_in_sync(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """#1085: a disabled user saved with no PIN is in sync, not waiting for one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_ENABLED: False}},
        },
        unique_id="pr1085",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(in_sync_entity_id(hass, entry, 1)).state == STATE_ON
    assert 1 not in mock_lock(entry).codes
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_slot_left_to_another_entry_does_not_show_that_entry_s_code(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An entry whose slot another entry already manages on the lock is not in sync.

    Its user's PIN (2222) is not on the lock; the first entry's (5678) is. Its
    per-lock sensors mirrored the first entry's slot: "in sync", and the code
    sensor showed the other entry's user's PIN.
    """
    await async_setup_lcm_entry(hass)
    second = await async_setup_lcm_entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                2: {CONF_NAME: "other2", CONF_PIN: "2222", CONF_ENABLED: True},
                3: {CONF_NAME: "other3", CONF_PIN: "3333", CONF_ENABLED: True},
            },
        },
        unique_id="other_entry",
    )
    assert mock_lock(second).codes[2] == "5678"

    in_sync = hass.states.get(in_sync_entity_id(hass, second, 2))
    code = hass.states.get(code_entity_id(hass, second, 2))
    assert in_sync.state != STATE_ON
    assert code.state != "5678"
    # Its own slot on the lock is still viewed.
    assert hass.states.get(in_sync_entity_id(hass, second, 3)).state == STATE_ON


CONDITION = "input_boolean.access"


async def test_a_condition_change_shows_the_slot_syncing(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The calendar/condition turning a slot off queues a clear: the sensor says syncing.

    The old sensor showed ``syncing`` from the moment a sync was due. Now the
    worker reports a change only when a job finishes, and a condition change
    writes no entity state, so the sensor keeps saying in_sync for the whole
    of a slow clear.
    """
    hass.states.async_set(CONDITION, "on")
    entry = await async_setup_lcm_entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {
                    CONF_NAME: "test1",
                    CONF_PIN: "1234",
                    CONF_ENABLED: True,
                    CONF_CONDITION: CONDITION,
                },
            },
        },
    )
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    assert sync_status(hass, in_sync) == "in_sync"

    fault = lock.hang("clear", 1)
    hass.states.async_set(CONDITION, "off")
    await fault.async_wait_held()

    assert sync_status(hass, in_sync) == "syncing"

    fault.clear()
    await hass.async_block_till_done()
    assert 1 not in lock.codes
    assert sync_status(hass, in_sync) == "in_sync"
