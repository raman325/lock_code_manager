"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
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
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.access_control import CredentialData, UserData
from zwave_js_server.model.node import Node

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.domain.credentials import (
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock
from tests.common import (
    async_advance_sync_ticks,
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
from tests.providers.zwave_js.helpers import make_uc_value_event


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


def _user_code_report(node: Node, code_slot: int, status: int, code: str) -> None:
    """
    Deliver one User Code report for a slot, the way the driver persists it.

    node-zwave-js writes ``userIdStatus`` before ``userCode``
    (``UserCodeCC.ts`` ``persistUserCode``), so the status is in the value
    database by the time the code's value event arrives.
    """
    for property_name, value in (
        (LOCK_USERCODE_STATUS_PROPERTY, status),
        (LOCK_USERCODE_PROPERTY, code),
    ):
        node.receive_event(
            ZwaveEvent(
                type="value updated",
                data={
                    "source": "node",
                    "event": "value updated",
                    "nodeId": node.node_id,
                    "args": {
                        "commandClassName": "User Code",
                        "commandClass": CommandClass.USER_CODE,
                        "endpoint": 0,
                        "property": property_name,
                        "propertyKey": code_slot,
                        "newValue": value,
                        "propertyName": property_name,
                    },
                },
            )
        )


# A push lock is read only when asked, and Home Assistant debounces the asking:
# a read asked for during the cooldown of the last one runs up to ten seconds
# later. Five sync ticks, and a few more for the sync to act on what it read.
DEBOUNCED_READ_TICKS = 8


@dataclass
class _UserCodeLock:
    """
    A User Code lock as the driver's unified access-control API presents it.

    Each occupied slot reads as an implicit user, numbered as its slot,
    carrying that slot's one PIN. A write through Home Assistant's lock
    helpers changes what the lock holds and is recorded, and the lock
    answers it with the User Code report the driver turns into value
    events: Enabled with the code field it shows after a set, Available
    with the one it leaves after a clear.
    """

    node: Node
    codes: dict[int, str]
    # A masking lock shows an asterisk per digit wherever it would show a code.
    masks_codes: bool = False
    # What a cleared slot's code field still carries: nothing, a masked
    # placeholder, or the code it held.
    code_after_clear: str = ""
    sets: list[tuple[int, str]] = field(default_factory=list)
    clears: list[int] = field(default_factory=list)

    def shown(self, pin: str) -> str:
        """Return the code field the lock shows for ``pin``."""
        return "*" * len(pin) if self.masks_codes else pin

    def users(self) -> list[UserData]:
        """Answer a users read."""
        return [
            UserData(
                user_id=slot, active=True, user_type=UserCredentialUserType.GENERAL
            )
            for slot in self.codes
        ]

    def credentials(self) -> list[CredentialData]:
        """Answer a credentials read."""
        return [
            CredentialData(
                user_id=slot,
                type=UserCredentialType.PIN_CODE,
                slot=slot,
                data=self.shown(pin),
            )
            for slot, pin in self.codes.items()
        ]

    def set_user(
        self, node: Node, user_id: int | None = None, **_fields: Any
    ) -> dict[str, int]:
        """Answer a user write with the user it names, or the first free one."""
        free = next(slot for slot in count(1) if slot not in self.codes)
        return {"user_id": free if user_id is None else user_id}

    def set_credential(
        self,
        node: Node,
        user_id: int,
        credential_type: UserCredentialType,
        pin: str,
        credential_slot: int,
    ) -> dict[str, int]:
        """Hold ``pin`` on the slot and report it."""
        self.codes[credential_slot] = pin
        self.sets.append((credential_slot, pin))
        _user_code_report(
            self.node, credential_slot, CodeSlotStatus.ENABLED, self.shown(pin)
        )
        return {"credential_slot": credential_slot, "user_id": user_id}

    def delete_credential(
        self,
        node: Node,
        user_id: int,
        credential_type: UserCredentialType,
        credential_slot: int,
    ) -> None:
        """Empty the slot and report it Available."""
        del self.codes[credential_slot]
        self.clears.append(credential_slot)
        _user_code_report(
            self.node, credential_slot, CodeSlotStatus.AVAILABLE, self.code_after_clear
        )


@pytest.fixture
def user_code_lock(
    lock_schlage_be469: Node, mock_access_control: MagicMock, mock_lock_helpers: dict
) -> _UserCodeLock:
    """
    Make the Z-Wave JS lock a User Code lock holding the entry's PINs.

    The node's value database already reports those PINs on their slots.
    Request this before ``lcm_config_entry``, so the entry sets up against it.
    """
    lock = _UserCodeLock(
        lock_schlage_be469,
        {slot: config[CONF_PIN] for slot, config in ZWAVE_JS_LCM_CONFIG_SLOTS.items()},
    )
    for read in ("get_users_cached", "get_users"):
        getattr(mock_access_control, read).side_effect = lock.users
    for read in ("get_all_credentials_cached", "get_all_credentials"):
        getattr(mock_access_control, read).side_effect = lock.credentials
    mock_lock_helpers["async_set_user"].side_effect = lock.set_user
    mock_lock_helpers["async_set_credential"].side_effect = lock.set_credential
    mock_lock_helpers["async_delete_credential"].side_effect = lock.delete_credential
    return lock


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
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        creator_disabled: bool,
    ) -> None:
        """
        Regression for #1623: a stale Available on the second entry's slot is ignored.

        Whether a PIN is wanted was asked only of the entry that set the
        shared lock up, which does not own slot 3, so the stale Available
        was believed and the second entry's code written again.
        """
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        if creator_disabled:
            assert await hass.config_entries.async_set_disabled_by(
                lcm_config_entry.entry_id, ConfigEntryDisabler.USER
            )
            await hass.async_block_till_done()
        await _async_settle_in_sync(
            hass, freezer, second_entry, lock_entity, 3, max_ticks=DEBOUNCED_READ_TICKS
        )
        assert user_code_lock.sets == [(3, "5555")]

        user_code_lock.node.receive_event(
            make_uc_value_event(
                user_code_lock.node.node_id,
                LOCK_USERCODE_STATUS_PROPERTY,
                3,
                CodeSlotStatus.AVAILABLE,
            )
        )
        await async_advance_sync_ticks(hass, freezer, 3)

        assert _slot_states(hass, second_entry, lock_entity, 3) == (STATE_ON, "5555")
        assert user_code_lock.sets == [(3, "5555")]

        await hass.config_entries.async_unload(second_entry.entry_id)

    async def test_the_second_entrys_withheld_code_shows_its_own_pin(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1623: a code the lock withholds shows the PIN of the slot's entry.

        The code sensor stands in the configured PIN for a code the lock will
        not show. Asked of the entry that set the shared lock up, slot 3 had
        no PIN, and the second entry's sensor showed none.
        """
        user_code_lock.masks_codes = True
        second_entry = _entry_on_slot_3(lock_entity, "test_zwave_js_e2e_second")
        second_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(second_entry.entry_id)
        await hass.async_block_till_done()
        await _async_settle_in_sync(
            hass, freezer, second_entry, lock_entity, 3, max_ticks=DEBOUNCED_READ_TICKS
        )

        _user_code_report(user_code_lock.node, 3, CodeSlotStatus.ENABLED, "****")
        await async_advance_sync_ticks(hass, freezer, 2)

        assert _slot_states(hass, second_entry, lock_entity, 3) == (STATE_ON, "5555")
        # Masked, the creating entry's slots read unreadable and are written too.
        assert [write for write in user_code_lock.sets if write[0] == 3] == [
            (3, "5555")
        ]

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
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
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
                lcm_config_entry.entry_id, ConfigEntryDisabler.USER
            )
        else:
            assert await hass.config_entries.async_reload(lcm_config_entry.entry_id)
        await hass.async_block_till_done()
        user_code_lock.codes[3] = "0000"
        _user_code_report(user_code_lock.node, 3, CodeSlotStatus.ENABLED, "0000")
        await hass.async_block_till_done()

        assert _slot_states(hass, second_entry, lock_entity, 3) == (STATE_OFF, "0000")
        await _async_settle_in_sync(hass, freezer, second_entry, lock_entity, 3)
        assert user_code_lock.codes[3] == "5555"

        await hass.config_entries.async_unload(second_entry.entry_id)

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
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        user_code_lock: _UserCodeLock,
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
        user_code_lock.codes[2] = "4321"
        _user_code_report(user_code_lock.node, 2, CodeSlotStatus.ENABLED, "4321")
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
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
        code: str,
    ) -> None:
        """
        Available means the slot holds nothing, whatever its code field says.

        A lock that masks its codes reports an Available slot with a masked
        placeholder; read as a code, it was the PIN ``****`` (#819).
        """
        _user_code_report(
            lock_schlage_be469, UNWANTED_SLOT, CodeSlotStatus.AVAILABLE, code
        )
        await hass.async_block_till_done()

        assert (
            e2e_zwave_lock.coordinator.data.get(pin_address(UNWANTED_SLOT))
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
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
        code: str,
    ) -> None:
        """
        Where a PIN is wanted, an Available status alone does not empty the slot.

        Some locks announce Available after a PIN lands (#863); taken as
        empty, sync would rewrite the slot forever. The code field is read
        as it always was.
        """
        _user_code_report(
            lock_schlage_be469, WANTED_SLOT, CodeSlotStatus.AVAILABLE, code
        )
        await hass.async_block_till_done()

        assert e2e_zwave_lock.coordinator.data.get(
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
        e2e_zwave_lock: ZWaveJSLock,
        lock_schlage_be469: Node,
        code_slot: int,
        code: str,
        expected: SlotCredential,
    ) -> None:
        """An Enabled slot holds its code, or a code it will not say."""
        _user_code_report(lock_schlage_be469, code_slot, CodeSlotStatus.ENABLED, code)
        await hass.async_block_till_done()

        assert e2e_zwave_lock.coordinator.data.get(pin_address(code_slot)) == expected

    @pytest.mark.parametrize(
        "code_after_clear",
        [pytest.param("****", id="masked"), pytest.param("1234", id="leftover_code")],
    )
    async def test_a_cleared_slot_reported_available_stays_cleared(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        code_after_clear: str,
    ) -> None:
        """
        Regression for #819: a cleared slot reported Available reads empty.

        The lock answers the clear with Available and a code field still
        carrying a masked placeholder or the old code. Read as a code, the
        slot never looked cleared and was cleared again on every report.
        """
        user_code_lock.code_after_clear = code_after_clear
        await _async_settle_in_sync(hass, freezer, lcm_config_entry, lock_entity, 2)

        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            target={
                ATTR_ENTITY_ID: slot_entity_id(
                    hass, SWITCH_DOMAIN, lcm_config_entry, 2, CONF_ENABLED
                )
            },
            blocking=True,
        )
        await _async_settle_in_sync(hass, freezer, lcm_config_entry, lock_entity, 2)
        # The lock repeats the report, as it does on every later read.
        _user_code_report(
            user_code_lock.node, 2, CodeSlotStatus.AVAILABLE, code_after_clear
        )
        await async_advance_sync_ticks(hass, freezer, 3)

        assert _slot_states(hass, lcm_config_entry, lock_entity, 2) == (STATE_ON, "")
        assert user_code_lock.clears == [2]

    @pytest.mark.parametrize(
        "with_code", [False, True], ids=["status_only", "status_and_code"]
    )
    async def test_a_stale_available_after_a_set_does_not_rewrite_the_slot(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        with_code: bool,
    ) -> None:
        """
        Regression for #863: an Available after a set does not empty a wanted slot.

        Some locks announce the slot Available a few seconds after the PIN
        lands. A code field that did not change raises no value event of its
        own, so the status can arrive alone. Taken as empty, sync rewrote the
        slot, and the next stale Available started it over.
        """
        await _async_change_pin(hass, lcm_config_entry, 2, "4321")
        await _async_settle_in_sync(hass, freezer, lcm_config_entry, lock_entity, 2)
        assert user_code_lock.sets == [(2, "4321")]

        if with_code:
            _user_code_report(user_code_lock.node, 2, CodeSlotStatus.AVAILABLE, "4321")
        else:
            user_code_lock.node.receive_event(
                make_uc_value_event(
                    user_code_lock.node.node_id,
                    LOCK_USERCODE_STATUS_PROPERTY,
                    2,
                    CodeSlotStatus.AVAILABLE,
                )
            )
        await async_advance_sync_ticks(hass, freezer, 3)

        assert _slot_states(hass, lcm_config_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert user_code_lock.sets == [(2, "4321")]

    async def test_a_code_changed_at_the_keypad_is_put_back(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1327: a User Code report of a keypad change reaches sync.

        A lock routed to User Code raises no unified credential event for a
        change programmed at the keypad, only the report's value events.
        Unheard, the slot kept showing the PIN it no longer held.
        """
        await _async_settle_in_sync(hass, freezer, lcm_config_entry, lock_entity, 2)

        user_code_lock.codes[2] = "4321"
        _user_code_report(user_code_lock.node, 2, CodeSlotStatus.ENABLED, "4321")
        await hass.async_block_till_done()
        assert _slot_states(hass, lcm_config_entry, lock_entity, 2) == (
            STATE_OFF,
            "4321",
        )

        await _async_settle_in_sync(hass, freezer, lcm_config_entry, lock_entity, 2)
        assert user_code_lock.sets == [(2, "1234")]
        assert user_code_lock.codes[2] == "1234"

    async def test_a_masked_code_after_a_set_is_not_rewritten(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        user_code_lock: _UserCodeLock,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #776: a masking lock's report of a set PIN is not a new code.

        The lock reports every slot it holds as asterisks. Read as the code
        ``****``, a PIN just written never matched, and sync rewrote it after
        every report, the code alternating between the PIN and ``****``.
        """
        user_code_lock.masks_codes = True
        await _async_change_pin(hass, lcm_config_entry, 2, "4321")
        await _async_settle_in_sync(hass, freezer, lcm_config_entry, lock_entity, 2)
        assert user_code_lock.sets == [(2, "4321")]

        _user_code_report(user_code_lock.node, 2, CodeSlotStatus.ENABLED, "****")
        await hass.async_block_till_done()
        for _ in range(3):
            assert _slot_states(hass, lcm_config_entry, lock_entity, 2)[0] == STATE_ON
            await async_advance_sync_ticks(hass, freezer)

        assert _slot_states(hass, lcm_config_entry, lock_entity, 2) == (
            STATE_ON,
            "4321",
        )
        assert user_code_lock.sets == [(2, "4321")]
