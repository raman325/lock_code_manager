"""Full lifecycle E2E tests for Z-Wave JS lock provider."""

from __future__ import annotations

import copy
from datetime import timedelta
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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

from homeassistant.const import CONF_ENABLED, STATE_OFF, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.lock_code_manager.const import (
    ATTR_SYNC_STATUS,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    PENDING_WRITE_TTL,
    UNCONFIRMED_RETRY_MAX,
)
from custom_components.lock_code_manager.domain.credentials import (
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.domain.exceptions import LockOperationFailed
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


_UNKNOWN = "credential_rejected_unknown"


def _cache_holds(access_control: MagicMock, node: Node, pin: str | None) -> None:
    """Make the driver's cache show slot 1 holding ``pin``, or nothing."""
    node.values[f"{node.node_id}-99-0-userIdStatus-1"].update(
        {"value": 0 if pin is None else 1}
    )
    node.values[f"{node.node_id}-99-0-userCode-1"].update({"value": pin or ""})
    access_control.get_users_cached.return_value = (
        []
        if pin is None
        else [
            UserData(user_id=1, active=True, user_type=UserCredentialUserType.GENERAL)
        ]
    )
    access_control.get_all_credentials_cached.return_value = (
        []
        if pin is None
        else [
            CredentialData(
                user_id=1, type=UserCredentialType.PIN_CODE, slot=1, data=pin
            )
        ]
    )


async def _run_for(hass: HomeAssistant, freezer, laps: int) -> None:
    """Let the given number of write deadlines pass, one at a time."""
    for _ in range(laps):
        freezer.tick(timedelta(seconds=PENDING_WRITE_TTL))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


def _suspended(hass: HomeAssistant) -> bool:
    """Return whether any slot of the lock has been suspended."""
    return any(
        issue_id.startswith("slot_suspended_")
        for (_, issue_id) in ir.async_get(hass).issues
    )


def _timed_unknown_writes(helpers: dict) -> list[tuple[float, str]]:
    """Make every PIN write come back unknown, and note when each went out."""
    writes: list[tuple[float, str]] = []

    def unknown(*args: Any, **kwargs: Any) -> None:
        writes.append((time.monotonic(), args[3]))
        raise HomeAssistantError(translation_key=_UNKNOWN)

    helpers["async_set_credential"].side_effect = unknown
    return writes


def _gaps(writes: list[tuple[float, str]], pin: str) -> list[float]:
    """Return the seconds between consecutive writes of ``pin``."""
    times = [at for at, written in writes if written == pin]
    return [later - earlier for earlier, later in zip(times, times[1:], strict=False)]


class TestUnconfirmedWrites:
    """A write or clear the driver could not read back is retried, not suspended."""

    async def _setup(
        self, hass: HomeAssistant, lock_entity: er.RegistryEntry, slots: dict
    ) -> MockConfigEntry:
        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
            unique_id="test_zwave_js_unconfirmed",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        return lcm_entry

    async def test_a_write_the_lock_never_reads_back_is_retried_not_suspended(
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
        outcome as unknown, and its cache keeps showing the slot empty. That
        is no evidence the write failed, so the slot says it is unconfirmed
        and tries again less and less often, rather than being suspended
        after three tries.
        """
        mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
            translation_key=_UNKNOWN
        )
        _cache_holds(mock_access_control, lock_schlage_be469, None)
        lcm_entry = await self._setup(hass, lock_entity, ZWAVE_JS_LCM_CONFIG_SLOTS)
        in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)

        # Twenty minutes: three strikes suspend within five.
        await _run_for(hass, freezer, 20)

        writes = [
            call
            for call in mock_lock_helpers["async_set_credential"].await_args_list
            if call.args[3] == "9999"
        ]
        # First try, then waits of one, two, four and eight minutes.
        assert 3 <= len(writes) <= 5
        state = hass.states.get(in_sync)
        assert state is not None
        assert state.state == STATE_OFF
        assert state.attributes.get(ATTR_SYNC_STATUS) == "unconfirmed"
        assert not _suspended(hass)

        # The lock finally shows it: in sync, with nothing written again.
        _cache_holds(mock_access_control, lock_schlage_be469, "9999")
        coordinator = lcm_entry.runtime_data.locks[lock_entity.entity_id].coordinator
        await coordinator.async_refresh()
        await _run_for(hass, freezer, 1)
        state = hass.states.get(in_sync)
        assert state is not None
        assert state.state == STATE_ON
        assert state.attributes.get(ATTR_SYNC_STATUS) == "in_sync"

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_changed_pin_is_written_without_waiting_out_the_retry(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """The wait is for the same attempt; a new PIN is a new one."""
        writes = _timed_unknown_writes(mock_lock_helpers)
        _cache_holds(mock_access_control, lock_schlage_be469, None)
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        lcm_entry = await self._setup(hass, lock_entity, slots)
        # Long enough that the next retry is minutes away.
        await _run_for(hass, freezer, 8)

        slots[1]["pin"] = "8888"
        assert write_entry_config(
            hass, lcm_entry, {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots}
        )
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=5))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

        assert any(at for at, pin in writes if pin == "8888")
        # A new target starts the wait over.
        await _run_for(hass, freezer, 6)
        assert _gaps(writes, "8888")[0] <= 3 * PENDING_WRITE_TTL
        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_clear_the_lock_never_reads_back_is_retried_not_suspended(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """A delete the driver could not read back gets the same treatment."""
        mock_lock_helpers["async_delete_credential"].side_effect = HomeAssistantError(
            translation_key=_UNKNOWN
        )
        _cache_holds(mock_access_control, lock_schlage_be469, "9999")
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        slots[1][CONF_ENABLED] = False
        lcm_entry = await self._setup(hass, lock_entity, slots)
        in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)

        await _run_for(hass, freezer, 20)

        deletes = [
            call
            for call in mock_lock_helpers["async_delete_credential"].await_args_list
            if call.args[3] == 1
        ]
        assert 3 <= len(deletes) <= 5
        state = hass.states.get(in_sync)
        assert state is not None
        assert state.attributes.get(ATTR_SYNC_STATUS) == "unconfirmed"
        assert not _suspended(hass)

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_the_wait_between_retries_grows_but_not_past_an_hour(
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
        A lock that never confirms is asked less and less, then hourly.

        And once it is seen in sync, the next unconfirmed write starts the
        wait over rather than picking up where it left off.
        """
        writes = _timed_unknown_writes(mock_lock_helpers)
        _cache_holds(mock_access_control, lock_schlage_be469, None)
        lcm_entry = await self._setup(hass, lock_entity, ZWAVE_JS_LCM_CONFIG_SLOTS)

        await _run_for(hass, freezer, 6 * 60)

        gaps = _gaps(writes, "9999")
        assert len(gaps) >= 7
        assert gaps == sorted(gaps)
        # The wait itself, plus the time it takes to give a write up.
        assert max(gaps) <= UNCONFIRMED_RETRY_MAX + 2 * PENDING_WRITE_TTL
        assert gaps[-1] > UNCONFIRMED_RETRY_MAX

        # Seen in sync, then lost again: the wait starts over.
        coordinator = lcm_entry.runtime_data.locks[lock_entity.entity_id].coordinator
        _cache_holds(mock_access_control, lock_schlage_be469, "9999")
        await coordinator.async_refresh()
        await _run_for(hass, freezer, 1)
        _cache_holds(mock_access_control, lock_schlage_be469, None)
        writes.clear()
        await coordinator.async_refresh()
        await _run_for(hass, freezer, 6)
        assert _gaps(writes, "9999")[0] <= 3 * PENDING_WRITE_TTL

        await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_write_whose_reads_all_fail_is_not_taken_as_in_sync(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """No read replaced the value the write put in place, so it is withdrawn."""
        writes = _timed_unknown_writes(mock_lock_helpers)
        _cache_holds(mock_access_control, lock_schlage_be469, None)
        with patch.object(
            ZWaveJSLock,
            "async_hard_refresh_codes",
            AsyncMock(side_effect=LockOperationFailed("node timed out")),
        ):
            lcm_entry = await self._setup(hass, lock_entity, ZWAVE_JS_LCM_CONFIG_SLOTS)
            in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)
            await _run_for(hass, freezer, 20)

            state = hass.states.get(in_sync)
            assert state is not None
            assert state.state == STATE_OFF
            assert state.attributes.get(ATTR_SYNC_STATUS) == "unconfirmed"
            assert 3 <= len([pin for _, pin in writes if pin == "9999"]) <= 5
            assert not _suspended(hass)
            await hass.config_entries.async_unload(lcm_entry.entry_id)

    @pytest.mark.parametrize(
        ("cache", "clear_reaches_the_lock"),
        [
            # The cache never saw the write: the clear finds no owner and
            # sends nothing.
            pytest.param(None, False, id="nothing_to_clear"),
            # The cache shows the old code under its user: the clear goes out.
            pytest.param("4444", True, id="cleared"),
        ],
    )
    async def test_disabling_a_slot_the_lock_never_confirmed(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
        cache: str | None,
        clear_reaches_the_lock: bool,
    ) -> None:
        """
        Only a clear that did something speaks for the slot.

        One that found nothing to clear leaves the unconfirmed PIN unverified,
        so enabling the user again is not taken as in sync on the write alone.
        """
        _timed_unknown_writes(mock_lock_helpers)
        _cache_holds(mock_access_control, lock_schlage_be469, cache)
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        with patch.object(
            ZWaveJSLock,
            "async_hard_refresh_codes",
            AsyncMock(side_effect=LockOperationFailed("node timed out")),
        ):
            lcm_entry = await self._setup(hass, lock_entity, slots)
            in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)
            await _run_for(hass, freezer, 2)
            coordinator = lcm_entry.runtime_data.locks[
                lock_entity.entity_id
            ].coordinator
            assert not coordinator.is_verified(pin_address(1))

            disabled = copy.deepcopy(slots)
            disabled[1][CONF_ENABLED] = False
            assert write_entry_config(
                hass,
                lcm_entry,
                {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: disabled},
            )
            for _ in range(3):
                freezer.tick(timedelta(seconds=3))
                async_fire_time_changed(hass)
                await hass.async_block_till_done()
            assert mock_lock_helpers["async_delete_credential"].await_count == (
                1 if clear_reaches_the_lock else 0
            )
            state = hass.states.get(in_sync)
            assert state is not None
            assert (state.state == STATE_ON) is clear_reaches_the_lock

            if not clear_reaches_the_lock:
                # Nothing to clear and nothing read: it waits, not a clear
                # attempt on every tick.
                await _run_for(hass, freezer, 3)
                clears = mock_lock_helpers["async_delete_credential"].await_count
                assert clears == 0
                assert state.attributes.get(ATTR_SYNC_STATUS) != "in_sync"
                manager = lcm_entry.runtime_data.slot_coordinators[1].sync_managers[0]
                attempts = manager._unconfirmed_attempts
                assert 1 <= attempts <= 3
                assert write_entry_config(
                    hass,
                    lcm_entry,
                    {CONF_LOCKS: [lock_entity.entity_id], CONF_SLOTS: slots},
                )
                await hass.async_block_till_done()
                freezer.tick(timedelta(seconds=5))
                async_fire_time_changed(hass)
                await hass.async_block_till_done()
                state = hass.states.get(in_sync)
                assert state is not None
                assert state.state == STATE_OFF

            await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_clear_the_lock_never_confirmed_is_read_back_at_once(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        lock_schlage_be469: Node,
        freezer,
    ) -> None:
        """One that landed settles on that read, with nothing sent again."""
        mock_lock_helpers["async_delete_credential"].side_effect = HomeAssistantError(
            translation_key=_UNKNOWN
        )
        _cache_holds(mock_access_control, lock_schlage_be469, "9999")
        slots = copy.deepcopy(ZWAVE_JS_LCM_CONFIG_SLOTS)
        slots[1][CONF_ENABLED] = False
        refreshed = AsyncMock(
            return_value={
                1: SlotCredential.empty(),
                2: SlotCredential.known("1234"),
            }
        )
        with patch.object(ZWaveJSLock, "async_hard_refresh_codes", refreshed):
            lcm_entry = await self._setup(hass, lock_entity, slots)
            in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)
            await _run_for(hass, freezer, 20)

            deletes = [
                call
                for call in mock_lock_helpers["async_delete_credential"].await_args_list
                if call.args[3] == 1
            ]
            assert len(deletes) == 1
            assert refreshed.await_args_list[0].args == ({1},)
            state = hass.states.get(in_sync)
            assert state is not None
            assert state.state == STATE_ON
            await hass.config_entries.async_unload(lcm_entry.entry_id)

    async def test_a_changed_pin_the_cache_never_shows_is_not_suspended(
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
        The driver's cache keeps showing the code the slot held before.

        That is the same read the driver could not complete, so the change is
        unconfirmed, not a failure three strikes turn into a suspension.
        """
        writes = _timed_unknown_writes(mock_lock_helpers)
        _cache_holds(mock_access_control, lock_schlage_be469, "4444")
        lcm_entry = await self._setup(hass, lock_entity, ZWAVE_JS_LCM_CONFIG_SLOTS)
        in_sync = in_sync_entity_id(hass, lcm_entry, 1, lock_entity.entity_id)

        await _run_for(hass, freezer, 20)

        assert 3 <= len([pin for _, pin in writes if pin == "9999"]) <= 5
        state = hass.states.get(in_sync)
        assert state is not None
        assert state.attributes.get(ATTR_SYNC_STATUS) == "unconfirmed"
        assert not _suspended(hass)
        await hass.config_entries.async_unload(lcm_entry.entry_id)
