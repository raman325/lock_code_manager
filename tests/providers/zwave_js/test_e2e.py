"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import timedelta
import itertools
import re
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
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.lock_code_manager.const import (
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
)
from custom_components.lock_code_manager.domain.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.domain.credentials import (
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.domain.exceptions import DuplicateCodeError
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.domain.util import per_lock_issue_id
from custom_components.lock_code_manager.providers._base import MIN_OPERATION_DELAY
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_switch_slot,
    async_tick_until,
    code_entity_id,
    in_sync_entity_id,
    slot_entity_id,
    user_subentries,
)
from tests.conftest import async_trigger_sync_tick
from tests.providers.zwave_js.conftest import (
    ZWAVE_JS_LCM_CONFIG_SLOTS,
    get_zwave_lock,
)
from tests.providers.zwave_js.fake_node import (
    FakeZWaveLock,
    supervised_lock_state,
    user_credential_lock_state,
)
from tests.providers.zwave_js.helpers import (
    make_uc_value_event,
)


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


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Z-Wave JS provider."""

    async def test_provider_discovered_as_zwave_js(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Z-Wave JS lock and creates a ZWaveJSLock."""
        lock = lcm_config_entry.runtime_data.locks.get(lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, ZWaveJSLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_zwave_lock.coordinator is not None

    async def test_push_subscription_established(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
    ) -> None:
        """The provider subscribes to Z-Wave JS value updates during setup."""
        assert e2e_zwave_lock._push_unsubs


class TestSetAndClearUsercodes:
    """Verify set/clear operations invoke the unified access-control primitives."""

    async def test_set_usercode_calls_lock_helpers(
        self,
        hass: HomeAssistant,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        zwave_integration: MockConfigEntry,
    ) -> None:
        """Setting a code drives async_set_user then async_set_credential via lock_helpers."""
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zwave_js_lock.lock.entity_id],
                CONF_SLOTS: {"4": {}},
            },
        )
        lcm_entry.add_to_hass(hass)
        zwave_js_lock._min_operation_delay = 0.0
        mock_access_control.get_user_cached.return_value = None
        mock_lock_helpers["async_set_user"].return_value = {"user_id": 4}

        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is WriteResult.CONFIRMED
        mock_lock_helpers["async_set_user"].assert_called_once()
        mock_lock_helpers["async_set_credential"].assert_called_once()

    async def test_clear_usercode_calls_lock_helpers(
        self,
        hass: HomeAssistant,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        zwave_integration: MockConfigEntry,
    ) -> None:
        """Clearing a slot resolves the owner then calls async_delete_credential."""
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zwave_js_lock.lock.entity_id],
                CONF_SLOTS: {"2": {}},
            },
        )
        lcm_entry.add_to_hass(hass)
        zwave_js_lock._min_operation_delay = 0.0
        mock_access_control.get_users_cached.return_value = [
            UserData(
                user_id=2,
                active=True,
                user_type=UserCredentialUserType.GENERAL,
                user_name="bob",
            ),
        ]
        mock_access_control.get_all_credentials_cached.return_value = [
            CredentialData(
                user_id=2,
                type=UserCredentialType.PIN_CODE,
                slot=2,
                data="1234",
            ),
        ]

        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True
        mock_lock_helpers["async_delete_credential"].assert_called_once()


class TestGetUsercodes:
    """Verify reading usercodes from the access_control API."""

    async def test_get_usercodes_returns_codes_from_access_control(
        self,
        hass: HomeAssistant,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        zwave_integration: MockConfigEntry,
    ) -> None:
        """
        async_get_usercodes projects access_control users and credentials to slots.

        The access_control fixture is seeded with two users at slots 1 and 2.
        The result maps each slot to the readable Personal Identification Number.
        """
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [zwave_js_lock.lock.entity_id],
                CONF_SLOTS: {"1": {}, "2": {}},
            },
        )
        lcm_entry.add_to_hass(hass)
        mock_access_control.get_users_cached.return_value = [
            UserData(
                user_id=1,
                active=True,
                user_type=UserCredentialUserType.GENERAL,
                user_name="alice",
            ),
            UserData(
                user_id=2,
                active=True,
                user_type=UserCredentialUserType.GENERAL,
                user_name="bob",
            ),
        ]
        mock_access_control.get_all_credentials_cached.return_value = [
            CredentialData(
                user_id=1,
                type=UserCredentialType.PIN_CODE,
                slot=1,
                data="9999",
            ),
            CredentialData(
                user_id=2,
                type=UserCredentialType.PIN_CODE,
                slot=2,
                data="1234",
            ),
        ]

        codes = await zwave_js_lock.async_get_usercodes()

        assert codes[1] == SlotCredential.known("9999")
        assert codes[2] == SlotCredential.known("1234")


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

    async def test_push_credential_added_reaches_coordinator(
        self,
        hass: HomeAssistant,
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
    ) -> None:
        """A credential-added node event for a Personal Identification Number updates the coordinator."""
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
        # Identification Number value in the event; a coordinator refresh reads it).
        assert (
            e2e_zwave_lock.coordinator.data.get(pin_address(1))
            == SlotCredential.unreadable()
        )


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
        assert lock.coordinator.data.get(pin_address(3)) == SlotCredential.known("5555")

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

        assert lock.coordinator.data.get(pin_address(1)) == SlotCredential.empty()

        await hass.config_entries.async_unload(second_entry.entry_id)

    @pytest.mark.parametrize(
        "state",
        [
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.UNLOAD_IN_PROGRESS,
            ConfigEntryState.NOT_LOADED,
        ],
        ids=[
            "setting_up",
            "retrying_setup",
            "unloading_to_reload",
            "between_unload_and_setup",
        ],
    )
    async def test_an_enabled_entry_not_loaded_keeps_its_slots(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        lock_schlage_be469: Node,
        state: ConfigEntryState,
    ) -> None:
        """
        An enabled entry's slots stay its own through setup and a reload.

        It syncs them before its setup finishes, and a reload unloads and sets
        it up again without anybody releasing them, so a stale Available on
        such a slot is still ignored while its code is wanted.
        """
        joining_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_joining")
        joining_entry.add_to_hass(hass)
        joining_entry.mock_state(hass, state)
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

        assert lock.coordinator.data.get(pin_address(3)) == SlotCredential.known("5555")

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

        assert lock.coordinator.data.get(pin_address(3)) == SlotCredential.empty()


def _lock_holds_on_slot_3(access_control: MagicMock, pin: str) -> None:
    """Make the lock's next read report ``pin`` on slot 3."""
    access_control.get_users_cached.return_value = [
        UserData(
            user_id=3,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="slot3",
        )
    ]
    access_control.get_all_credentials_cached.return_value = [
        CredentialData(user_id=3, type=UserCredentialType.PIN_CODE, slot=3, data=pin)
    ]


async def _assert_a_refresh_reads_the_lock(
    lock: ZWaveJSLock, access_control: MagicMock
) -> None:
    """Assert the lock's coordinator still reads the lock when refreshed."""
    _lock_holds_on_slot_3(access_control, "7777")
    await lock.coordinator.async_refresh()
    assert lock.coordinator.data.get(pin_address(3)) == SlotCredential.known("7777")


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

        await _assert_a_refresh_reads_the_lock(lock, mock_access_control)

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

        await _assert_a_refresh_reads_the_lock(lock, mock_access_control)

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
        fake_zwave_lock.program(3, "0000")
        await hass.async_block_till_done()

        assert _slot_states(hass, second_entry, lock_entity, 3) == (STATE_OFF, "0000")
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
    async def test_the_coordinator_shuts_down_once_the_last_entry_unloads(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        creating_entry_last: bool,
    ) -> None:
        """
        The coordinator runs until the last entry using the lock unloads, then stops.

        Watched from before the coordinator exists, so a shutdown registered
        while it is built is counted with the one Lock Code Manager runs.
        """
        with patch.object(
            LockUsercodeUpdateCoordinator,
            "async_shutdown",
            autospec=True,
            side_effect=LockUsercodeUpdateCoordinator.async_shutdown,
        ) as shutdown:
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
            first, last = (
                (second_entry, creating_entry)
                if creating_entry_last
                else (creating_entry, second_entry)
            )

            assert await hass.config_entries.async_unload(first.entry_id)
            await hass.async_block_till_done()
            shutdown.assert_not_called()
            await _assert_a_refresh_reads_the_lock(lock, mock_access_control)

            assert await hass.config_entries.async_unload(last.entry_id)
            await hass.async_block_till_done()
            shutdown.assert_awaited_once_with(lock.coordinator)


class TestColdStartRace:
    """Regression tests for issue #1321: LCM setup racing zwave_js startup."""

    async def test_lcm_setup_survives_zwave_entry_not_loaded(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control,
        mock_lock_helpers: dict,
    ) -> None:
        """LCM setup while zwave_js is still loading degrades and recovers.

        Reproduces issue #1321: the zwave_js config entry has not reached
        LOADED when LCM sets up (slow boot). The lock must not be dropped;
        it stays registered in a degraded state and recovers automatically
        when the zwave_js entry finishes loading.
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

        # The lock survived setup in a degraded state instead of being dropped.
        lock = lcm_entry.runtime_data.locks.get(lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, ZWaveJSLock)
        assert lock.coordinator is not None
        assert lock._setup_succeeded is False

        # zwave_js finishes loading -> the LOADED transition drives recovery.
        assert await hass.config_entries.async_setup(zwave_integration.entry_id)
        await hass.async_block_till_done()

        assert lock._setup_succeeded is True
        assert lock._push_unsubs

        await hass.config_entries.async_unload(lcm_entry.entry_id)

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

        # The lock's reports reach the slot: it is listened to, not just read.
        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()
        assert _slot_states(hass, lcm_entry, lock_entity, 2) == (STATE_OFF, "4321")

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

        assert (
            fake_e2e_lock.coordinator.data.get(pin_address(UNWANTED_SLOT))
            == SlotCredential.empty()
        )

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
        fake_zwave_lock.send_user_code_report(
            WANTED_SLOT, CodeSlotStatus.AVAILABLE, code
        )
        await hass.async_block_till_done()

        assert fake_e2e_lock.coordinator.data.get(
            pin_address(WANTED_SLOT)
        ) == SlotCredential.known(code)

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
        fake_zwave_lock.send_user_code_report(code_slot, CodeSlotStatus.ENABLED, code)
        await hass.async_block_till_done()

        assert fake_e2e_lock.coordinator.data.get(pin_address(code_slot)) == expected

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

        fake_zwave_lock.program(2, "4321")
        await hass.async_block_till_done()
        assert _slot_states(hass, fake_zwave_lock_entry, lock_entity, 2) == (
            STATE_OFF,
            "4321",
        )

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

    @pytest.fixture
    def lock_schlage_be469_state(
        self, lock_schlage_be469_state: dict[str, Any], code_display: str
    ) -> dict[str, Any]:
        """The node's value database as the lock filled it at inclusion."""
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
    def fake_zwave_lock_options(self, code_display: str) -> dict[str, Any]:
        """The lock holds the entry's PINs, and 5555 at slot 5."""
        return {
            "code_display": code_display,
            "codes": {1: "9999", 2: "1234", 5: "5555"},
        }

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
        assert "duplicate" in issue.translation_placeholders["reason"]
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
    ) -> None:
        """
        The refused PIN does not stay on the slot while it is being disabled.

        A masking lock's write is announced with the code written, as if
        the lock took it; the slot shows what the lock reports holding.
        """
        with pytest.raises(DuplicateCodeError):
            await fake_e2e_lock.async_set_usercode(2, "5555")

        assert fake_e2e_lock.coordinator.credentials_by_slot()[2] == (
            SlotCredential.unreadable()
        )
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
) -> MockConfigEntry:
    """Set up an entry on the Z-Wave JS lock, one user per slot with these PINs."""
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
            _async_setup_entry_on_slots(hass, lock_entity, NEW_PINS)
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
        assert any(
            message["command"] == "endpoint.invoke_cc_api"
            and message["methodName"] == "getUsersCount"
            for message in fake_zwave_lock.commands
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
        assert "out of range" in issue.translation_placeholders["reason"]
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
        assert "ZW0322" in issue.translation_placeholders["reason"]
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
        Regression for #1397: the repair counts the reads that went unanswered.

        The code landed every time; only the replies were lost. "The lock may
        be rejecting the code" sent the user after the wrong cause.
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

        reason = _suspension(
            hass, fake_zwave_lock_entry, lock_entity, 2
        ).translation_placeholders["reason"]
        counted = re.search(r"(\d+) read requests went unanswered", reason)
        assert counted is not None
        assert 0 < int(counted[1]) <= fake_zwave_lock.statistics["timeoutResponse"]
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
        assert fake_zwave_lock.cached(
            CommandClass.USER_CODE, LOCK_USERCODE_PROPERTY, 2
        ) == ("4321")
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

    @pytest.fixture(params=[False, True], ids=["driver_15_25_2", "older_driver"])
    def fake_zwave_lock_options(self, request: pytest.FixtureRequest) -> dict[str, Any]:
        """Which driver the lock sits behind."""
        return {"driver_before_15_25_2": request.param}

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

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "#1654: the provider pushes the PIN it wrote over what its own "
            "post-write read found, so a supervised Success the lock did not "
            "apply shows in sync"
        ),
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

        The slot is disabled and the repair names the duplicate. A lock that
        shows its codes is not asked: the duplicate is seen first. One that
        hashes them refuses the write.
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
        assert "duplicate" in issue.translation_placeholders["reason"]
        assert fake_zwave_lock.sets[written:] == ([(2, "5555")] if hash_length else [])
        assert fake_zwave_lock.holds(2) is None
