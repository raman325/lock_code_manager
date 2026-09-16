"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from zwave_js_server.const.command_class.access_control import (
    UserCredentialType,
    UserCredentialUserType,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.access_control import CredentialData, UserData
from zwave_js_server.model.node import Node

from homeassistant.const import CONF_ENABLED, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_SYNC_STATUS,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    PENDING_WRITE_TTL,
)
from custom_components.lock_code_manager.domain.credentials import (
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock
from tests.common import in_sync_entity_id, write_entry_config
from tests.providers.zwave_js.conftest import ZWAVE_JS_LCM_CONFIG_SLOTS


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


def _cache_holds(access_control: MagicMock, node: Node, pin: str) -> None:
    """Make the driver's cache show slot 1 holding ``pin``, as it never stops doing."""
    node.values[f"{node.node_id}-99-0-userIdStatus-1"].update({"value": 1})
    node.values[f"{node.node_id}-99-0-userCode-1"].update({"value": pin})
    access_control.get_users_cached.return_value = [
        UserData(user_id=1, active=True, user_type=UserCredentialUserType.GENERAL)
    ]
    access_control.get_all_credentials_cached.return_value = [
        CredentialData(user_id=1, type=UserCredentialType.PIN_CODE, slot=1, data=pin)
    ]


async def _settle_then_reread(
    hass: HomeAssistant, lcm_entry: MockConfigEntry, lock_entity_id: str, freezer
) -> None:
    """
    Run well past the breaker's window, then read the lock and run again.

    The read comes through the cache that never saw the write, as a
    refresh after the lock comes back does.
    """
    coordinator = lcm_entry.runtime_data.locks[lock_entity_id].coordinator
    for lap in range(24):
        if lap == 12:
            await coordinator.async_refresh()
        freezer.tick(timedelta(seconds=PENDING_WRITE_TTL))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


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


class TestUnconfirmableWrites:
    """A write the driver could not read back is trusted, not retried to suspension."""

    async def test_a_write_the_lock_never_reads_back_settles_in_sync(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """
        Issue #1307: the code lands, but the lock never shows it.

        A lock without Supervision is verified by the driver reading the code
        back. On a lossy link that read times out, the driver reports the
        outcome as unknown, and its cache keeps showing the slot empty. Every
        later read comes through the same path and shows the same nothing,
        which is no evidence the write failed.
        """
        mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
            translation_key="credential_rejected_unknown"
        )
        # The driver's cache still says the slots are free: the reads that
        # would have updated it never came back.
        node = lock_schlage_be469
        for slot in (1, 2):
            node.values[f"{node.node_id}-99-0-userIdStatus-{slot}"].update({"value": 0})
            node.values[f"{node.node_id}-99-0-userCode-{slot}"].update({"value": ""})
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [lock_entity.entity_id],
                CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS,
            },
            unique_id="test_zwave_js_unconfirmable",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)

        # Well past the breaker's window: repeated write-and-give-up laps
        # would keep the slot out of sync and then suspend it.
        for _ in range(12):
            freezer.tick(timedelta(seconds=PENDING_WRITE_TTL))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

        state = hass.states.get(in_sync)
        assert state is not None
        assert state.attributes.get(ATTR_SYNC_STATUS) == "in_sync"
        assert state.state == STATE_ON
        writes = [
            call
            for call in mock_lock_helpers["async_set_credential"].await_args_list
            if call.args[3] == "9999"
        ]
        assert len(writes) == 1

        # Disabling the user has to take the code off the lock, although the
        # driver's cache still shows nothing to take.
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        slots[1][CONF_ENABLED] = False
        assert write_entry_config(
            hass,
            lcm_entry,
            {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
        )
        for _ in range(6):
            freezer.tick(timedelta(seconds=PENDING_WRITE_TTL))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()

        deletes = [
            call
            for call in mock_lock_helpers["async_delete_credential"].await_args_list
            if call.args[3] == 1
        ]
        assert [call.args[1:] for call in deletes] == [
            (1, UserCredentialType.PIN_CODE, 1)
        ]
        state = hass.states.get(in_sync)
        assert state is not None
        assert state.state == STATE_ON

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_changed_code_the_lock_never_reads_back_settles_in_sync(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """
        The same, over a code the driver's cache still shows.

        A cache that never saw the write keeps answering with the code the
        slot held before, which is no more evidence than an empty slot.
        """
        mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
            translation_key="credential_rejected_unknown"
        )
        _cache_holds(mock_access_control, lock_schlage_be469, "4444")
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [lock_entity.entity_id],
                CONF_SLOTS: ZWAVE_JS_LCM_CONFIG_SLOTS,
            },
            unique_id="test_zwave_js_unconfirmable_change",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)

        await _settle_then_reread(hass, lcm_entry, lock_entity.entity_id, freezer)

        state = hass.states.get(in_sync)
        assert state is not None
        assert state.state == STATE_ON
        writes = [
            call
            for call in mock_lock_helpers["async_set_credential"].await_args_list
            if call.args[3] == "9999"
        ]
        assert len(writes) == 1

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_clear_the_lock_never_reads_back_settles_in_sync(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """
        A delete the driver could not read back is trusted too.

        Its cache keeps showing the code that was there, so a clear judged by
        that cache would be repeated until the slot was suspended.
        """
        mock_lock_helpers["async_delete_credential"].side_effect = HomeAssistantError(
            translation_key="credential_rejected_unknown"
        )
        _cache_holds(mock_access_control, lock_schlage_be469, "9999")
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
            unique_id="test_zwave_js_unconfirmable_clear",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)

        slots = copy.deepcopy(slots)
        slots[1][CONF_ENABLED] = False
        assert write_entry_config(
            hass,
            lcm_entry,
            {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
        )
        await _settle_then_reread(hass, lcm_entry, lock_entity.entity_id, freezer)

        deletes = [
            call
            for call in mock_lock_helpers["async_delete_credential"].await_args_list
            if call.args[3] == 1
        ]
        assert len(deletes) == 1
        state = hass.states.get(in_sync)
        assert state is not None
        assert state.state == STATE_ON

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    @pytest.mark.parametrize(
        ("read_between", "expected_deletes"),
        [
            pytest.param(None, 2, id="the_cache_shows_nothing"),
            pytest.param("4321", 1, id="the_cache_shows_another_code"),
        ],
    )
    async def test_releasing_the_slot_clears_it_through_the_same_user(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
        read_between: str | None,
        expected_deletes: int,
    ) -> None:
        """
        Removing the user clears the slot again, though the cache names no owner.

        Neither the write nor the clear reached the driver's cache, so the
        user they went through is the only one that can hold the code. Once
        the cache shows the slot holding a code of someone else's, that user
        is no longer a guess worth acting on: releasing a slot leaves a code
        it cannot attribute alone.
        """
        unknown = HomeAssistantError(translation_key="credential_rejected_unknown")
        mock_lock_helpers["async_set_credential"].side_effect = unknown
        mock_lock_helpers["async_delete_credential"].side_effect = unknown
        mock_lock_helpers["async_set_user"].return_value = {"user_id": 7}
        node = lock_schlage_be469
        node.values[f"{node.node_id}-99-0-userIdStatus-1"].update({"value": 0})
        node.values[f"{node.node_id}-99-0-userCode-1"].update({"value": ""})
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
            unique_id="test_zwave_js_unconfirmable_release",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await _settle_then_reread(hass, lcm_entry, lock_entity.entity_id, freezer)

        slots = copy.deepcopy(slots)
        slots[1][CONF_ENABLED] = False
        assert write_entry_config(
            hass, lcm_entry, {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots}
        )
        await _settle_then_reread(hass, lcm_entry, lock_entity.entity_id, freezer)
        if read_between is not None:
            _cache_holds(mock_access_control, lock_schlage_be469, read_between)
            coordinator = lcm_entry.runtime_data.locks[
                lock_entity.entity_id
            ].coordinator
            await coordinator.async_refresh()

        del slots[1]
        assert write_entry_config(
            hass, lcm_entry, {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots}
        )
        await hass.async_block_till_done()

        deletes = [
            call.args[1:]
            for call in mock_lock_helpers["async_delete_credential"].await_args_list
            if call.args[3] == 1
        ]
        assert deletes == [(7, UserCredentialType.PIN_CODE, 1)] * expected_deletes

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_disabling_a_user_whose_write_is_still_pending_clears_it(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """
        Sync drops the pending write before it clears the slot.

        The clear must still reach the user the write went to, though
        nothing is in doubt any more by the time it runs.
        """
        mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
            translation_key="credential_rejected_unknown"
        )
        mock_lock_helpers["async_set_user"].return_value = {"user_id": 7}
        node = lock_schlage_be469
        node.values[f"{node.node_id}-99-0-userIdStatus-1"].update({"value": 0})
        node.values[f"{node.node_id}-99-0-userCode-1"].update({"value": ""})
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
            unique_id="test_zwave_js_unconfirmable_pending_disable",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        for _ in range(10):
            if mock_lock_helpers["async_set_credential"].await_count:
                break
            freezer.tick(timedelta(seconds=1))
            async_fire_time_changed(hass)
            await hass.async_block_till_done()
        coordinator = lcm_entry.runtime_data.locks[lock_entity.entity_id].coordinator
        assert coordinator.pending_write(pin_address(1)) is not None

        slots = copy.deepcopy(slots)
        slots[1][CONF_ENABLED] = False
        assert write_entry_config(
            hass, lcm_entry, {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots}
        )
        await _settle_then_reread(hass, lcm_entry, lock_entity.entity_id, freezer)

        deletes = [
            call.args[1:]
            for call in mock_lock_helpers["async_delete_credential"].await_args_list
            if call.args[3] == 1
        ]
        assert deletes == [(7, UserCredentialType.PIN_CODE, 1)]

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    @pytest.mark.parametrize(
        ("cache_after", "expected_deletes"),
        [
            # The PIN change was never shown: the cache still names the old
            # code, under a user this release may not adopt.
            pytest.param("stale", 1, id="the_cache_still_shows_the_old_code"),
            # Someone's code was read there, then the cache lost sight of it
            # again: occupied, value withheld, so it names no user.
            pytest.param("masked", 0, id="a_read_settled_it_first"),
        ],
    )
    async def test_releasing_a_slot_follows_what_was_learned_not_the_cache(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
        cache_after: str,
        expected_deletes: int,
    ) -> None:
        """Only a slot still in doubt is cleared through the user it was written to."""
        mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
            translation_key="credential_rejected_unknown"
        )
        mock_lock_helpers["async_set_user"].return_value = {"user_id": 7}
        _cache_holds(mock_access_control, lock_schlage_be469, "4444")
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
            unique_id="test_zwave_js_unconfirmable_release_learned",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await _settle_then_reread(hass, lcm_entry, lock_entity.entity_id, freezer)

        if cache_after == "masked":
            coordinator = lcm_entry.runtime_data.locks[
                lock_entity.entity_id
            ].coordinator
            coordinator.push_update({1: SlotCredential.known("2468")})
            mock_access_control.get_users_cached.return_value = []
            mock_access_control.get_all_credentials_cached.return_value = []

        del slots[1]
        assert write_entry_config(
            hass, lcm_entry, {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots}
        )
        await hass.async_block_till_done()

        deletes = [
            call.args[1:]
            for call in mock_lock_helpers["async_delete_credential"].await_args_list
            if call.args[3] == 1
        ]
        assert deletes == [(7, UserCredentialType.PIN_CODE, 1)] * expected_deletes

        await hass.config_entries.async_unload(lcm_entry.entry_id)
