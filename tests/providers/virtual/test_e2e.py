"""Full lifecycle E2E tests for Virtual lock provider."""

import logging
from unittest.mock import PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_NAME,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_SYNC_STATUS,
    ATTR_TARGET,
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    credential_in_sync_key,
)
from custom_components.lock_code_manager.ha.adapter import ProviderPort, _Coordinator
from custom_components.lock_code_manager.ha.allocation import async_read_occupancy
from custom_components.lock_code_manager.ha.runtime import (
    async_give_up,
    async_reconcile_issues,
    async_want,
    lock_registry,
    signal_lock_changed,
)
from custom_components.lock_code_manager.model.config import build_slot_unique_id
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialRef,
    CredentialType,
    SlotCredential,
    credential_from_slot,
    pin_address,
)
from custom_components.lock_code_manager.providers.virtual import VirtualLock
from custom_components.lock_code_manager.sync.slot import Holds, Repair, Seen, Slot
from custom_components.lock_code_manager.sync.worker import LockWorker
from tests.common import (
    async_configure_flow,
    code_entity_id,
    in_sync_entity_id,
    user_subentries,
)

from ...common import TICK_INTERVAL
from .conftest import VIRTUAL_LOCK_ENTITY_ID, get_virtual_lock

SLOT_1_EVENT_ENTITY = "event.mock_title_slot1_credential_used"
SLOT_1_PIN_ENTITY = "text.mock_title_slot1_pin"


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Virtual provider."""

    async def test_provider_discovered_as_virtual(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
    ) -> None:
        """Verify LCM discovers the virtual lock and creates a VirtualLock."""
        lock = lcm_config_entry.runtime_data.locks.get(VIRTUAL_LOCK_ENTITY_ID)
        assert lock is not None
        assert isinstance(lock, VirtualLock)

    async def test_always_connected(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Virtual locks are always connected."""
        assert await e2e_virtual_lock.async_is_integration_connected()


class TestSetAndGetUsercodes:
    """Verify set/get/clear operations through the E2E-initialized provider."""

    async def test_set_and_get_usercode(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set a credential, then get_usercodes via base projection and verify it is present."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="test_user",
            source="direct",
        )

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] == SlotCredential.known("1111")

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set a credential, clear it, and verify it is gone."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="test_user",
            source="direct",
        )
        await e2e_virtual_lock.async_delete_credential(
            CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        )

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] is SlotCredential.empty()

    async def test_base_orchestration_set_and_get(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set via async_set_usercode (the hook the worker writes through) and verify via projection."""
        await e2e_virtual_lock.async_set_usercode(1, "2222", "orchestrated")

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] == SlotCredential.known("2222")

    async def test_base_orchestration_clear(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set then clear via the worker's write hooks; verify projection shows empty."""
        await e2e_virtual_lock.async_set_usercode(1, "3333", "user")
        await e2e_virtual_lock.async_clear_usercode(1)

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] is SlotCredential.empty()

    async def test_codes_persist_across_unload_and_reload(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set codes, unload (persists to store), reload, verify codes survive."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="user1",
            source="direct",
        )
        await e2e_virtual_lock.async_set_credential(
            2,
            credential_from_slot(2, SlotCredential.known("2222")),
            "2222",
            name="user2",
            source="direct",
        )

        # Unload without removing permanently saves data to the store
        config_entry = e2e_virtual_lock._lcm_config_entry
        await e2e_virtual_lock.async_unload(False)

        # Re-setup reloads from the store via hard_refresh_codes
        await e2e_virtual_lock.async_setup_internal(
            config_entry, _Coordinator(ProviderPort(e2e_virtual_lock))
        )
        codes = await e2e_virtual_lock.async_get_usercodes()

        assert codes[1] == SlotCredential.known("1111")
        assert codes[2] == SlotCredential.known("2222")

    async def test_hard_refresh_reloads_from_store(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Hard refresh reloads from the store, so unsaved codes are lost."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="user1",
            source="direct",
        )

        # Hard refresh reloads from the store (which has not been saved to)
        codes = await e2e_virtual_lock.async_hard_refresh_codes()

        assert codes[1] is SlotCredential.empty()

    async def test_get_users_returns_correct_structure(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """
        async_get_users returns User objects wrapping the slot credentials.

        The entry's own sync has already put its PINs on both configured
        slots by the time setup settles, so slot 2 holds its configured PIN
        and an inactive user is shown by clearing slot 2 on the lock.
        """
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("5555")),
            "5555",
            name="slot_user",
            source="direct",
        )
        await e2e_virtual_lock.async_delete_credential(
            CredentialRef(user_id=2, type=CredentialType.PIN, slot=2)
        )

        users = await e2e_virtual_lock.async_get_users()
        user_map = {u.user_id: u for u in users}

        assert 1 in user_map
        assert user_map[1].active is True
        assert user_map[1].pin_credentials[0].state == SlotCredential.known("5555")
        assert 2 in user_map
        assert user_map[2].active is False


class TestUnloadReleasesBaseListeners:
    """Unloading Lock Code Manager must leave nothing armed on the lock's entry."""

    async def test_provider_reload_after_unload_does_not_rearm(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        virtual_provider_entry: MockConfigEntry,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A provider reload after a Lock Code Manager unload must not re-run setup.

        The base config-entry-state listener watches the lock's own config
        entry, and only the base teardown releases it. A provider that
        unloads without chaining ``super()`` leaves it armed, so the lock's
        entry reaching LOADED re-runs provider setup against the unloaded
        Lock Code Manager entry -- and anything reached from there finds no
        runtime data on it.

        The spy, not the log, is the detector: what a re-setup goes on to
        break is provider-specific, so the assertion has to be that it does
        not happen at all.
        """
        lock = get_virtual_lock(hass, lcm_config_entry)

        assert await hass.config_entries.async_unload(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        caplog.clear()
        with patch.object(lock, "async_setup", wraps=lock.async_setup) as resetup:
            virtual_provider_entry.mock_state(hass, ConfigEntryState.LOADED)
            await hass.async_block_till_done()

        assert resetup.call_count == 0
        # The re-setup runs in a task, so a crash inside it never reaches the
        # caller; the log would be the only place it surfaced.
        assert not [
            record for record in caplog.records if record.levelno >= logging.ERROR
        ]


class TestStoredPaddingNeverReachesTheLock:
    """A PIN stored with padding must not be programmed onto a device padded."""

    async def test_a_padded_stored_pin_is_written_stripped(
        self,
        hass: HomeAssistant,
        virtual_lock_entity,
    ) -> None:
        """
        The sync writes the stripped PIN, not what storage happened to hold.

        Written straight into the entry config on purpose: this is data that
        predates the write-path strip, so no path that would normalize it can
        be used to set it up. The device store is the assertion because that
        is the last place the padding could still be observed -- a caller who
        types the PIN on the keypad has no way to reproduce the padding.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [VIRTUAL_LOCK_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: "padded", CONF_PIN: " 4321 ", CONF_ENABLED: True}
                },
            },
            unique_id="test_virtual_padded_storage",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # The first tick loads initial state; the write happens on the next.
        for tick in range(2):
            async_fire_time_changed(
                hass, dt_util.utcnow() + TICK_INTERVAL * (tick + 1) * 2
            )
            await hass.async_block_till_done()

        lock = get_virtual_lock(hass, entry)
        assert lock._data["1"]["code"] == "4321"

        await hass.config_entries.async_unload(entry.entry_id)


class TestCredentialUseRecording:
    """A virtual lock is the surface for uses Lock Code Manager cannot watch."""

    async def test_event_entity_records_against_the_virtual_lock(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
    ) -> None:
        """
        A virtual-only entry records a use without the lock claiming anything.

        The virtual lock observes nothing and says so
        (``supports_code_slot_events`` is False). Recording a use against it
        anyway is what a virtual lock is for: it is the surface somebody adds
        for uses Lock Code Manager cannot watch.
        """
        state = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert state
        assert state.state != STATE_UNAVAILABLE
        lock = get_virtual_lock(hass, lcm_config_entry)
        # The first sync's write probed the capabilities: the virtual lock
        # advertises every credential type, on top of the types it takes.
        assert lock.cached_capabilities is not None
        assert state.attributes[ATTR_EVENT_TYPES] == sorted(
            {CredentialType.PIN}
            | lock.managed_credential_types
            | set(lock.cached_capabilities.credential_types)
        )
        assert lock.supports_code_slot_events is False

        lock.async_fire_code_slot_event(1, False)
        await hass.async_block_till_done()

        recorded = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert recorded.state != state.state
        assert recorded.attributes[ATTR_EVENT_TYPE] == CredentialType.PIN
        assert recorded.attributes[ATTR_TARGET] == VIRTUAL_LOCK_ENTITY_ID

    async def test_a_user_added_later_learns_what_the_lock_reports(
        self,
        hass: HomeAssistant,
        virtual_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        A use recorder made for a lock already held follows that lock's probe.

        The virtual lock is asked what it can hold only by its first write,
        which comes after the new user's entities are added. The kinds the
        recorder publishes widen then, not at the next recorded use.
        """
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [VIRTUAL_LOCK_ENTITY_ID], CONF_SLOTS: {}},
            unique_id="test_virtual_added_user",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        lock = get_virtual_lock(hass, entry)
        assert lock.cached_capabilities is None

        [subentry] = user_subentries(
            {1: {CONF_NAME: "late", CONF_PIN: "1234", CONF_ENABLED: True}}
        )
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(**subentry))
        await hass.async_block_till_done()
        async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL)
        await hass.async_block_till_done()

        assert lock.cached_capabilities is not None
        event_entity = er.async_get(hass).async_get_entity_id(
            "event",
            DOMAIN,
            build_slot_unique_id(entry.entry_id, 1, EVENT_CREDENTIAL_USED),
        )
        assert hass.states.get(event_entity).attributes[ATTR_EVENT_TYPES] == sorted(
            {CredentialType.PIN}
            | lock.managed_credential_types
            | set(lock.cached_capabilities.credential_types)
        )
        assert await hass.config_entries.async_unload(entry.entry_id)

    @pytest.mark.parametrize("pin", ["1", "9" * 64])
    async def test_advertised_capabilities_reject_no_pin_length(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        pin: str,
    ) -> None:
        """
        Advertising capabilities must not start rejecting lengths.

        Virtual entries had no advertised bounds at all, so any PIN a user
        already stored has to keep saving. The write below is what populates
        the capabilities cache the length gate reads -- without it the gate
        skips every lock and this proves nothing, so the cache is asserted
        before the lengths are tried.
        """
        lock = get_virtual_lock(hass, lcm_config_entry)
        await lock.async_set_usercode(1, "1234")
        assert lock.cached_capabilities is not None

        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: pin},
            target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
            blocking=True,
        )

        state = hass.states.get(SLOT_1_PIN_ENTITY)
        assert state
        assert state.state == pin

    async def test_a_password_is_a_kind_of_use_before_capabilities_are_read(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A type the lock takes is one a use can be reported as, probed or not."""
        lock = get_virtual_lock(hass, lcm_config_entry)

        # Unprobed, so only the types the lock takes can name a password;
        # shown at once by the change signal the worker sends after a job.
        lock._capabilities_cache = None
        async_dispatcher_send(hass, signal_lock_changed(VIRTUAL_LOCK_ENTITY_ID))

        assert hass.states.get(SLOT_1_EVENT_ENTITY).attributes[ATTR_EVENT_TYPES] == [
            CredentialType.PASSWORD,
            CredentialType.PIN,
        ]


class TestReauthLockSwap:
    """A lock swapped out during reauth must lose its credential store."""

    async def test_reauth_swap_removes_the_dropped_locks_store(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        virtual_provider_entry,
    ) -> None:
        """
        Swapping a lock out via reauth takes its stored credentials with it.

        For a Virtual lock the store IS the credentials, so a store that
        outlives every entry referencing it is a cleartext PIN left on disk.
        Reauth writes the entry and reloads it wholesale, which is a different
        path from the options flow's per-lock removal -- this asserts the two
        agree about what leaving an entry means.
        """
        ent_reg = er.async_get(hass)
        replacement = ent_reg.async_get_or_create(
            "lock",
            "virtual",
            "test_virtual_replacement",
            config_entry=virtual_provider_entry,
        )
        hass.states.async_set(replacement.entity_id, "locked")

        lock = get_virtual_lock(hass, lcm_config_entry)
        await lock.async_set_usercode(1, "1234")

        def _fresh_store() -> Store:
            return Store(hass, 1, f"{lock.domain}_{DOMAIN}_{VIRTUAL_LOCK_ENTITY_ID}")

        lcm_config_entry.async_start_reauth(
            hass, context={"lock_entity_id": VIRTUAL_LOCK_ENTITY_ID}
        )
        await hass.async_block_till_done()
        [flow] = lcm_config_entry.async_get_active_flows(hass, {SOURCE_REAUTH})

        result = await async_configure_flow(
            hass, flow["flow_id"], {CONF_LOCKS: [replacement.entity_id]}
        )
        assert result["type"] == "abort"
        assert result["reason"] == "locks_updated"
        await hass.async_block_till_done()

        assert await _fresh_store().async_load() is None


def _entry_on_slot_3(hass: HomeAssistant, standing: str) -> MockConfigEntry:
    """
    Add a second entry on the virtual lock that holds slot 3 but syncs nothing.

    ``standing`` is how it stopped syncing: switched off by the user, or a
    setup that failed. Either way its user still holds the number.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [VIRTUAL_LOCK_ENTITY_ID]},
        subentries_data=user_subentries(
            {3: {CONF_NAME: "slot3", CONF_PIN: "5555", CONF_ENABLED: True}}
        ),
        unique_id=f"test_virtual_e2e_{standing}",
        disabled_by=ConfigEntryDisabler.USER if standing == "disabled" else None,
    )
    entry.add_to_hass(hass)
    if standing == "setup_failed":
        entry.mock_state(hass, ConfigEntryState.SETUP_ERROR)
    return entry


@pytest.mark.parametrize("standing", ["disabled", "setup_failed"])
class TestASlotHeldByAnEntryThatSyncsNothing:
    """
    A slot whose entry syncs nothing is still that entry's.

    Not wanted on the lock, since nobody syncs it, but whatever sits there
    belongs to that entry's user rather than to nobody.
    """

    async def test_a_use_is_credited_to_its_user(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        standing: str,
    ) -> None:
        """The code is that user's, so its use is announced as theirs."""
        holder = _entry_on_slot_3(hass, standing)
        events: list[Event] = []
        hass.bus.async_listen(BUS_EVENT_CREDENTIAL_USED, events.append)

        get_virtual_lock(hass, lcm_config_entry).async_fire_code_slot_event(3, False)
        await hass.async_block_till_done()

        assert [
            (event.data[ATTR_NAME], event.data[ATTR_CONFIG_ENTRY_ID])
            for event in events
        ] == [("slot3", holder.entry_id)]


class TestLockLeaving:
    """A lock leaves Lock Code Manager only when no entry lists it."""

    async def test_a_disabled_entrys_lock_keeps_its_codes_when_another_drops_it(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
        virtual_provider_entry,
    ) -> None:
        """The codes the virtual lock holds survive for the disabled entry that lists it."""
        _entry_on_slot_3(hass, "disabled")
        lock = get_virtual_lock(hass, lcm_config_entry)
        await lock.async_set_usercode(3, "5555")
        replacement = er.async_get(hass).async_get_or_create(
            "lock",
            "virtual",
            "test_virtual_replacement",
            config_entry=virtual_provider_entry,
        )
        hass.states.async_set(replacement.entity_id, "locked")

        lcm_config_entry.async_start_reauth(
            hass, context={"lock_entity_id": VIRTUAL_LOCK_ENTITY_ID}
        )
        await hass.async_block_till_done()
        [flow] = lcm_config_entry.async_get_active_flows(hass, {SOURCE_REAUTH})
        result = await async_configure_flow(
            hass, flow["flow_id"], {CONF_LOCKS: [replacement.entity_id]}
        )
        assert result["reason"] == "locks_updated"
        await hass.async_block_till_done()

        stored = await Store(
            hass, 1, f"{lock.domain}_{DOMAIN}_{VIRTUAL_LOCK_ENTITY_ID}"
        ).async_load()
        assert stored is not None


class TestSlotRepairs:
    """A slot's repairs are decided over every credential it holds."""

    async def test_a_held_pin_keeps_its_repair_beside_an_in_sync_password(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """The repair is the slot's: a credential in sync does not answer another's hold."""
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None
        # Set by hand and reconciled at once: nothing awaits in between, so the
        # worker cannot sync them first.
        worker.slots[pin_address(1)] = Slot(
            "1234", seen=Holds("0000"), hold=Repair.CANT_SET
        )
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(
            None, seen=Seen.EMPTY
        )

        async_reconcile_issues(hass, VIRTUAL_LOCK_ENTITY_ID, worker)

        issue_id = (
            f"slot_suspended_{lcm_config_entry.entry_id}_{VIRTUAL_LOCK_ENTITY_ID}_1"
        )
        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    async def test_a_repair_from_a_previous_run_waits_for_every_credential(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A persisted repair stays while one of the slot's credentials is not in sync."""
        issue_id = (
            f"slot_suspended_{lcm_config_entry.entry_id}_{VIRTUAL_LOCK_ENTITY_ID}_1"
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="slot_suspended",
            translation_placeholders={
                "lock_entity_id": VIRTUAL_LOCK_ENTITY_ID,
                "lock_name": VIRTUAL_LOCK_ENTITY_ID,
                "slot_num": "1",
                "reason": "x",
            },
            data={"lock_entity_id": VIRTUAL_LOCK_ENTITY_ID, "slot": 1},
        )
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None
        # Set by hand and reconciled at once: nothing awaits in between, so the
        # worker cannot sync them first. The PIN was resumed and is not on the
        # lock yet; the password is.
        worker.slots[pin_address(1)] = Slot("1234", seen=Holds("0000"))
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(
            None, seen=Seen.EMPTY
        )

        async_reconcile_issues(hass, VIRTUAL_LOCK_ENTITY_ID, worker)

        assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    async def test_a_clear_that_would_not_take_is_the_slots_repair(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A code that may still open the door outranks one that could not be set."""
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None
        # Set by hand and reconciled at once: nothing awaits in between, so the
        # worker cannot sync them first.
        worker.slots[pin_address(1)] = Slot(
            None, seen=Holds("0000"), hold=Repair.CODE_MAY_REMAIN
        )
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(
            "secret", seen=Seen.EMPTY, hold=Repair.CANT_SET
        )

        async_reconcile_issues(hass, VIRTUAL_LOCK_ENTITY_ID, worker)

        issue = ir.async_get(hass).async_get_issue(
            DOMAIN,
            f"slot_suspended_{lcm_config_entry.entry_id}_{VIRTUAL_LOCK_ENTITY_ID}_1",
        )
        assert issue is not None
        assert issue.translation_key == "slot_clear_failed"


class TestSlotInSync:
    """A slot's in-sync state on a lock covers every credential it holds."""

    async def test_the_aggregate_in_sync_covers_every_credential_in_the_slot(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A password out of sync takes the slot out of sync; the code sensor stays the PIN."""
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 1, VIRTUAL_LOCK_ENTITY_ID)
        code = code_entity_id(hass, lcm_config_entry, 1, VIRTUAL_LOCK_ENTITY_ID)
        assert hass.states.get(in_sync).state == STATE_ON
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None

        # Wanted and not on the lock, set by hand; the change signal is what the
        # worker sends after a job.
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(
            "pw", seen=Seen.EMPTY
        )
        async_dispatcher_send(hass, signal_lock_changed(VIRTUAL_LOCK_ENTITY_ID))
        await hass.async_block_till_done()

        assert hass.states.get(in_sync).state == STATE_OFF
        assert hass.states.get(code).state == "1234"

    async def test_a_hidden_pin_in_sync_shows_beside_a_password_out_of_sync(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """The code sensor trusts the PIN's own sync, not the slot's."""
        code = code_entity_id(hass, lcm_config_entry, 1, VIRTUAL_LOCK_ENTITY_ID)
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None

        # A PIN the lock will not show, written and so trusted, beside a
        # password wanted and not on the lock; set by hand, then the change
        # signal the worker sends after a job.
        pin = Slot("1234", seen=Seen.MASKED)
        pin.wrote_want(worker.salt)
        worker.slots[pin_address(1)] = pin
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(
            "pw", seen=Seen.EMPTY
        )
        async_dispatcher_send(hass, signal_lock_changed(VIRTUAL_LOCK_ENTITY_ID))
        await hass.async_block_till_done()

        assert hass.states.get(code).state == "1234"

    async def test_a_held_password_outranks_an_out_of_sync_pin(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """The aggregate shows the credential furthest from in sync."""
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 1, VIRTUAL_LOCK_ENTITY_ID)
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None

        # Set by hand and shown at once: nothing awaits in between, so the
        # worker cannot sync them first.
        worker.slots[pin_address(1)] = Slot("1234", seen=Holds("0000"))
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(
            "pw", seen=Seen.EMPTY, hold=Repair.CANT_SET
        )
        async_dispatcher_send(hass, signal_lock_changed(VIRTUAL_LOCK_ENTITY_ID))

        state = hass.states.get(in_sync)
        assert state.state == STATE_OFF
        assert state.attributes[ATTR_SYNC_STATUS] == "suspended"

    async def test_a_slot_with_a_credential_never_read_is_unavailable(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """Before the lock first answers, every credential in the slot must be read."""
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 1, VIRTUAL_LOCK_ENTITY_ID)
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None

        # The PIN is read and the password never was; set by hand and shown
        # at once, so the worker cannot read the password first.
        worker.slots[pin_address(1)] = Slot("1234", seen=Holds("1234"))
        worker.slots[CredentialAddress(1, CredentialType.PASSWORD)] = Slot(None)
        with patch.object(
            LockWorker, "answered", new_callable=PropertyMock, return_value=False
        ):
            async_dispatcher_send(hass, signal_lock_changed(VIRTUAL_LOCK_ENTITY_ID))

            assert hass.states.get(in_sync).state == STATE_UNAVAILABLE

    async def test_a_sync_queued_for_the_password_shows_the_slot_syncing(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A sync of any credential in the slot is the slot syncing."""
        in_sync = in_sync_entity_id(hass, lcm_config_entry, 1, VIRTUAL_LOCK_ENTITY_ID)
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None
        password = CredentialAddress(1, CredentialType.PASSWORD)

        # Queued and shown at once: nothing awaits in between, so the worker
        # cannot run the sync first.
        worker.slots[password] = Slot(None, seen=Seen.EMPTY)
        worker.request_sync([password])
        assert worker.syncing(password)
        assert not worker.syncing(pin_address(1))
        async_dispatcher_send(hass, signal_lock_changed(VIRTUAL_LOCK_ENTITY_ID))

        assert hass.states.get(in_sync).attributes[ATTR_SYNC_STATUS] == "syncing"


class TestPasswordsKeptPerLock:
    """A Virtual lock takes passwords: they occupy, are kept, and get a sensor."""

    async def test_a_password_alone_occupies_a_slot(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """Allocation does not hand out a slot that holds only a password."""
        lock = get_virtual_lock(hass, lcm_config_entry)
        lock._data["7"] = {"name": None, "password": "pw"}

        occupancy = await async_read_occupancy(
            hass, lcm_config_entry, [VIRTUAL_LOCK_ENTITY_ID], [6, 7]
        )

        assert occupancy.locks[0].occupied == frozenset({7})

    async def test_a_lock_that_stops_taking_passwords_lets_its_address_go(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A type the lock no longer takes is dropped at the next want, its value left."""
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None
        password = CredentialAddress(1, CredentialType.PASSWORD)
        assert password in worker.slots

        with patch.object(
            VirtualLock,
            "writable_credential_types",
            new_callable=PropertyMock,
            return_value=frozenset({CredentialType.PIN}),
        ):
            async_want(
                hass,
                lcm_config_entry,
                VIRTUAL_LOCK_ENTITY_ID,
                1,
                {CredentialType.PIN: "1234"},
            )

        assert password not in worker.slots

    async def test_a_release_in_flight_is_kept_when_the_lock_stops_taking_its_type(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A type the lock no longer takes is not let go of while its clear is owed."""
        worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
        assert worker is not None
        password = CredentialAddress(1, CredentialType.PASSWORD)

        # Given up and wanted again with nothing awaited in between, so the
        # release clear has not run when the want arrives.
        async_give_up(hass, lcm_config_entry, VIRTUAL_LOCK_ENTITY_ID, 1)
        assert worker.slots[password].release
        with patch.object(
            VirtualLock,
            "writable_credential_types",
            new_callable=PropertyMock,
            return_value=frozenset({CredentialType.PIN}),
        ):
            async_want(
                hass,
                lcm_config_entry,
                VIRTUAL_LOCK_ENTITY_ID,
                1,
                {CredentialType.PIN: "1234"},
            )

        assert worker.slots[password].release

    async def test_password_in_sync_is_registered_but_disabled_by_default(
        self, hass: HomeAssistant, lcm_config_entry
    ) -> None:
        """A lock that takes passwords gets the per-type sensor, off by default."""
        entity_id = er.async_get(hass).async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            build_slot_unique_id(
                lcm_config_entry.entry_id,
                1,
                credential_in_sync_key(CredentialType.PASSWORD.value),
                VIRTUAL_LOCK_ENTITY_ID,
            ),
        )
        assert entity_id is not None
        entry = er.async_get(hass).async_get(entity_id)
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


class TestOpenBugs:
    """Open virtual-lock bugs, each pinned by a strict xfail."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "open: a lock with no state yet is named by its entity name alone, "
            "dropping the device name Home Assistant puts in front of it"
        ),
    )
    async def test_a_lock_named_after_its_device_is_called_by_its_full_name(
        self,
        hass: HomeAssistant,
        virtual_provider_entry,
    ) -> None:
        """A lock Home Assistant shows as "Front Door Lock" is not called just "Lock"."""
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=virtual_provider_entry.entry_id,
            identifiers={("virtual", "front_door")},
            name="Front Door",
        )
        lock_entity = er.async_get(hass).async_get_or_create(
            "lock",
            "virtual",
            "front_door_lock",
            config_entry=virtual_provider_entry,
            device_id=device.id,
            has_entity_name=True,
            original_name="Lock",
        )
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [lock_entity.entity_id]},
            subentries_data=user_subentries(
                {1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True}}
            ),
            unique_id="test_virtual_named",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        names = [
            state.name
            for state in hass.states.async_all("binary_sensor")
            if state.entity_id.endswith("_in_sync")
        ]
        await hass.config_entries.async_unload(entry.entry_id)
        assert names
        assert all("Front Door Lock" in name for name in names), names
