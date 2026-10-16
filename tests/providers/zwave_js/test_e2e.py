"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

import asyncio
from collections import Counter
from collections.abc import Iterable
import copy
from datetime import timedelta
import itertools
from typing import Any
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.access_control import (
    UserCredentialType,
    UserCredentialUserType,
)
from zwave_js_server.const.command_class.lock import (
    LOCK_USERCODE_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.access_control import CredentialData, UserData
from zwave_js_server.model.node import Node

from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.lock_code_manager.const import (
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_SLOT,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
)
from custom_components.lock_code_manager.ha.allocation import async_read_occupancy
from custom_components.lock_code_manager.ha.runtime import (
    lock_codes,
    lock_registry,
    locks_that_cannot_report,
)
from custom_components.lock_code_manager.model.credentials import (
    WriteResult,
)
from custom_components.lock_code_manager.model.exceptions import DuplicateCodeError
from custom_components.lock_code_manager.model.models import SlotCode, SlotCredential
from custom_components.lock_code_manager.model.queries import subentry_id_for_slot
from custom_components.lock_code_manager.model.util import per_lock_issue_id
from custom_components.lock_code_manager.providers._base import MIN_OPERATION_DELAY
from custom_components.lock_code_manager.providers._util import parse_tag
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock
from custom_components.lock_code_manager.sync.slot import Holds
from custom_components.lock_code_manager.sync.worker import LockWorker
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_configure_flow,
    async_set_slot_pin,
    async_switch_slot,
    async_tick_until,
    code_entity_id,
    in_sync_entity_id,
    lcm_issue_ids,
    slot_entity_id,
    user_subentries,
)
from tests.conftest import async_trigger_sync_tick
from tests.providers.zwave_js.conftest import (
    ZWAVE_JS_LCM_CONFIG_SLOTS,
    get_zwave_lock,
)
from tests.providers.zwave_js.fake_node import (
    USER_CREDENTIAL_CC_ID,
    FakeZWaveLock,
    UserReportType,
    supervised_lock_state,
    user_credential_lock_state,
)
from tests.providers.zwave_js.helpers import (
    make_uc_value_event,
)


def _codes(hass: HomeAssistant, lock: ZWaveJSLock) -> dict[int, str | SlotCode]:
    """Return what the lock's worker has seen of every slot on it."""
    return lock_codes(hass, lock.lock.entity_id)


def _record_pushes(
    hass: HomeAssistant, lock: ZWaveJSLock
) -> list[tuple[int, SlotCredential]]:
    """Record what the provider pushes to the lock's worker, slot by slot."""
    worker = lock_registry(hass).worker(lock.lock.entity_id)
    assert worker is not None
    port = worker.port
    pushed: list[tuple[int, SlotCredential]] = []
    forward = port.pushed

    def _pushed(slot: int, credential: SlotCredential) -> None:
        pushed.append((slot, credential))
        forward(slot, credential)

    port.pushed = _pushed  # type: ignore[method-assign]
    return pushed


def _users_tagged_for(lock: FakeZWaveLock, slot: int) -> set[int]:
    """Return the ids of the lock's users whose name carries LCM's tag for ``slot``."""
    return {
        user_id
        for user_id, user in lock.users.items()
        if parse_tag(user.user_name)[0] == slot
    }


def _first_push(
    pushed: list[tuple[int, SlotCredential]], slot: int
) -> SlotCredential | None:
    """Return the first credential pushed for ``slot`` (later ones are read-backs)."""
    return next((credential for number, credential in pushed if number == slot), None)


def async_capture_events(
    hass: HomeAssistant, event_name: str
) -> list[Event[dict[str, Any]]]:
    """Create a helper that captures events."""
    events: list[Event[dict[str, Any]]] = []

    @callback
    def capture_events(event: Event[dict[str, Any]]) -> None:
        events.append(event)

    hass.bus.async_listen(event_name, capture_events)
    return events


class TestEvents:
    """Verify Z-Wave notification events flow through to LCM events."""

    async def test_notification_event_fires_lock_state_changed(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
    ) -> None:
        """
        Fire a Z-Wave notification event and verify LCM processes it.

        A keypad lock notification (type 6, event 5) should be handled by the
        provider's event listener. We verify the listener is active by checking
        the provider has registered listeners.
        """
        fired = MagicMock()
        e2e_zwave_lock.async_fire_code_slot_event = fired

        event = ZwaveEvent(
            type="notification",
            data={
                "source": "node",
                "event": "notification",
                "nodeId": lock_schlage_be469.node_id,
                "endpointIndex": 0,
                "ccId": 113,
                "args": {
                    "type": 6,
                    "event": 5,
                    "label": "Access Control",
                    "eventLabel": "Keypad lock operation",
                    "parameters": {"userId": 1},
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        fired.assert_called_once_with(code_slot=1, to_locked=True)

    async def test_push_credential_added_reaches_the_worker(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
    ) -> None:
        """A credential-added node event for a Personal Identification Number reaches the worker."""
        pushed = _record_pushes(hass, e2e_zwave_lock)
        event = ZwaveEvent(
            type="credential added",
            data={
                "source": "node",
                "event": "credential added",
                "nodeId": lock_schlage_be469.node_id,
                "endpointIndex": 0,
                "args": {
                    "userId": 1,
                    "credentialType": UserCredentialType.PIN_CODE,
                    "credentialSlot": 1,
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        # Credential events push unreadable (the lock doesn't expose the Personal
        # Identification Number value in the event; the worker's read gets it).
        assert _first_push(pushed, 1) == SlotCredential.unreadable()


# A push lock is read only when asked, and Home Assistant debounces the asking:
# a read asked for during the cooldown of the last one runs up to ten seconds
# later. Five sync ticks, and a few more for the sync to act on what it read.
DEBOUNCED_READ_TICKS = 8


def _slot_states(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    slot: int,
) -> tuple[str, str]:
    """Return a slot's in-sync and code sensor states on the lock."""
    return (
        hass.states.get(
            in_sync_entity_id(hass, entry, slot, lock_entity.entity_id)
        ).state,
        hass.states.get(code_entity_id(hass, entry, slot, lock_entity.entity_id)).state,
    )


def _record_slot_states(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    slot: int,
) -> list[tuple[str, str]]:
    """Record a slot's in-sync and code sensor states each time either changes."""
    seen: list[tuple[str, str]] = []

    @callback
    def _record(event: Event[EventStateChangedData]) -> None:
        seen.append(_slot_states(hass, entry, lock_entity, slot))

    async_track_state_change_event(
        hass,
        [
            in_sync_entity_id(hass, entry, slot, lock_entity.entity_id),
            code_entity_id(hass, entry, slot, lock_entity.entity_id),
        ],
        _record,
    )
    return seen


async def _async_change_pin(
    hass: HomeAssistant, entry: MockConfigEntry, slot: int, pin: str
) -> None:
    """Set a slot's PIN through its text entity."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_VALUE: pin},
        target={
            ATTR_ENTITY_ID: slot_entity_id(hass, TEXT_DOMAIN, entry, slot, CONF_PIN)
        },
        blocking=True,
    )


async def _async_settle_in_sync(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    slot: int,
    *,
    max_ticks: int = 3,
) -> None:
    """Advance sync ticks until the slot reports in sync on the lock."""
    await async_tick_until(
        hass,
        freezer,
        lambda: _slot_states(hass, entry, lock_entity, slot)[0] == STATE_ON,
        max_ticks=max_ticks,
        what=f"slot {slot} in sync",
    )


def _keypad_unlock(node: Node, user_id: int) -> None:
    """Deliver the notification a lock sends when a code unlocks it at the keypad."""
    node.receive_event(
        ZwaveEvent(
            type="notification",
            data={
                "source": "node",
                "event": "notification",
                "nodeId": node.node_id,
                "endpointIndex": 0,
                "ccId": CommandClass.NOTIFICATION,
                "args": {
                    "type": 6,  # Access Control
                    "event": 6,  # Keypad unlock operation
                    "label": "Access Control",
                    "eventLabel": "Keypad unlock operation",
                    "parameters": {"userId": user_id},
                },
            },
        )
    )


def _entry_on_slot_3(lock_entity: er.RegistryEntry, unique_id: str) -> MockConfigEntry:
    """Build a second entry on the Z-Wave JS lock, its one user on slot 3."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [lock_entity.entity_id]},
        unique_id=unique_id,
        subentries_data=user_subentries(
            {3: {CONF_NAME: "slot3", CONF_PIN: "5555", CONF_ENABLED: True}}
        ),
    )


class TestSharedLock:
    """A lock entries share answers for each slot from the entry that owns it."""

    async def test_a_stale_available_on_the_second_entrys_slot_keeps_its_code(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        lock_schlage_be469: Node,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
    ) -> None:
        """
        The second entry's code is wanted on its slot, so a stale Available is ignored.

        The lock instance and its coordinator belong to whichever entry set the
        lock up first. Asked only of that entry, a slot the second entry owns
        wants nothing, so the stale Available some locks send after a code is
        set (#863) would be taken at its word: the slot reported empty while it
        holds the code, and the code written again.
        """
        # The lock holds the second entry's code: its reads say so.
        _lock_holds_on_slot_3(mock_access_control, "5555")
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        lock = get_zwave_lock(hass, lcm_config_entry, lock_entity)
        assert second_entry.runtime_data.locks[lock_entity.entity_id] is lock
        in_sync = in_sync_entity_id(hass, second_entry, 3, lock_entity.entity_id)

        # The lock reports the second entry's code on its slot: in sync.
        lock_schlage_be469.receive_event(
            make_uc_value_event(lock_schlage_be469.node_id, "userCode", 3, "5555")
        )
        await hass.async_block_till_done()
        await async_trigger_sync_tick(hass, in_sync, set_dirty=False)
        assert hass.states.get(in_sync).state == "on"
        mock_lock_helpers["async_set_credential"].reset_mock()

        lock_schlage_be469.receive_event(
            make_uc_value_event(
                lock_schlage_be469.node_id, "userIdStatus", 3, CodeSlotStatus.AVAILABLE
            )
        )
        await hass.async_block_till_done()
        assert _codes(hass, lock).get(3) == "5555"

        await async_trigger_sync_tick(hass, in_sync, set_dirty=False)
        assert hass.states.get(in_sync).state == "on"
        mock_lock_helpers["async_set_credential"].assert_not_called()

        await hass.config_entries.async_unload(second_entry.entry_id)

    @pytest.mark.parametrize(
        "creator_disabled", [False, True], ids=["creator_loaded", "creator_disabled"]
    )
    async def test_a_stale_available_on_the_second_entrys_slot_is_not_rewritten(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        creator_disabled: bool,
    ) -> None:
        """
        Regression for #1623: a stale Available on the second entry's slot is ignored.

        Whether a PIN is wanted was asked only of the entry that set the
        shared lock up, which does not own slot 3, so the stale Available --
        its code field still carrying the PIN -- was believed and the second
        entry's code written again.
        """
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        if creator_disabled:
            assert await hass.config_entries.async_set_disabled_by(
                fake_zwave_lock_entry.entry_id, ConfigEntryDisabler.USER
            )
            await hass.async_block_till_done()
        await _async_settle_in_sync(
            hass, freezer, second_entry, lock_entity, 3, max_ticks=DEBOUNCED_READ_TICKS
        )
        assert fake_zwave_lock.sets == [(3, "5555")]

        fake_zwave_lock.send_user_code_report(3, CodeSlotStatus.AVAILABLE, "5555")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert _slot_states(hass, second_entry, lock_entity, 3) == (STATE_ON, "5555")
        assert fake_zwave_lock.sets == [(3, "5555")]

        await hass.config_entries.async_unload(second_entry.entry_id)

    async def test_the_creating_entry_disabled_wants_none_of_its_slots(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        lock_schlage_be469: Node,
    ) -> None:
        """
        Disabling the entry that set the shared lock up releases its slots.

        The second entry keeps the lock, which the first entry built, and its
        coordinator alive. A disabled entry syncs
        nothing, so its codes are not wanted and an Available on one of its
        slots is taken at its word.
        """
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        lock = get_zwave_lock(hass, lcm_config_entry, lock_entity)

        assert await hass.config_entries.async_set_disabled_by(
            lcm_config_entry.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert second_entry.runtime_data.locks[lock_entity.entity_id] is lock

        lock_schlage_be469.receive_event(
            make_uc_value_event(
                lock_schlage_be469.node_id, "userIdStatus", 1, CodeSlotStatus.AVAILABLE
            )
        )
        await hass.async_block_till_done()

        assert _codes(hass, lock).get(1) == SlotCode.EMPTY

        await hass.config_entries.async_unload(second_entry.entry_id)

    @pytest.mark.parametrize(
        "state",
        [
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.MIGRATION_ERROR,
            ConfigEntryState.FAILED_UNLOAD,
        ],
        ids=["setup_failed", "migration_failed", "unload_failed"],
    )
    async def test_an_entry_that_failed_keeps_no_slots(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        lock_schlage_be469: Node,
        state: ConfigEntryState,
    ) -> None:
        """
        An entry that failed syncs nothing until the user acts, so it wants nothing.

        Counted as keeping its slots, a genuine Available on one of them would
        be dropped as stale and the slot shown holding a code it no longer has.
        """
        failed_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_failed")
        failed_entry.add_to_hass(hass)
        failed_entry.mock_state(hass, state)
        lock = get_zwave_lock(hass, lcm_config_entry, lock_entity)

        lock_schlage_be469.receive_event(
            make_uc_value_event(lock_schlage_be469.node_id, "userCode", 3, "5555")
        )
        lock_schlage_be469.receive_event(
            make_uc_value_event(
                lock_schlage_be469.node_id, "userIdStatus", 3, CodeSlotStatus.AVAILABLE
            )
        )
        await hass.async_block_till_done()

        assert _codes(hass, lock).get(3) == SlotCode.EMPTY


def _lock_holds_on_slot_3(access_control: MagicMock, pin: str) -> None:
    """Make the lock's next read report ``pin`` on slot 3."""
    access_control.get_users_cached.return_value = [
        UserData(
            user_id=3,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            # Lock Code Manager's own user: an untagged one is someone else's.
            user_name="lcm:3:slot3",
        )
    ]
    access_control.get_all_credentials_cached.return_value = [
        CredentialData(user_id=3, type=UserCredentialType.PIN_CODE, slot=3, data=pin)
    ]


async def _assert_a_refresh_reads_the_lock(
    hass: HomeAssistant, lock: ZWaveJSLock, access_control: MagicMock
) -> None:
    """Assert the lock's worker still reads the lock when asked."""
    _lock_holds_on_slot_3(access_control, "7777")
    worker = lock_registry(hass).worker(lock.lock.entity_id)
    assert worker is not None
    assert (await worker.read([3]))[3] == Holds("7777")


class TestSharedCoordinatorLifetime:
    """A shared lock's coordinator runs while any entry uses the lock."""

    async def test_disabling_the_creating_entry_keeps_the_coordinator_running(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
    ) -> None:
        """The entry left using the lock still has a coordinator that reads it."""
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        lock = get_zwave_lock(hass, lcm_config_entry, lock_entity)

        assert await hass.config_entries.async_set_disabled_by(
            lcm_config_entry.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        assert second_entry.runtime_data.locks[lock_entity.entity_id] is lock

        await _assert_a_refresh_reads_the_lock(hass, lock, mock_access_control)

        await hass.config_entries.async_unload(second_entry.entry_id)

    async def test_reloading_the_creating_entry_keeps_the_coordinator_running(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
    ) -> None:
        """The reloaded entry takes back the shared lock with a working coordinator."""
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        lock = get_zwave_lock(hass, lcm_config_entry, lock_entity)

        assert await hass.config_entries.async_reload(lcm_config_entry.entry_id)
        await hass.async_block_till_done()
        assert get_zwave_lock(hass, lcm_config_entry, lock_entity) is lock

        await _assert_a_refresh_reads_the_lock(hass, lock, mock_access_control)

        await hass.config_entries.async_unload(second_entry.entry_id)

    @pytest.mark.parametrize("leave", ["disable", "reload"])
    async def test_a_keypad_change_reaches_the_second_entry_after_the_creator_leaves(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        leave: str,
    ) -> None:
        """
        Regression for #1629: the second entry still hears the lock once its creator leaves.

        The shared lock's coordinator belonged to the entry that created it,
        so disabling or reloading that entry shut it down, and what the lock
        reported afterwards reached nobody.
        """
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        await _async_settle_in_sync(
            hass, freezer, second_entry, lock_entity, 3, max_ticks=DEBOUNCED_READ_TICKS
        )

        if leave == "disable":
            assert await hass.config_entries.async_set_disabled_by(
                fake_zwave_lock_entry.entry_id, ConfigEntryDisabler.USER
            )
        else:
            assert await hass.config_entries.async_reload(
                fake_zwave_lock_entry.entry_id
            )
        await hass.async_block_till_done()
        seen = _record_slot_states(hass, second_entry, lock_entity, 3)
        fake_zwave_lock.program(3, "0000")
        await hass.async_block_till_done()

        # The change is seen, and the worker puts the code back at once.
        assert (STATE_OFF, "0000") in seen
        await _async_settle_in_sync(hass, freezer, second_entry, lock_entity, 3)
        assert fake_zwave_lock.holds(3) == "5555"

        await hass.config_entries.async_unload(second_entry.entry_id)

    async def test_a_keypad_use_after_reloading_beside_an_unloaded_sibling_counts_once(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        lock_schlage_be469: Node,
    ) -> None:
        """
        Regression for #1476: a code used once is recorded once after a reload.

        With the second entry unloaded, reloading the first left its old lock
        instance listening -- another entry still listed the lock -- and the
        reload, finding no loaded entry to share with, built a second one.
        Every keypad use then reached the bus, and the user's event entity,
        twice.
        """
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(second_entry.entry_id)
        assert await hass.config_entries.async_reload(lcm_config_entry.entry_id)
        await hass.async_block_till_done()
        used = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)
        recorded: list[Event[EventStateChangedData]] = []
        async_track_state_change_event(
            hass,
            slot_entity_id(
                hass, EVENT_DOMAIN, lcm_config_entry, 1, EVENT_CREDENTIAL_USED
            ),
            recorded.append,
        )

        _keypad_unlock(lock_schlage_be469, user_id=1)
        await hass.async_block_till_done()

        assert [event.data[ATTR_NAME] for event in used] == ["slot1"]
        assert len(recorded) == 1

    @pytest.mark.parametrize(
        "creating_entry_last", [True, False], ids=["creating_last", "creating_first"]
    )
    async def test_the_worker_stops_once_the_last_entry_unloads(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        creating_entry_last: bool,
    ) -> None:
        """
        The lock's worker runs until the last entry using the lock unloads, then stops.

        Watched from before the worker exists, so every stop is counted.
        """
        with patch.object(
            LockWorker, "stop", autospec=True, side_effect=LockWorker.stop
        ) as stop:
            creating_entry = MockConfigEntry(
                domain=DOMAIN,
                data={
                    CONF_LOCKS: [lock_entity.entity_id],
                    CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS,
                },
                unique_id="test_zwave_js_e2e",
            )
            second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
            for entry in (creating_entry, second_entry):
                entry.add_to_hass(hass)
                assert await hass.config_entries.async_setup(entry.entry_id)
                await hass.async_block_till_done()
            lock = get_zwave_lock(hass, creating_entry, lock_entity)
            worker = lock_registry(hass).worker(lock_entity.entity_id)
            first, last = (
                (second_entry, creating_entry)
                if creating_entry_last
                else (creating_entry, second_entry)
            )

            assert await hass.config_entries.async_unload(first.entry_id)
            await hass.async_block_till_done()
            stop.assert_not_called()
            assert lock_registry(hass).worker(lock_entity.entity_id) is worker
            await _assert_a_refresh_reads_the_lock(hass, lock, mock_access_control)

            assert await hass.config_entries.async_unload(last.entry_id)
            await hass.async_block_till_done()
            stop.assert_awaited_once()
            assert stop.await_args.args[0] is worker
            assert lock_registry(hass).worker(lock_entity.entity_id) is None


class TestColdStartRace:
    """Regression tests for issue #1321: LCM setup racing zwave_js startup."""

    async def test_entities_come_up_once_zwave_js_loads(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1321: a lock set up before Z-Wave JS loads comes up with it.

        On a slow boot the Z-Wave JS entry has not loaded when Lock Code
        Manager sets up. The lock was dropped, and its entities stayed
        unavailable until a manual reload.
        """
        assert await hass.config_entries.async_unload(zwave_integration.entry_id)
        await hass.async_block_till_done()
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [lock_entity.entity_id],
                CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS,
            },
            unique_id="test_zwave_js_cold_start",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        assert _slot_states(hass, lcm_entry, lock_entity, 2) == (
            STATE_UNAVAILABLE,
            STATE_UNAVAILABLE,
        )

        assert await hass.config_entries.async_setup(zwave_integration.entry_id)
        await hass.async_block_till_done()
        await _async_settle_in_sync(
            hass, freezer, lcm_entry, lock_entity, 2, max_ticks=DEBOUNCED_READ_TICKS
        )
        assert _slot_states(hass, lcm_entry, lock_entity, 2) == (STATE_ON, "1234")

        # The lock's reports reach the slot: it is listened to, not just read
        # (the worker then puts the code back at once).
        seen = _record_slot_states(hass, lcm_entry, lock_entity, 2)
        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()
        assert (STATE_OFF, "4321") in seen

        await hass.config_entries.async_unload(lcm_entry.entry_id)


# No entry manages slot 3, so no PIN is wanted there; slot 2 wants one.
UNWANTED_SLOT = 3
WANTED_SLOT = 2


class TestUserCodeReports:
    """A User Code report reaches the coordinator as what the slot holds."""

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param("****", id="masked"),
            pytest.param("5678", id="leftover_code"),
        ],
    )
    async def test_an_available_slot_no_pin_is_wanted_on_is_empty(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock: ZWaveJSLock,
        code: str,
    ) -> None:
        """
        Available means the slot holds nothing, whatever its code field says.

        A lock that masks its codes reports an Available slot with a masked
        placeholder; read as a code, it was the PIN ``****`` (#819).
        """
        fake_zwave_lock.send_user_code_report(
            UNWANTED_SLOT, CodeSlotStatus.AVAILABLE, code
        )
        await hass.async_block_till_done()

        assert _codes(hass, fake_e2e_lock).get(UNWANTED_SLOT) == SlotCode.EMPTY

    @pytest.mark.parametrize(
        "code",
        [
            pytest.param(ZWAVE_JS_LCM_CONFIG_SLOTS[WANTED_SLOT]["pin"], id="pin"),
            pytest.param("****", id="masked"),
        ],
    )
    async def test_a_stale_available_does_not_empty_a_wanted_slot(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock: ZWaveJSLock,
        code: str,
    ) -> None:
        """
        Where a PIN is wanted, an Available status alone does not empty the slot.

        Some locks announce Available after a PIN lands (#863); taken as
        empty, sync would rewrite the slot forever. The code field is read
        as it always was.
        """
        pushed = _record_pushes(hass, fake_e2e_lock)
        fake_zwave_lock.send_user_code_report(
            WANTED_SLOT, CodeSlotStatus.AVAILABLE, code
        )
        await hass.async_block_till_done()

        assert _first_push(pushed, WANTED_SLOT) == SlotCredential.known(code)

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            pytest.param("4321", SlotCredential.known("4321"), id="code"),
            # The lock withholding the code of an occupied slot.
            pytest.param("", SlotCredential.unreadable(), id="withheld"),
        ],
    )
    @pytest.mark.parametrize("code_slot", [UNWANTED_SLOT, WANTED_SLOT])
    async def test_an_enabled_slot_reads_its_code_field(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock: ZWaveJSLock,
        code_slot: int,
        code: str,
        expected: SlotCredential,
    ) -> None:
        """An Enabled slot holds its code, or a code it will not say."""
        pushed = _record_pushes(hass, fake_e2e_lock)
        fake_zwave_lock.send_user_code_report(code_slot, CodeSlotStatus.ENABLED, code)
        await hass.async_block_till_done()

        assert _first_push(pushed, code_slot) == expected

    @pytest.mark.parametrize(
        "code_after_clear",
        [pytest.param("****", id="masked"), pytest.param("1234", id="leftover_code")],
    )
    async def test_a_cleared_slot_reported_available_stays_cleared(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        code_after_clear: str,
    ) -> None:
        """
        Regression for #819: a cleared slot reported Available reads empty.

        Once cleared, the lock reports the slot Available with a code field
        still carrying a masked placeholder or the old code. Read as a code,
        the slot never looked cleared and was cleared again on every report.
        """
        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2
        )

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            target={
                ATTR_ENTITY_ID: slot_entity_id(
                    hass, SWITCH_DOMAIN, fake_zwave_lock_entry, 2, CONF_ENABLED
                )
            },
            blocking=True,
        )
        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2
        )
        # The lock's own report of the cleared slot.
        fake_zwave_lock.send_user_code_report(
            2, CodeSlotStatus.AVAILABLE, code_after_clear
        )
        await async_advance_sync_ticks(hass, freezer, 3)

        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "",
        )
        assert fake_zwave_lock.clears == [2]

    async def test_a_stale_available_after_a_set_does_not_rewrite_the_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #863: an Available after a set does not empty a wanted slot.

        Some locks report the slot Available a few seconds after the PIN
        lands, its code field still carrying the PIN. The driver persists the
        status and the code together, raising a value event for each. Taken
        as empty, the slot was rewritten, and the next stale Available
        started it over.
        """
        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2
        )
        assert fake_zwave_lock.sets == [(2, "4321")]

        fake_zwave_lock.send_user_code_report(2, CodeSlotStatus.AVAILABLE, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert fake_zwave_lock.sets == [(2, "4321")]

    async def test_a_code_changed_at_the_keypad_is_put_back(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1327: a User Code report of a keypad change reaches sync.

        A lock routed to User Code raises no unified credential event for a
        change programmed at the keypad, only the report's value events.
        Unheard, the slot kept showing the PIN it no longer held.
        """
        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2
        )

        seen = _record_slot_states(hass, fake_zwave_lock_entry, lock_entity, 2)
        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()
        # Seen, then put back at once.
        assert (STATE_OFF, "4321") in seen

        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2
        )
        assert fake_zwave_lock.sets == [(2, "1234")]
        assert fake_zwave_lock.holds(2) == "1234"


class TestMaskingLock:
    """A lock that shows every code it holds as asterisks, from the start."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The node's value database as a masking lock fills it."""
        for value in lock_schlage_be469_state["values"]:
            if (
                value["commandClass"] == CommandClass.USER_CODE
                and value["property"] == LOCK_USERCODE_PROPERTY
                and value.get("value")
            ):
                value["value"] = "*" * len(value["value"])
        return lock_schlage_be469_state

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """The lock holds the entry's PINs and masks every code it reports."""
        return {
            "code_display": "masked",
            "codes": {
                slot: config[CONF_PIN]
                for slot, config in ZWAVE_JS_LCM_CONFIG_SLOTS.items()
            },
        }

    async def test_each_slot_is_written_once_and_left_alone(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #776: a masking lock's slots are written once, not on every report.

        A PIN the lock will not show back cannot be confirmed, so each slot
        is written once and the write trusted. Read as the code ``****``, a
        slot never matched its PIN, and sync rewrote it after every report,
        the code alternating between the PIN and ``****``.
        """
        for _ in range(15):
            await async_advance_sync_ticks(hass, freezer)
            # The lock reports its slots between ticks, masked as ever.
            for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
                fake_zwave_lock.report(slot)

        writes = Counter(slot for slot, _pin in fake_zwave_lock.sets)
        for slot, config in ZWAVE_JS_LCM_CONFIG_SLOTS.items():
            assert writes[slot] == 1
            assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, slot) == (
                STATE_ON,
                config[CONF_PIN],
            )

    async def test_the_second_entrys_withheld_code_shows_its_own_pin(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1623: a code the lock withholds shows the PIN of the slot's entry.

        The code sensor stands in the configured PIN for a code the lock will
        not show. Asked of the entry that set the shared lock up, slot 3 had
        no PIN, and the second entry's sensor showed none.
        """
        # The creating entry's startup writes land first: a push while the
        # joining entry's debounced first read waits cancels that read, and
        # its slot would stay unavailable (an open bug, not tested here).
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        await _async_settle_in_sync(
            hass, freezer, second_entry, lock_entity, 3, max_ticks=DEBOUNCED_READ_TICKS
        )

        fake_zwave_lock.report(3)
        await async_advance_sync_ticks(hass, freezer, 2)

        assert _slot_states(hass, second_entry, lock_entity, 3) == (STATE_ON, "5555")
        assert [write for write in fake_zwave_lock.sets if write[0] == 3] == [
            (3, "5555")
        ]

        await hass.config_entries.async_unload(second_entry.entry_id)


class TestDuplicateRefusedByUserCodeLock:
    """
    A User Code lock that holds 5555 at slot 5 and does not show its codes.

    Lock Code Manager cannot see 5555 to refuse it first, so the lock does:
    it keeps the slot's old code and sends Access Control notification 15,
    the only word User Code CC has for a duplicate.
    """

    @pytest.fixture(params=["masked", "withheld"])
    def code_display(self, request: pytest.FixtureRequest) -> str:
        """How the lock shows a code it holds."""
        return request.param

    @pytest.fixture(
        params=[None, "15.25.1"], ids=["unsupervised", "supervised_older_driver"]
    )
    def supervised_driver(self, request: pytest.FixtureRequest) -> str | None:
        """
        The driver behind a lock that supervises its Sets, or None if it does not.

        Before 15.25.2 the driver cached nothing of a supervised Set and read
        nothing back after a failed one (#1651/#1652).
        """
        return request.param

    @pytest.fixture
    def lock_schlage_be469_state(
        self,
        lock_schlage_be469_state: dict[str, Any],
        code_display: str,
        supervised_driver: str | None,
    ) -> dict[str, Any]:
        """The node's value database as the lock filled it at inclusion."""
        if supervised_driver is not None:
            lock_schlage_be469_state = supervised_lock_state(lock_schlage_be469_state)
        for value in lock_schlage_be469_state["values"]:
            if (
                value["commandClass"] == CommandClass.USER_CODE
                and value["property"] == LOCK_USERCODE_PROPERTY
                and value.get("value")
            ):
                value["value"] = (
                    "*" * len(value["value"]) if code_display == "masked" else ""
                )
        return lock_schlage_be469_state

    @pytest.fixture
    def fake_zwave_lock_options(
        self, code_display: str, supervised_driver: str | None
    ) -> dict[str, Any]:
        """The lock holds the entry's PINs, and 5555 at slot 5."""
        options: dict[str, Any] = {
            "code_display": code_display,
            "codes": {1: "9999", 2: "1234", 5: "5555"},
        }
        if supervised_driver is not None:
            options["driver_version"] = supervised_driver
        return options

    async def _async_settle(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> int:
        """Settle both slots in sync; return how many writes that took."""
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(hass, freezer, entry, lock_entity, slot)
        return len(fake_zwave_lock.sets)

    @pytest.mark.parametrize(
        "parameters", [None, {"userId": 0}, {"userId": 2}], ids=["none", "0", "2"]
    )
    async def test_a_refused_pin_disables_the_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        parameters: dict[str, Any] | None,
    ) -> None:
        """
        Regression for #1651: the refusal disables the slot, as an in-band duplicate does.

        The driver's User Code write has no duplicate result: a masking lock's
        read-back of the old code passes as the new one, and a withholding
        lock's comes back unconfirmed. Unheard, the notification left the slot
        in sync with a PIN the lock never took.
        """
        fake_zwave_lock.duplicate_parameters = parameters
        written = await self._async_settle(
            hass, freezer, fake_zwave_lock, fake_zwave_lock_entry, lock_entity
        )
        used = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "5555")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert (
            hass.states.get(
                slot_entity_id(
                    hass, SWITCH_DOMAIN, fake_zwave_lock_entry, 2, CONF_ENABLED
                )
            ).state
            == STATE_OFF
        )
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"slot_disabled_{fake_zwave_lock_entry.entry_id}_2"
        )
        assert issue is not None
        # The worker passes on only that the PIN was refused, not why.
        assert "refused" in issue.translation_placeholders["reason"]
        assert fake_zwave_lock.sets[written:] == [(2, "5555")]
        # Disabled, the slot wants no code, and its old one is cleared.
        assert fake_zwave_lock.holds(2) is None
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "",
        )
        # A refusal is not a use of the slot's code.
        assert used == []

    async def test_a_unique_pin_after_a_refusal_is_taken(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """The user's way out: a PIN no other slot holds, and the slot enabled again."""
        written = await self._async_settle(
            hass, freezer, fake_zwave_lock, fake_zwave_lock_entry, lock_entity
        )
        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "5555")
        await async_advance_sync_ticks(hass, freezer, 3)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_switch_slot(hass, fake_zwave_lock_entry, 2, SERVICE_TURN_ON)
        await async_advance_sync_ticks(hass, freezer, 5)

        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert fake_zwave_lock.sets[written:] == [(2, "5555"), (2, "4321")]
        assert fake_zwave_lock.holds(2) == "4321"

    async def test_a_refused_pin_is_not_what_the_slot_shows(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock: ZWaveJSLock,
        supervised_driver: str | None,
    ) -> None:
        """
        The refused PIN does not stay on the slot while it is being disabled.

        A masking lock's write is announced with the code written, as if
        the lock took it; the slot shows what the lock reports holding. An
        older driver takes a supervised Fail at its word and announces
        nothing.
        """
        pushed = _record_pushes(hass, fake_e2e_lock)
        with pytest.raises(DuplicateCodeError):
            await fake_e2e_lock.async_set_usercode(2, "5555")
        await hass.async_block_till_done()

        assert _first_push(pushed, 2) == (
            None if supervised_driver else SlotCredential.unreadable()
        )
        # The worker then reads the slot: it shows what the lock holds.
        assert _codes(hass, fake_e2e_lock).get(2) != "5555"
        assert fake_zwave_lock.holds(2) == "1234"

    async def test_a_pin_no_other_slot_holds_is_taken(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A write the lock accepts is not taken for a refusal."""
        written = await self._async_settle(
            hass, freezer, fake_zwave_lock, fake_zwave_lock_entry, lock_entity
        )

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert fake_zwave_lock.sets[written:] == [(2, "4321")]
        assert fake_zwave_lock.holds(2) == "4321"

    @pytest.mark.parametrize("parameters", [None, {"userId": 2}], ids=["none", "2"])
    async def test_a_refusal_with_no_write_in_flight_disables_nothing(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        parameters: dict[str, Any] | None,
    ) -> None:
        """A refusal Lock Code Manager did not ask for is not pinned on a slot."""
        written = await self._async_settle(
            hass, freezer, fake_zwave_lock, fake_zwave_lock_entry, lock_entity
        )

        fake_zwave_lock.notify_access_control(
            AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE,
            parameters,
        )
        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert fake_zwave_lock.sets[written:] == [(2, "4321")]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "#1655: a duplicate refusal the provider cannot pin on a write is "
            "announced as a use of the slot it names"
        ),
    )
    async def test_a_refusal_is_never_a_use_of_a_code(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A refusal with no write in flight does not read as someone using that slot's code.

        Some other controller, or the keypad, tried to add a code the lock
        already holds; nobody used slot 2's code.
        """
        await self._async_settle(
            hass, freezer, fake_zwave_lock, fake_zwave_lock_entry, lock_entity
        )
        used = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)

        fake_zwave_lock.notify_access_control(
            AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE,
            {"userId": 2},
        )
        await hass.async_block_till_done()

        assert used == []

    async def test_a_refusal_naming_another_slot_is_not_the_writes(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        fake_e2e_lock: ZWaveJSLock,
    ) -> None:
        """
        A refusal naming a slot other than the one written is not pinned on it.

        Writes to a lock run one at a time, but a slot named outright is not
        the written one: disabling it would guess.
        """
        fake_zwave_lock.duplicate_parameters = {"userId": 5}

        result = await fake_e2e_lock.async_set_usercode(2, "5555")

        assert result in (WriteResult.CONFIRMED, WriteResult.OPTIMISTIC)


async def _async_setup_entry_on_slots(
    hass: HomeAssistant,
    lock_entity: er.RegistryEntry,
    pins: dict[int, str],
    unique_id: str = "test_zwave_js_slots",
    *,
    settle: bool = True,
) -> MockConfigEntry:
    """
    Set up an entry on the Z-Wave JS lock, one user per slot with these PINs.

    ``settle=False`` for a paced test: settling waits for the lock's worker,
    whose pacing waits on the frozen clock.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [lock_entity.entity_id]},
        unique_id=unique_id,
        subentries_data=user_subentries(
            {
                slot: {CONF_NAME: f"slot{slot}", CONF_PIN: pin, CONF_ENABLED: True}
                for slot, pin in pins.items()
            }
        ),
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    if settle:
        await hass.async_block_till_done()
    return entry


def _all_in_sync(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    slots: Iterable[int],
) -> bool:
    """Return whether every slot reports in sync on the lock."""
    return all(
        _slot_states(hass, entry, lock_entity, slot)[0] == STATE_ON for slot in slots
    )


def _suspension(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    slot: int,
) -> ir.IssueEntry | None:
    """Return the repair suspending a slot's sync on the lock, if any."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_suspended_{entry.entry_id}_{lock_entity.entity_id}_{slot}"
    )


def _setup_failure(
    hass: HomeAssistant, lock_entity: er.RegistryEntry
) -> ir.IssueEntry | None:
    """Return the repair saying the lock could not be set up, if any."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, per_lock_issue_id("lock_setup_failed", lock_entity.entity_id)
    )


def _reads(fake_zwave_lock: FakeZWaveLock) -> int:
    """Return how many Gets reached the lock, however they were asked for."""
    return sum(e.operation == "get" for e in fake_zwave_lock.exchanges)


NEW_PINS = {slot: str(slot) * 4 for slot in range(3, 8)}


class TestPacing:
    """Regression for #139: a burst of writes is paced, and none reach a dead node."""

    @pytest.mark.paced
    async def test_writes_at_startup_are_spaced(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Every slot out of sync at once is written one at a time, spaced apart.

        Writes back to back at startup dropped Kwikset locks off the mesh.
        """
        setup = hass.async_create_task(
            _async_setup_entry_on_slots(hass, lock_entity, NEW_PINS, settle=False)
        )
        # Setup itself waits out the delay between its reads.
        await async_advance_clock(hass, freezer, 5 * MIN_OPERATION_DELAY, settle=False)
        assert setup.done()
        entry = setup.result()

        # Each write waits out the delay after the operation before it.
        await async_advance_clock(
            hass, freezer, 4 * len(NEW_PINS) * MIN_OPERATION_DELAY, settle=False
        )

        assert _all_in_sync(hass, entry, lock_entity, NEW_PINS)

        assert fake_zwave_lock.codes.items() >= NEW_PINS.items()
        times = [e.at for e in fake_zwave_lock.exchanges if e.operation == "set"]
        assert len(times) == len(NEW_PINS)
        assert min(b - a for a, b in itertools.pairwise(times)) >= MIN_OPERATION_DELAY
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_nothing_is_sent_to_a_dead_node(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A PIN changed while the node is dead waits for it to come back."""
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        fake_zwave_lock.mark_dead()
        await hass.async_block_till_done()
        before = len(fake_zwave_lock.exchanges)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.exchanges[before:] == []
        assert fake_zwave_lock.holds(2) == "1234"

        # Polls back off while the node is dead, so the next one may be
        # two minutes away.
        fake_zwave_lock.mark_alive()
        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2, max_ticks=80
        )
        assert fake_zwave_lock.sets[-1:] == [(2, "4321")]


class TestInterviewedAsleep:
    """
    A lock whose interview finished while it slept: the driver never cached its slot count.

    The driver then offers zero PIN slots (#1251 on 4.0.x, #1298, PR #1344).
    """

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """The interview cached neither the users count nor the slots."""
        return {"interviewed": False}

    async def test_the_count_is_asked_of_the_lock_and_the_slots_sync(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1251 and PR #1344: zero slots is asked again, not believed.

        Taken at its word, every slot was disabled ("must be between 1 and 0")
        and a re-included lock was refused outright.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )

        assert _setup_failure(hass, lock_entity) is None
        # Asked once: the count is re-read after it, not re-asked.
        assert (
            sum(
                message["command"] == "endpoint.invoke_cc_api"
                and message["methodName"] == "getUsersCount"
                for message in fake_zwave_lock.commands
            )
            == 1
        )
        for slot, config in ZWAVE_JS_LCM_CONFIG_SLOTS.items():
            assert fake_zwave_lock.holds(slot) == config[CONF_PIN]

    async def test_a_lock_that_does_not_answer_says_how_to_fix_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1298: the repair names the remedy, then clears once it is done.

        The lock does not answer the count either. The setup failure said
        the lock "does not advertise PIN credential support", which sent
        users looking at the wrong thing.
        """
        asleep = fake_zwave_lock.drop_reply("get", 0)
        entry = await _async_setup_entry_on_slots(
            hass, lock_entity, {2: "4321"}, unique_id="test_zwave_js_asleep"
        )

        issue = _setup_failure(hass, lock_entity)
        assert issue is not None
        assert "Re-interview the lock" in issue.translation_placeholders["error"]
        assert fake_zwave_lock.sets == []

        # The user wakes the lock, re-interviews it, and reloads.
        asleep.clear()
        fake_zwave_lock.reinterview()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        await _async_settle_in_sync(hass, freezer, entry, lock_entity, 2, max_ticks=10)

        assert _setup_failure(hass, lock_entity) is None
        assert fake_zwave_lock.holds(2) == "4321"
        await hass.config_entries.async_unload(entry.entry_id)


class TestUnconfirmableWrite:
    """
    A lock that takes a PIN and will not show it back, on User Code CC version 2.

    The driver's read-back cannot confirm the write, so it answers
    Error_Unknown though the code landed.
    """

    @pytest.fixture(params=["masked", "withheld"])
    def code_display(self, request: pytest.FixtureRequest) -> str:
        """How the lock shows a code it holds."""
        return request.param

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_v2_state: dict[str, Any], code_display: str
    ) -> dict[str, Any]:
        """The version 2 node, its cache filled as the lock shows codes."""
        for value in lock_schlage_be469_v2_state["values"]:
            if (
                value["commandClass"] == CommandClass.USER_CODE
                and value["property"] == LOCK_USERCODE_PROPERTY
                and value.get("value")
            ):
                value["value"] = (
                    "*" * len(value["value"]) if code_display == "masked" else ""
                )
        return lock_schlage_be469_v2_state

    @pytest.fixture
    def fake_zwave_lock_options(self, code_display: str) -> dict[str, Any]:
        """The lock holds the entry's PINs."""
        return {
            "code_display": code_display,
            "codes": {
                slot: config[CONF_PIN]
                for slot, config in ZWAVE_JS_LCM_CONFIG_SLOTS.items()
            },
        }

    async def test_a_new_pin_is_written_once_and_kept(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regressions for #1251 (masked write, PR #1258) and PR #1408 (withheld code).

        Taken as a rejection, the accepted PIN disabled the slot; read as an
        empty slot, a withheld code was written and verified forever.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        written = len(fake_zwave_lock.sets)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        for _ in range(10):
            await async_advance_sync_ticks(hass, freezer)
            fake_zwave_lock.report(2)

        assert fake_zwave_lock.sets[written:] == [(2, "4321")]
        assert fake_zwave_lock.holds(2) == "4321"
        assert (
            hass.states.get(
                slot_entity_id(
                    hass, SWITCH_DOMAIN, fake_zwave_lock_entry, 2, CONF_ENABLED
                )
            ).state
            == STATE_ON
        )
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )


class TestUnmanagedMaskedSlot:
    """A masking lock holding a code on a slot no entry manages."""

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """The entry's PINs, and 5555 at slot 5, all shown as asterisks."""
        return {
            "code_display": "masked",
            "codes": {1: "9999", 2: "1234", 5: "5555"},
        }

    async def test_the_entry_loads_and_syncs(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #760: a masked code on an unmanaged slot breaks nothing.

        Reading it raised ``RuntimeError: StopIteration``, and no slot loaded.
        """
        fake_zwave_lock.report(5)
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )

        assert fake_zwave_lock_entry.state is ConfigEntryState.LOADED
        assert fake_zwave_lock.holds(5) == "5555"
        assert 5 not in dict(fake_zwave_lock.sets)
        assert 5 not in fake_zwave_lock.clears


class TestRefreshes:
    """What Lock Code Manager reads from a Z-Wave lock, and when."""

    async def test_the_lock_is_not_walked_on_a_timer(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1278: no hourly walk of every slot, or reads of them one by one.

        A walk is one exchange per slot: on a 250-slot lock on S0, about 39
        minutes of radio traffic every hour.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        reads = _reads(fake_zwave_lock)

        await async_advance_clock(
            hass, freezer, timedelta(hours=2, minutes=5).total_seconds()
        )

        assert _reads(fake_zwave_lock) == reads

    async def test_an_unconfirmed_pin_is_confirmed_by_reading_its_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1549: the confirmation read asks only about the slot written.

        On a lossy link a walk of the whole lock never completed, so the
        written PIN never confirmed and the slot was suspended after three.
        Here every walk fails: it starts with the users count, which the lock
        never answers.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        written = len(fake_zwave_lock.sets)
        # The set lands, but the lock's replies to it are lost.
        fake_zwave_lock.drop_reply("set", 2, times=1)
        fake_zwave_lock.fail("get", 0)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.sets[written:] == [(2, "4321")]
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert _suspension(hass, fake_zwave_lock_entry, lock_entity, 2) is None


class TestSlotBeyondTheLock:
    """
    Regression for #1398: a slot the lock does not have is not retried forever.

    Taken as a transient failure, the refusal fed the lock's breaker, and the
    only word the user had was "Update failed 3 consecutive times".
    """

    def _attempts(self, fake_zwave_lock: FakeZWaveLock, slot: int) -> int:
        """Return how many writes of ``slot`` reached the driver."""
        return sum(
            message["command"] == "endpoint.access_control.set_credential"
            and message["credentialSlot"] == slot
            for message in fake_zwave_lock.commands
        )

    async def test_a_slot_past_the_lock_s_count_is_suspended_unwritten(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A lock that says it has one slot is never asked to fill slot 2."""
        fake_zwave_lock.capacity = 1
        fake_zwave_lock.reinterview()
        entry = await _async_setup_entry_on_slots(hass, lock_entity, {2: "4321"})

        await async_advance_sync_ticks(hass, freezer, 10)

        issue = _suspension(hass, entry, lock_entity, 2)
        assert issue is not None
        # The repair's reason is the core's own; it no longer names the cause.
        assert issue.translation_key == "slot_suspended"
        assert self._attempts(fake_zwave_lock, 2) == 0
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_a_slot_the_driver_refuses_is_suspended_after_one_try(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A lock re-interviewed with fewer slots refuses the write, and it is not retried.

        Lock Code Manager still holds the count it read at setup, so the
        driver is the one to refuse it, as Argument_Invalid (322) before any
        radio traffic.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        fake_zwave_lock.capacity = 1
        fake_zwave_lock.reinterview()
        before = self._attempts(fake_zwave_lock, 2)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        issue = _suspension(hass, fake_zwave_lock_entry, lock_entity, 2)
        assert issue is not None
        # The repair's reason is the core's own; it no longer names ZW0322.
        assert issue.translation_key == "slot_suspended"
        assert self._attempts(fake_zwave_lock, 2) - before == 1


class TestLossyLink:
    """A lock whose replies are lost, so writes cannot be confirmed."""

    async def test_the_suspension_says_how_the_link_is_doing(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1397: a lock whose replies are lost ends in a repair.

        The code landed every time; only the replies were lost. The repair no
        longer counts the unanswered reads (the core's reason is fixed text),
        but the slot is held instead of being written for ever.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        fake_zwave_lock.drop_reply(None, 2)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_tick_until(
            hass,
            freezer,
            lambda: (
                _suspension(hass, fake_zwave_lock_entry, lock_entity, 2) is not None
            ),
            max_ticks=60,
            what="slot 2 suspended",
        )

        assert fake_zwave_lock.holds(2) == "4321"


class TestReadBack:
    """Writes the driver reads back, or is told about, on a User Code lock."""

    async def test_a_pin_confirmed_by_its_read_back_is_written_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #873: a version 1 lock without Supervision is not rewritten.

        The cache after the write was stale, so sync wrote the slot again on
        every tick.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        written = len(fake_zwave_lock.sets)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        # Only the driver's read-back tells the cache; the lock sends nothing.
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.sets[written:] == [(2, "4321")]
        assert (
            fake_zwave_lock.cached(CommandClass.USER_CODE, LOCK_USERCODE_PROPERTY, 2)
            == "4321"
        )
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )


class TestSupervisedWrites:
    """
    A lock whose Sets are supervised, so the driver takes their outcome at its word.

    Before 15.25.2 the driver cached nothing a supervised Set wrote, and
    did not read the slot after a failure.
    """

    @pytest.fixture(
        params=["15.25.2", "15.25.1"], ids=["driver_15_25_2", "older_driver"]
    )
    def fake_zwave_lock_options(self, request: pytest.FixtureRequest) -> dict[str, Any]:
        """Which driver the lock sits behind."""
        return {"driver_version": request.param}

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, advertising Supervision CC."""
        return supervised_lock_state(lock_schlage_be469_state)

    async def test_a_write_the_lock_refused_is_put_right(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #1334: a supervised write that failed is written again.

        An older driver does not read the slot after a failure, so its cache
        stays wrong, and the write must not be left at that.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        written = len(fake_zwave_lock.sets)
        fake_zwave_lock.refuse("set", 2, times=1)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.holds(2) == "4321"
        assert fake_zwave_lock.sets[written:] == [(2, "4321"), (2, "4321")]
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )

    async def test_a_write_the_lock_reported_but_never_applied_is_put_right(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A supervised Success the lock did not apply is caught by the read after it.

        Persisted unread, the driver's cache claims the new PIN while the lock
        still holds the old one. The read after the write (PR #1334) sees the
        old code, but the provider then announces the PIN it wrote over it,
        and nothing reads the slot again.
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        written = len(fake_zwave_lock.sets)
        fake_zwave_lock.ignore("set", 2, times=1)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.holds(2) == "4321"
        assert fake_zwave_lock.sets[written:] == [(2, "4321"), (2, "4321")]
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )

    async def test_a_reload_after_a_write_rewrites_nothing(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for PR #1334: the cache a restart reads holds what was written.

        An older driver cached nothing of a supervised write, so after a
        restart the slot read as its old code and was written again.
        """
        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        written = len(fake_zwave_lock.sets)

        assert await hass.config_entries.async_reload(fake_zwave_lock_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.sets[written:] == []
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )


class TestReadAfterAWrite:
    """
    A supervised User Code write is read back once, by the worker, on every driver.

    Before zwave-js/zwave-js#8927 (15.25.2) the driver cached nothing a
    supervised Set wrote. The worker's read-back is a fresh get_credential,
    so it puts that cache right with no read of the provider's own.
    """

    @pytest.fixture(
        params=["15.31.1", "15.25.2", "15.25.1", "not-a-version"],
        ids=["driver_15_31_1", "driver_15_25_2", "older_driver", "unknown_driver"],
    )
    def fake_zwave_lock_options(self, request: pytest.FixtureRequest) -> dict[str, Any]:
        """Put the lock behind a driver."""
        return {"driver_version": request.param}

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, advertising Supervision CC."""
        return supervised_lock_state(lock_schlage_be469_state)

    async def test_a_write_is_read_back_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A new PIN reaches the lock and the driver's cache, and is in sync."""
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        sent = len(fake_zwave_lock.commands)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        await _async_settle_in_sync(
            hass, freezer, fake_zwave_lock_entry, lock_entity, 2
        )

        assert fake_zwave_lock.holds(2) == "4321"
        assert (
            fake_zwave_lock.cached(CommandClass.USER_CODE, LOCK_USERCODE_PROPERTY, 2)
            == "4321"
        )
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        # The write, then the worker's read-back confirming the slot.
        assert [
            message["command"].rsplit(".", 1)[1]
            for message in fake_zwave_lock.commands[sent:]
            if message["command"]
            in (
                "endpoint.access_control.set_credential",
                "endpoint.access_control.get_credential",
            )
        ] == ["set_credential", "get_credential"]


class TestDuplicateRefusedByUserCredentialLock:
    """A User Credential lock holding 5555 at slot 5, which refuses duplicates in band."""

    @pytest.fixture(params=[0, 32], ids=["shows_codes", "hashes_codes"])
    def hash_length(self, request: pytest.FixtureRequest) -> int:
        """Whether the lock sends credential data back (0) or hashes it."""
        return request.param

    @pytest.fixture
    def fake_zwave_lock_options(self, hash_length: int) -> dict[str, Any]:
        """The lock's hashing, as its capabilities report it."""
        return {"max_credential_hash_length": hash_length}

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    async def test_a_refused_pin_disables_the_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        hash_length: int,
    ) -> None:
        """
        Regression for #848: a duplicate is not retried forever.

        The slot is disabled with a repair, whether the lock shows its codes
        (caught before sending) or hashes them (refused in band).
        """
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        fake_zwave_lock.program(5, "5555")
        await hass.async_block_till_done()
        written = len(fake_zwave_lock.sets)

        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "5555")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert (
            hass.states.get(
                slot_entity_id(
                    hass, SWITCH_DOMAIN, fake_zwave_lock_entry, 2, CONF_ENABLED
                )
            ).state
            == STATE_OFF
        )
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"slot_disabled_{fake_zwave_lock_entry.entry_id}_2"
        )
        assert issue is not None
        # The worker passes on only that the PIN was refused, not why.
        assert "refused" in issue.translation_placeholders["reason"]
        # A lock that shows its codes is caught before the PIN is sent; one
        # that hashes them refuses it in band.
        assert fake_zwave_lock.sets[written:] == ([(2, "5555")] if hash_length else [])
        assert fake_zwave_lock.holds(2) is None


class TestOpenBugs:
    """Open Z-Wave JS bugs, each pinned by a strict xfail."""

    async def test_a_joining_entry_s_slots_come_up_though_the_lock_pushes(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A second entry's slot is read and shows its state, whatever the lock reports meanwhile."""
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()

        # Slot 1 is changed at the keypad within the joining entry's first read's delay.
        fake_zwave_lock.program(1, "1111")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)

        state = _slot_states(hass, second_entry, lock_entity, 3)
        await hass.config_entries.async_unload(second_entry.entry_id)
        assert state == (STATE_ON, "5555")

    @pytest.mark.parametrize("code", ["", "0000", "****"])
    async def test_a_pin_the_lock_keeps_dropping_is_suspended_not_rewritten_forever(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        code: str,
    ) -> None:
        """A slot that reads back as not holding its PIN after every write is suspended."""
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(
                hass, freezer, fake_zwave_lock_entry, lock_entity, slot
            )
        sets = len(fake_zwave_lock.sets)
        await _async_change_pin(hass, fake_zwave_lock_entry, 2, "4321")
        for _ in range(20):
            await async_advance_sync_ticks(hass, freezer)
            if len(fake_zwave_lock.sets) > sets:
                sets = len(fake_zwave_lock.sets)
                # A few seconds after each set, the lock says the slot is Available.
                fake_zwave_lock.send_user_code_report(2, CodeSlotStatus.AVAILABLE, code)
                await hass.async_block_till_done()

        # Three strikes, with headroom; today it is one write per tick.
        assert len([s for s in fake_zwave_lock.sets if s[0] == 2]) <= 5
        assert _suspension(hass, fake_zwave_lock_entry, lock_entity, 2) is not None

    async def test_a_slot_whose_condition_is_off_is_cleared_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A stale Available after the clear does not bring the code back.

        Today the slot shows the dropped code and stays out of sync. With the
        mock lock's canned reads the catalog's probe saw the clear reissued
        on most ticks instead; either way it is the same guard.

        The slot has a PIN and is enabled, but its condition is off, so it
        wants nothing on the lock. The lock reports Available paired with
        the code it just dropped, as some locks do after a write (#863).
        """
        hass.states.async_set("input_boolean.access", STATE_OFF)
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            unique_id="test_zwave_js_condition",
            subentries_data=user_subentries(
                {
                    2: {
                        CONF_NAME: "slot2",
                        CONF_PIN: "1234",
                        CONF_ENABLED: True,
                        CONF_CONDITION: "input_boolean.access",
                    }
                }
            ),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await async_tick_until(
            hass,
            freezer,
            lambda: fake_zwave_lock.holds(2) is None,
            max_ticks=5,
            what="slot 2 cleared",
        )
        cleared = len(fake_zwave_lock.clears)

        for _ in range(4):
            fake_zwave_lock.send_user_code_report(2, CodeSlotStatus.AVAILABLE, "1234")
            await async_advance_sync_ticks(hass, freezer)
        await async_advance_sync_ticks(hass, freezer, 6)

        clears = fake_zwave_lock.clears[cleared:]
        states = _slot_states(hass, entry, lock_entity, 2)
        await hass.config_entries.async_unload(entry.entry_id)
        assert len(clears) <= 1
        assert states == (STATE_ON, "")


def _switch(hass: HomeAssistant, entry: MockConfigEntry, slot: int) -> str:
    return hass.states.get(
        slot_entity_id(hass, SWITCH_DOMAIN, entry, slot, CONF_ENABLED)
    ).state


def _disabled_reason(hass: HomeAssistant, entry: MockConfigEntry, slot: int) -> str:
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_disabled_{entry.entry_id}_{slot}"
    )
    assert issue is not None
    return issue.translation_placeholders["reason"]


class TestDuplicates:
    """The restored duplicate refusal, met through the real Z-Wave JS stack."""

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """The lock holds the entry's PINs, and the homeowner's 5555 at slot 5."""
        return {"codes": {1: "9999", 2: "1234", 5: "5555"}}

    async def _settle(self, hass, freezer, entry, lock_entity) -> None:
        await async_advance_sync_ticks(hass, freezer, 5)
        for slot in (1, 2):
            entity_id = in_sync_entity_id(hass, entry, slot, lock_entity.entity_id)
            assert hass.states.get(entity_id).state == STATE_ON

    async def test_the_homeowner_s_code_refusal_says_which_slot_has_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The repair says why the slot was turned off: a duplicate, and of what.

        It reads "... has been disabled. Lock ...: cannot set code on slot 2
        -- PIN duplicates unmanaged slot 5". Saying only that the lock
        "refused" the PIN leaves the user re-enabling it, it turned off again,
        and nothing saying the homeowner's own code is in the way.
        """
        entry = fake_zwave_lock_entry
        await self._settle(hass, freezer, entry, lock_entity)

        await async_set_slot_pin(hass, entry, 2, "5555")
        await async_advance_sync_ticks(hass, freezer, 5)

        assert _switch(hass, entry, 2) == STATE_OFF
        reason = _disabled_reason(hass, entry, 2)
        assert "duplicate" in reason.lower()
        assert "slot 5" in reason

    async def test_swapping_two_users_pins_recovers_once_both_are_set(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Swapping two users' PINs in place turns the first slot edited off.

        Re-enabling it once the other slot moved on puts both in sync and
        clears the repair.
        """
        entry = fake_zwave_lock_entry
        await self._settle(hass, freezer, entry, lock_entity)

        await async_set_slot_pin(hass, entry, 1, "1234")  # slot 2's PIN
        await async_advance_sync_ticks(hass, freezer, 5)
        assert _switch(hass, entry, 1) == STATE_OFF
        assert lcm_issue_ids(hass, "slot_disabled")

        await async_set_slot_pin(hass, entry, 2, "9999")
        await async_advance_sync_ticks(hass, freezer, 5)
        await async_switch_slot(hass, entry, 1, SERVICE_TURN_ON)
        await async_advance_sync_ticks(hass, freezer, 5)

        assert fake_zwave_lock.holds(1) == "1234"
        assert fake_zwave_lock.holds(2) == "9999"
        assert not lcm_issue_ids(hass, "slot_disabled")
        assert not lcm_issue_ids(hass, "slot_suspended")

    async def test_moving_a_pin_to_another_user_after_changing_the_first(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Slot 1 gets a new PIN, then slot 2 takes slot 1's old one: no refusal."""
        entry = fake_zwave_lock_entry
        await self._settle(hass, freezer, entry, lock_entity)

        await async_set_slot_pin(hass, entry, 1, "4321")
        await async_set_slot_pin(hass, entry, 2, "9999")
        await async_advance_sync_ticks(hass, freezer, 5)

        assert fake_zwave_lock.holds(1) == "4321"
        assert fake_zwave_lock.holds(2) == "9999"
        assert not lcm_issue_ids(hass, "slot_disabled")


class TestZWaveReload:
    """The lock's integration reloading underneath a loaded entry."""

    async def test_a_keypad_change_after_zwave_js_reloads_is_put_back(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """After a Z-Wave JS reload, the lock's reports still reach the worker."""
        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)
        assert fake_zwave_lock.holds(2) == "1234"

        assert await hass.config_entries.async_reload(zwave_integration.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)

        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.holds(2) == "1234"
        assert (
            hass.states.get(
                in_sync_entity_id(hass, entry, 2, lock_entity.entity_id)
            ).state
            == STATE_ON
        )

    async def test_a_pin_change_after_zwave_js_reloads_is_written(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """A PIN changed after the lock's integration reloaded reaches the lock."""
        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)

        assert await hass.config_entries.async_reload(zwave_integration.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)

        await async_set_slot_pin(hass, entry, 2, "4321")
        await async_advance_sync_ticks(hass, freezer, 10)

        assert fake_zwave_lock.holds(2) == "4321"
        assert not lcm_issue_ids(hass, "slot_suspended")
        assert not lcm_issue_ids(hass, "lock_offline")


class TestLockLeavesOneOfTwoEntries:
    """A lock two entries share, taken out of one of them."""

    async def _second(self, hass, lock_entity) -> MockConfigEntry:

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            unique_id="second_entry",
            subentries_data=user_subentries(
                {3: {CONF_NAME: "slot3", CONF_PIN: "5555", CONF_ENABLED: True}}
            ),
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    @pytest.mark.parametrize("dead", [False, True], ids=["alive", "dead"])
    async def test_the_other_entry_keeps_syncing_and_the_codes_go(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        dead: bool,
    ) -> None:

        first = fake_zwave_lock_entry
        second = await self._second(hass, lock_entity)
        await async_advance_sync_ticks(hass, freezer, 10)
        assert fake_zwave_lock.holds(1) == "9999"
        assert fake_zwave_lock.holds(3) == "5555"

        if dead:
            fake_zwave_lock.mark_dead()
            await hass.async_block_till_done()
        hass.config_entries.async_update_entry(
            first, options={CONF_LOCKS: [], CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS}
        )
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)
        if dead:
            await async_advance_clock(hass, freezer, 3 * 60 * 60)
            fake_zwave_lock.mark_alive()
            await hass.async_block_till_done()
            await async_advance_clock(hass, freezer, 60 * 60)

        # The removed entry's codes are cleared; the other entry's stays.
        assert fake_zwave_lock.holds(1) is None
        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.holds(3) == "5555"
        fake_zwave_lock.program(3, "4321")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 10)
        assert fake_zwave_lock.holds(3) == "5555"
        assert (
            hass.states.get(
                in_sync_entity_id(hass, second, 3, lock_entity.entity_id)
            ).state
            == STATE_ON
        )
        assert lcm_issue_ids(hass) == []


_BOTH = "User Code lock advertising User Credential"


class TestUserRemoved:
    """A user deleted from the entry: their code leaves the lock."""

    @pytest.fixture(params=["user_code", "user_credential", "both"])
    def lock_schlage_be469_state(
        self, request: pytest.FixtureRequest, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """
        A User Code lock (the BE469), a User Credential one, or one with both.

        The last advertises User Credential CC beside User Code CC but reports
        no User Credential users, so the driver sends User Code (AC:269-288).
        """
        if request.param == "user_credential":
            return user_credential_lock_state(lock_schlage_be469_state)
        if request.param == "both":
            state = copy.deepcopy(lock_schlage_be469_state)
            state["label"] = _BOTH
            state["endpoints"][0]["commandClasses"].append(
                {"id": USER_CREDENTIAL_CC_ID, "name": "User Credential", "version": 1}
            )
            return state
        return lock_schlage_be469_state

    @pytest.fixture
    def fake_zwave_lock_options(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The lock with both reports no User Credential users."""
        return {"max_users": 0} if lock_schlage_be469_state["label"] == _BOTH else {}

    async def test_a_deleted_user_s_code_is_cleared(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The release of a removed slot clears it, and its user with it.

        On a User Code lock there is no lock-side user to carry LCM's tag,
        so a release that adopts no untagged user found no owner and sent
        nothing: the deleted user's PIN still opens the door, the worker
        reads it back twice and raises ``slot_clear_failed`` -- which
        acknowledging only repeats. On a User Credential lock the release
        cleared only the PIN, leaving LCM's tagged user, carrying the
        slot's name, behind.
        """

        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)
        assert fake_zwave_lock.holds(2) == "1234"
        if fake_zwave_lock.node.label == _BOTH:
            assert not fake_zwave_lock.uses_user_credential_cc
        has_users = fake_zwave_lock.uses_user_credential_cc
        assert bool(_users_tagged_for(fake_zwave_lock, 2)) == has_users
        kept = _users_tagged_for(fake_zwave_lock, 1)
        cleared = len(fake_zwave_lock.clears)

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 2)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.holds(1) == "9999"
        assert not lcm_issue_ids(hass, "slot_suspended")
        assert not _users_tagged_for(fake_zwave_lock, 2)
        assert _users_tagged_for(fake_zwave_lock, 1) == kept
        if not has_users:
            # A User Code lock has no user to delete: the slot is cleared.
            assert fake_zwave_lock.clears[cleared:] == [2]


class TestSomeoneElsesCodeAtATurnedOffSlot:
    """A User Credential lock: a homeowner's untagged user at a turned-off slot's index (#1695)."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    async def test_the_slot_reads_clear_and_the_code_stays(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The code is someone else's, not the slot's: in sync, untouched, no repair.

        The clear finds no user tagged for the slot and leaves the code alone
        (#1518), so a slot reading it as its own never came in sync and was
        held with a repair nothing could resolve.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            subentries_data=user_subentries(
                {
                    **ZWAVE_JS_LCM_CONFIG_SLOTS,
                    3: {CONF_NAME: "slot3", CONF_PIN: "3333", CONF_ENABLED: False},
                }
            ),
            unique_id="test_zwave_js_foreign",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for slot in (1, 2, 3):
            await _async_settle_in_sync(hass, freezer, entry, lock_entity, slot)

        # The homeowner adds a user with a PIN at credential slot 3.
        fake_zwave_lock.program(3, "5555")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)

        assert _slot_states(hass, entry, lock_entity, 3) == (STATE_ON, "")
        assert fake_zwave_lock.holds(3) == "5555"
        assert lcm_issue_ids(hass) == []
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_a_new_slot_is_not_offered_over_it(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        The slot reads it as empty; what the lock holds is still occupied (#1693).

        Offered to a new slot, its write would adopt the user at that index
        and overwrite the homeowner's code with Lock Code Manager's.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            subentries_data=user_subentries(ZWAVE_JS_LCM_CONFIG_SLOTS),
            unique_id="test_zwave_js_foreign_occupancy",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The homeowner adds a user with a PIN at credential slot 5.
        fake_zwave_lock.program(5, "5555")
        await hass.async_block_till_done()

        occupancy = await async_read_occupancy(
            hass, None, [lock_entity.entity_id], [4, 5, 6]
        )
        assert occupancy.locks[0].occupied == {5}
        await hass.config_entries.async_unload(entry.entry_id)


class TestReleaseAtSomeoneElsesUser:
    """A User Credential lock: a homeowner's untagged user at a released slot's index."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    async def test_the_homeowner_s_user_and_code_stay(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Releasing a slot never deletes a user LCM did not tag (#1518/#1694).

        The homeowner's user sits at the slot's index and holds a PIN at its
        credential slot: exactly what the set path adopts as a pre-tag LCM
        user. Deleting a user deletes its credentials, so a release that
        adopted it would delete a code LCM never created.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            subentries_data=user_subentries(
                {
                    **ZWAVE_JS_LCM_CONFIG_SLOTS,
                    3: {CONF_NAME: "slot3", CONF_PIN: "3333", CONF_ENABLED: False},
                }
            ),
            unique_id="test_zwave_js_release_foreign",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for slot in (1, 2, 3):
            await _async_settle_in_sync(hass, freezer, entry, lock_entity, slot)

        # The homeowner adds a user with a PIN at credential slot 3.
        fake_zwave_lock.program(3, "5555")
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)
        homeowner = fake_zwave_lock.credentials[(int(UserCredentialType.PIN_CODE), 3)][
            0
        ]
        assert homeowner == 3
        assert parse_tag(fake_zwave_lock.users[homeowner].user_name)[0] is None

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 3)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert homeowner in fake_zwave_lock.users
        assert fake_zwave_lock.holds(3) == "5555"
        assert fake_zwave_lock.holds(1) == "9999"
        assert fake_zwave_lock.holds(2) == "1234"
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_a_user_named_with_the_slot_number_stays(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A homeowner's user named ``3`` is not LCM's slot 3 user.

        A bare slot number is LCM's tag only on a lock whose name limit
        cannot fit ``lcm:<slot>:``; this one fits it, so LCM never names a
        user ``3`` here. Slot 3 was never turned on, yet its release is sent
        once: deleting that user would delete its code, at another index.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            subentries_data=user_subentries(
                {
                    **ZWAVE_JS_LCM_CONFIG_SLOTS,
                    3: {CONF_NAME: "slot3", CONF_PIN: "3333", CONF_ENABLED: False},
                }
            ),
            unique_id="test_zwave_js_release_digit_named",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        for slot in (1, 2, 3):
            await _async_settle_in_sync(hass, freezer, entry, lock_entity, slot)

        # The homeowner's user 7, named "3" in the vendor app, PIN at index 7.
        fake_zwave_lock.program(7, "5555")
        fake_zwave_lock.users[7].user_name = "3"
        fake_zwave_lock._u3c_persist_user(7, UserReportType.USER_MODIFIED)
        await hass.async_block_till_done()

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 3)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert fake_zwave_lock.users[7].user_name == "3"
        assert fake_zwave_lock.holds(7) == "5555"
        assert fake_zwave_lock.holds(1) == "9999"
        assert fake_zwave_lock.holds(2) == "1234"
        await hass.config_entries.async_unload(entry.entry_id)


class TestReleaseOnAShortNameLock:
    """A User Credential lock whose name limit cannot fit ``lcm:<slot>:``."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """Five characters: ``lcm:2:`` does not fit, ``2`` does."""
        return {"max_user_name_length": 5}

    async def test_lcm_s_slot_numbered_user_is_deleted(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """LCM names its user ``2`` here, so releasing slot 2 deletes it."""
        entry = fake_zwave_lock_entry
        await _async_settle_in_sync(hass, freezer, entry, lock_entity, 2)
        assert _users_tagged_for(fake_zwave_lock, 2)
        assert all(
            user.user_name.isdecimal() for user in fake_zwave_lock.users.values()
        )

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 2)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert not _users_tagged_for(fake_zwave_lock, 2)
        assert _users_tagged_for(fake_zwave_lock, 1)
        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.holds(1) == "9999"


class TestReleaseKeepsWhatLCMDidNotWrite:
    """A User Credential lock: LCM's tagged user may hold credentials LCM never wrote."""

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The synthesized User Credential dump."""
        return user_credential_lock_state(lock_schlage_be469_state)

    @pytest.fixture
    def fake_zwave_lock_options(self) -> dict[str, Any]:
        """A lock that also takes RFID tags and DESFire cards."""
        return {
            "other_credential_types": (
                UserCredentialType.RFID_CODE,
                UserCredentialType.DESFIRE,
            )
        }

    async def test_a_user_holding_only_lcm_s_pin_is_deleted(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Nothing but LCM's PIN on the user: the user goes, and the PIN with it."""
        entry = fake_zwave_lock_entry
        await _async_settle_in_sync(hass, freezer, entry, lock_entity, 2)
        (user_id,) = _users_tagged_for(fake_zwave_lock, 2)
        assert [
            key
            for key, (owner, _) in fake_zwave_lock.credentials.items()
            if owner == user_id
        ] == [(int(UserCredentialType.PIN_CODE), 2)]

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 2)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert user_id not in fake_zwave_lock.users
        assert not any(
            owner == user_id for owner, _ in fake_zwave_lock.credentials.values()
        )
        assert fake_zwave_lock.holds(2) is None
        assert fake_zwave_lock.holds(1) == "9999"

    @pytest.mark.parametrize(
        "kind",
        [UserCredentialType.RFID_CODE, UserCredentialType.DESFIRE],
        ids=["rfid", "desfire"],
    )
    async def test_a_credential_added_in_the_vendor_app_stays(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        kind: UserCredentialType,
    ) -> None:
        """
        LCM's PIN goes; the user and the card someone added to it stay.

        Deleting the user would cascade to the card. DESFire is a type LCM
        has no name for, so ``async_get_users`` drops it: the release must
        still see it.
        """
        entry = fake_zwave_lock_entry
        await _async_settle_in_sync(hass, freezer, entry, lock_entity, 2)
        (user_id,) = _users_tagged_for(fake_zwave_lock, 2)
        name = fake_zwave_lock.users[user_id].user_name
        fake_zwave_lock.enroll(user_id, kind, 5, "04A2B3C4")
        await hass.async_block_till_done()

        assert hass.config_entries.async_remove_subentry(
            entry, subentry_id_for_slot(entry, 2)
        )
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert fake_zwave_lock.holds(2) is None
        assert (int(UserCredentialType.PIN_CODE), 2) not in fake_zwave_lock.credentials
        assert fake_zwave_lock.users[user_id].user_name == name
        assert fake_zwave_lock.credentials[(int(kind), 5)] == (user_id, "04A2B3C4")
        assert fake_zwave_lock.holds(1) == "9999"


class TestRestartMidWrite:
    """The entry going down (a restart, a reload) with a write out to the lock."""

    async def test_a_write_cut_off_by_a_reload_lands_after_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """The new PIN reaches the lock once the entry is back; no repair is left."""

        entry = fake_zwave_lock_entry
        await async_advance_sync_ticks(hass, freezer, 5)
        with fake_zwave_lock.hang("set", 2) as fault:
            await hass.services.async_call(
                TEXT_DOMAIN,
                SERVICE_SET_VALUE,
                {ATTR_VALUE: "4321"},
                target={
                    ATTR_ENTITY_ID: slot_entity_id(hass, "text", entry, 2, CONF_PIN)
                },
                blocking=True,
            )
            await fault.async_wait_held()
            reload = hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )
            for _ in range(20):
                await asyncio.sleep(0)
        assert await reload
        await hass.async_block_till_done()
        await async_advance_clock(hass, freezer, 10 * 60)

        assert fake_zwave_lock.holds(2) == "4321"
        assert (
            hass.states.get(
                in_sync_entity_id(hass, entry, 2, lock_entity.entity_id)
            ).state
            == STATE_ON
        )
        assert lcm_issue_ids(hass) == []


class TestReEnableAfterUnconfirmedClear:
    """A slot turned back on after its clear's read-back failed."""

    @pytest.fixture(name="lock_schlage_be469_state")
    def supervised_state_fixture(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, advertising Supervision CC: the clear is not verified."""
        return supervised_lock_state(lock_schlage_be469_state)

    async def test_the_slot_shows_pending_not_unknown(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Found on a production test: a slot turned back on went unavailable.

        The user was turned off and the clear landed. The lock reported the
        slot empty, then the read-back failed with a transient Z-Wave error
        (ZW1405). Turned back on a few seconds later, the slot's sensors went
        unavailable and the card showed the lock as unknown until the PIN
        was back. They stay available, out of sync (pending) until then.
        """
        entry = fake_zwave_lock_entry
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(hass, freezer, entry, lock_entity, slot)

        with fake_zwave_lock.fail(
            "get",
            1,
            times=1,
            error_code=1405,
            error_message="The node failed to decode the message. (ZW1405)",
        ) as fault:
            fail = fault.async_before

            async def _report_then_fail() -> None:
                # The lock's report of the empty slot gets through, then the
                # read-back fails.
                fake_zwave_lock.report(1)
                await fail()

            fault.async_before = _report_then_fail  # type: ignore[method-assign]
            await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
            # Less than the slot's wait before it tries again.
            await async_advance_clock(hass, freezer, 8)
        assert fault.hits == 1
        assert fake_zwave_lock.holds(1) is None
        assert _slot_states(hass, entry, lock_entity, 1) == (STATE_ON, "")

        states = _record_slot_states(hass, entry, lock_entity, 1)
        await async_switch_slot(hass, entry, 1, SERVICE_TURN_ON)
        await async_advance_clock(hass, freezer, 60)

        assert fake_zwave_lock.holds(1) == "9999"
        assert states[0] == (STATE_OFF, "")
        assert all(STATE_UNAVAILABLE not in seen for seen in states), states
        assert _slot_states(hass, entry, lock_entity, 1) == (STATE_ON, "9999")


class TestReEnableWhileClearing:
    """A user turned off, then back on before the clear is confirmed."""

    @pytest.fixture(name="lock_schlage_be469_state")
    def supervised_state_fixture(
        self, lock_schlage_be469_state: dict[str, Any]
    ) -> dict[str, Any]:
        """The BE469 dump, advertising Supervision CC: the clear is not verified."""
        return supervised_lock_state(lock_schlage_be469_state)

    @pytest.mark.parametrize(
        "clear_out",
        [
            # Production test 2 (and test 3's slower locks): back on while the
            # clear is still with the lock. The lock reports the slot Available
            # once the clear lands.
            pytest.param(True, id="clear_out"),
            # Production test 3's first lock: back on once its clear is in.
            pytest.param(False, id="clear_in"),
        ],
    )
    async def test_the_slot_shows_pending_then_takes_the_pin_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_zwave_lock: FakeZWaveLock,
        fake_zwave_lock_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        clear_out: bool,
    ) -> None:
        """
        Found on a production test: turned back on, the slot went unavailable.

        Wanting the PIN again while the clear was unconfirmed set the slot's
        reading aside, to read it before writing (``Slot.doubt``), and the
        sensors went unavailable while it was checked: the card showed "?".
        They stay available: off (pending) with an unknown code, then in sync
        with the PIN, written once.
        """
        entry = fake_zwave_lock_entry
        for slot in ZWAVE_JS_LCM_CONFIG_SLOTS:
            await _async_settle_in_sync(hass, freezer, entry, lock_entity, slot)
        sets = list(fake_zwave_lock.sets)
        states = _record_slot_states(hass, entry, lock_entity, 1)

        with fake_zwave_lock.hang("clear", 1) as fault:
            await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
            await fault.async_wait_held()
            if clear_out:
                await async_switch_slot(hass, entry, 1, SERVICE_TURN_ON)
        # The clear lands, and the lock says so.
        fake_zwave_lock.report(1)
        if not clear_out:
            await async_advance_clock(hass, freezer, 8)
            assert _slot_states(hass, entry, lock_entity, 1) == (STATE_ON, "")
            await async_switch_slot(hass, entry, 1, SERVICE_TURN_ON)
        await async_advance_clock(hass, freezer, 60)

        assert fake_zwave_lock.clears == [1]
        assert fake_zwave_lock.sets == [*sets, (1, "9999")]
        assert fake_zwave_lock.holds(1) == "9999"
        assert (STATE_OFF, STATE_UNKNOWN if clear_out else "") in states, states
        assert all(STATE_UNAVAILABLE not in seen for seen in states), states
        assert _slot_states(hass, entry, lock_entity, 1) == (STATE_ON, "9999")


class TestEmptySlotsAreReported:
    """A lock that can read its codes is never taken for one that cannot (#1581)."""

    async def test_setup_on_empty_slots_says_nothing_of_the_lock(
        self,
        hass: HomeAssistant,
        fake_zwave_lock: FakeZWaveLock,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Every slot the flow reads is empty: that is the lock describing them.

        The read projects only the slots an entry manages, and a new entry
        manages none, so the empty slots it asked about come back unnamed --
        answered all the same, not silent.
        """
        fake_zwave_lock.program(1, None)
        fake_zwave_lock.program(2, None)
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        flow_id = result["flow_id"]
        await async_configure_flow(
            hass, flow_id, {CONF_NAME: "zwave", CONF_LOCKS: [lock_entity.entity_id]}
        )
        await async_configure_flow(hass, flow_id, {"next_step_id": "ui"})
        result = await async_configure_flow(hass, flow_id, {CONF_NUM_USERS: 2})
        assert result["step_id"] == "code_slot", result.get("errors")
        for user, pin in (("user1", "1234"), ("user2", "5678")):
            result = await async_configure_flow(
                hass, flow_id, {CONF_NAME: user, CONF_PIN: pin, CONF_ENABLED: True}
            )

        assert result["type"] == "create_entry"
        assert result["description"] is None
        assert locks_that_cannot_report(hass, [lock_entity.entity_id]) == []
        entry = hass.config_entries.async_get_entry(result["result"].entry_id)
        assert {
            subentry.title: subentry.data[CONF_SLOT]
            for subentry in entry.subentries.values()
        } == {"user1": 1, "user2": 2}
        assert await hass.config_entries.async_unload(entry.entry_id)
