"""Full lifecycle E2E tests for Schlage lock provider."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lock import LockState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    PENDING_WRITE_TTL,
    SERVICE_HARD_REFRESH_USERCODES,
)
from custom_components.lock_code_manager.ha.runtime import lock_registry
from custom_components.lock_code_manager.model.credentials import (
    CredentialRef,
    CredentialType,
    SlotCredential,
    WriteResult,
    credential_from_slot,
    pin_address,
)
from custom_components.lock_code_manager.providers._util import (
    make_tagged_name,
    parse_tag,
)
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
)
from custom_components.lock_code_manager.sync.slot import Seen
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_set_slot_pin,
    in_sync_entity_id,
    lcm_issue_ids,
)
from tests.providers.helpers import register_mock_service
from tests.providers.schlage.conftest import SCHLAGE_LCM_CONFIG_SLOTS
from tests.providers.schlage.fake_service import FakeSchlageCloud


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Schlage provider."""

    async def test_provider_discovered_as_schlage(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Schlage lock and creates a SchlageLock."""
        lock = lcm_config_entry.runtime_data.locks.get(schlage_lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, SchlageLock)
        assert lock.domain == SCHLAGE_DOMAIN
        assert lock.supports_code_slot_events is False
        assert lock.usercode_scan_interval == timedelta(minutes=5)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_schlage_lock.coordinator is not None


@pytest.fixture
def no_read_lag() -> Iterator[None]:
    """Read straight after setup's writes: for tests of the read, not the lag."""
    with patch.object(SchlageLock, "read_lag", 0.0):
        yield


class TestSetAndClearCredentials:
    """Verify set/clear operations call the correct Schlage services."""

    async def test_set_credential(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a credential via the provider and verify the add_code service was called."""
        schlage_mock_services["add_code"].reset_mock()
        result = await e2e_schlage_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("9999")),
            "9999",
            name="Test User",
            source="direct",
        )

        assert result is WriteResult.CONFIRMED
        assert schlage_mock_services["add_code"].call_count >= 1
        add_call = schlage_mock_services["add_code"].call_args[0][0]
        assert add_call.data["name"] == "lcm:1:Test User"
        assert add_call.data["code"] == "9999"

    async def test_delete_credential(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Clear a credential via the provider and verify the delete_code service was called."""
        entity_id = schlage_lock_entity.entity_id

        # Set up a code to delete
        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "lcm:1:Guest", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )
        schlage_mock_services["delete_code"].reset_mock()

        result = await e2e_schlage_lock.async_delete_credential(
            CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        )

        assert result is True
        assert schlage_mock_services["delete_code"].call_count >= 1

    async def test_base_orchestration_set_credential(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """The worker's write, async_set_usercode, routes through base → async_set_credential."""
        schlage_mock_services["add_code"].reset_mock()
        await e2e_schlage_lock.async_set_usercode(1, "9999", "Test User", source="sync")

        assert schlage_mock_services["add_code"].call_count >= 1
        add_call = schlage_mock_services["add_code"].call_args[0][0]
        assert add_call.data["name"] == "lcm:1:Test User"
        assert add_call.data["code"] == "9999"

    async def test_coordinator_reflects_set_credential(
        self,
        no_read_lag: None,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        After a worker read, its readings reflect the lock state.

        Schlage is not a push provider, so the worker reads state from the
        lock via get_codes. When the mock reports a tagged code on slot 1,
        the worker sees it as a masked code.
        """
        entity_id = schlage_lock_entity.entity_id

        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "lcm:1:Test User", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )

        worker = lock_registry(hass).worker(entity_id)
        assert await worker.read([pin_address(1), pin_address(2)]) == {
            pin_address(1): Seen.MASKED,
            pin_address(2): Seen.EMPTY,
        }
        assert worker.slots[pin_address(1)].seen is Seen.MASKED
        assert worker.slots[pin_address(2)].seen is Seen.EMPTY

    async def test_coordinator_reflects_clear_credential(
        self,
        no_read_lag: None,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """After a worker read with empty codes, all slots are EMPTY."""
        entity_id = schlage_lock_entity.entity_id

        # Ensure get_codes returns empty
        schlage_mock_services["get_codes"] = AsyncMock(return_value={entity_id: {}})
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )

        worker = lock_registry(hass).worker(entity_id)
        assert await worker.read([pin_address(1), pin_address(2)]) == {
            pin_address(1): Seen.EMPTY,
            pin_address(2): Seen.EMPTY,
        }
        assert worker.slots[pin_address(1)].seen is Seen.EMPTY
        assert worker.slots[pin_address(2)].seen is Seen.EMPTY


class TestGetUsers:
    """Verify reading users (usercodes) from the Schlage lock."""

    async def test_get_users_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        async_get_users returns slot occupancy from Schlage.

        After initial setup with empty codes, both managed slots should
        be EMPTY. When the mock reports a tagged code on slot 1, that
        slot should become UNREADABLE_CODE.
        """
        entity_id = schlage_lock_entity.entity_id

        # Override the get_codes mock to report an occupied slot
        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "lcm:1:Guest", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass,
            SCHLAGE_DOMAIN,
            "get_codes",
            schlage_mock_services["get_codes"],
        )

        users = await e2e_schlage_lock.async_get_users()
        user_map = {u.user_id: u for u in users}

        assert user_map[1].pin_credentials[0].state is SlotCredential.unreadable()
        assert user_map[2].pin_credentials[0].state is SlotCredential.empty()

    async def test_get_usercodes_projection(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        The base async_get_usercodes projection produces the same slot->SlotCredential dict.
        """
        entity_id = schlage_lock_entity.entity_id

        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "lcm:1:Guest", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass,
            SCHLAGE_DOMAIN,
            "get_codes",
            schlage_mock_services["get_codes"],
        )

        codes = await e2e_schlage_lock.async_get_usercodes()

        assert codes[1] is SlotCredential.unreadable()
        assert codes[2] is SlotCredential.empty()


@asynccontextmanager
async def _schlage_entry(
    hass: HomeAssistant, lock_entity: er.RegistryEntry
) -> AsyncIterator[MockConfigEntry]:
    """
    Set up an entry on the Schlage lock, slots 1 and 2; unload it after.

    The Schlage entry is a mock that never loads, so the integration is
    reported connected for as long as the entry is in use, as the
    ``lcm_config_entry`` fixture does.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock_entity.entity_id],
            CONF_SLOTS: SCHLAGE_LCM_CONFIG_SLOTS,
        },
        unique_id="test_schlage_batch6",
    )
    entry.add_to_hass(hass)
    with patch.object(SchlageLock, "async_is_integration_connected", return_value=True):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        try:
            yield entry
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


# How long the cloud reads back the table from before a write: long enough
# that several sync ticks see the codes missing, inside PENDING_WRITE_TTL so a
# write the cloud does keep is never given up.
_CLOUD_LAG = PENDING_WRITE_TTL / 2


class TestCloud:
    """The Schlage cloud's lag and timeouts (#1083)."""

    async def test_codes_the_cloud_reads_back_late_are_written_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1083 (PRs #1089, #1090): a lagging read does not churn codes.

        Read back before the cloud caught up, each code looked missing and
        was deleted and added again every cycle. The ticks below all run
        while the cloud still reads back the table from before the writes.
        """
        fake_schlage_cloud.read_lag = _CLOUD_LAG
        async with _schlage_entry(hass, schlage_lock_entity) as entry:
            await async_advance_sync_ticks(hass, freezer, 10)
            if not fake_schlage_cloud.lagging:
                pytest.fail("setup: the cloud caught up before the ticks ran out")

            assert fake_schlage_cloud.count("add_code") == len(SCHLAGE_LCM_CONFIG_SLOTS)
            # Once caught up, the reads show every code where it belongs.
            await async_advance_clock(hass, freezer, 6 * 60)
            await async_advance_sync_ticks(hass, freezer, 5)
            assert fake_schlage_cloud.count("add_code") == len(SCHLAGE_LCM_CONFIG_SLOTS)
            pins = sorted(code for _name, code in fake_schlage_cloud.codes.values())
            assert pins == sorted(
                c[CONF_PIN] for c in SCHLAGE_LCM_CONFIG_SLOTS.values()
            )
            for slot in SCHLAGE_LCM_CONFIG_SLOTS:
                assert (
                    hass.states.get(
                        in_sync_entity_id(
                            hass, entry, slot, schlage_lock_entity.entity_id
                        )
                    ).state
                    == STATE_ON
                )

    async def test_a_new_pin_racing_the_cloud_s_delete_is_not_a_failure(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1083 (PR #1090): "already exists" after a delete is not refused.

        A new PIN is written as a delete of the slot's code, then an add. The
        cloud still reads the deleted code back, so the add is refused for a
        name that "already exists"; counted as a failed write, it suspended the
        slot while the cloud caught up.
        """
        async with _schlage_entry(hass, schlage_lock_entity) as entry:
            await async_advance_sync_ticks(hass, freezer, 5)
            fake_schlage_cloud.read_lag = _CLOUD_LAG

            await async_set_slot_pin(hass, entry, 1, "4321")
            await async_advance_sync_ticks(hass, freezer, 30)
            # The refused add left the slot empty; the next poll sees that.
            await async_advance_clock(hass, freezer, 6 * 60)
            await async_advance_sync_ticks(hass, freezer, 5)

            assert lcm_issue_ids(hass, "slot_suspended") == []
            assert "4321" in (code for _n, code in fake_schlage_cloud.codes.values())

    async def test_a_cloud_timeout_on_a_write_is_retried_not_suspended(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Regression for #1083 (PR #1089): a cloud read timeout is a transient failure.

        The raw ``ReadTimeout`` the Schlage service lets through suspended
        the slot's sync.
        """
        fake_schlage_cloud.time_out("add_code", times=1)
        async with _schlage_entry(hass, schlage_lock_entity):
            await async_advance_sync_ticks(hass, freezer, 10)

            assert lcm_issue_ids(hass, "slot_suspended") == []
            assert sorted(
                code for _n, code in fake_schlage_cloud.codes.values()
            ) == sorted(c[CONF_PIN] for c in SCHLAGE_LCM_CONFIG_SLOTS.values())


class TestAvailability:
    """Whether the lock is reachable comes from its entity, not a read of its codes."""

    async def test_an_idle_lock_s_codes_are_read_only_by_the_poll(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """
        Five idle minutes read the codes once, for the poll.

        The connection check read every code from the cloud every 30 seconds,
        outside the worker's pacing: about 2,880 reads a lock a day.
        """
        async with _schlage_entry(hass, schlage_lock_entity):
            await async_advance_sync_ticks(hass, freezer, 5)
            reads = fake_schlage_cloud.count("get_codes")

            await async_advance_clock(hass, freezer, 5 * 60)

            assert fake_schlage_cloud.count("get_codes") - reads <= 1

    async def test_a_lock_whose_entity_goes_unavailable_is_unreachable(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """Unreachable while the entity is unavailable, reachable once it returns."""
        entity_id = schlage_lock_entity.entity_id
        async with _schlage_entry(hass, schlage_lock_entity) as entry:
            lock = entry.runtime_data.locks[entity_id]
            await async_advance_sync_ticks(hass, freezer, 5)
            assert await lock.async_internal_is_reachable()

            hass.states.async_set(entity_id, STATE_UNAVAILABLE)
            assert not await lock.async_internal_is_reachable()

            hass.states.async_set(entity_id, LockState.LOCKED)
            assert await lock.async_internal_is_reachable()


class TestRefusedPin:
    """The lock's own refusal of a PIN holds the slot instead of retrying it."""

    @pytest.mark.parametrize(
        "other_name",
        [
            pytest.param(make_tagged_name(9, "Neighbour"), id="tagged_unmanaged"),
            pytest.param("Neighbour", id="untagged"),
        ],
    )
    async def test_a_pin_another_code_holds_is_refused_not_retried(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
        other_name: str,
    ) -> None:
        """
        Schlage refusing slot 1's PIN, held by another code, disables slot 1 at once.

        Taken as no answer, the add was sent again on every retry until the
        slot's breaker tripped, with no repair saying the PIN was refused.
        """
        # A code Lock Code Manager does not manage holds slot 1's PIN.
        other = fake_schlage_cloud.add(
            other_name, SCHLAGE_LCM_CONFIG_SLOTS[1][CONF_PIN]
        )
        async with _schlage_entry(hass, schlage_lock_entity):
            await async_advance_sync_ticks(hass, freezer, 30)

            slot_1_adds = [
                data
                for service, data in fake_schlage_cloud.calls
                if service == "add_code" and parse_tag(data["name"])[0] == 1
            ]
            assert len(slot_1_adds) == 1
            assert lcm_issue_ids(hass, "slot_disabled")
            assert sorted(
                code for _n, code in fake_schlage_cloud.codes.values()
            ) == sorted(c[CONF_PIN] for c in SCHLAGE_LCM_CONFIG_SLOTS.values())
            assert fake_schlage_cloud.codes[other] == (other_name, "1234")
            assert _deletes_of(fake_schlage_cloud, other_name) == []


def _deletes_of(cloud: FakeSchlageCloud, name: str) -> list[dict]:
    """Return the ``delete_code`` calls naming ``name``, compared as core does."""
    return [
        data
        for service, data in cloud.calls
        if service == "delete_code" and data["name"].lower().strip() == name.lower()
    ]


class TestForeignCodes:
    """A code without Lock Code Manager's tag is never read, renamed or deleted."""

    @pytest.mark.parametrize("foreign_name", ["Mom", "1", "lcm2"])
    async def test_a_foreign_code_survives_setup_and_hard_refresh(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
        foreign_name: str,
    ) -> None:
        """
        A homeowner's code keeps its name and PIN, and is no slot.

        Setup and every hard refresh renamed untagged codes into free slots
        (a tagged copy added, the original deleted), taking over codes Lock
        Code Manager never made. A bare-digit or compact name, which only
        Matter writes as a tag, was read as that slot's code.
        """
        entity_id = schlage_lock_entity.entity_id
        mom = fake_schlage_cloud.add(foreign_name, "9999")
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [entity_id],
                # Slot 3 is off, so a slot stays free for a hard refresh too.
                CONF_SLOTS: SCHLAGE_LCM_CONFIG_SLOTS
                | {3: {CONF_NAME: "slot3", CONF_PIN: "4321", CONF_ENABLED: False}},
            },
            unique_id="test_schlage_foreign",
        )
        entry.add_to_hass(hass)
        with patch.object(
            SchlageLock, "async_is_integration_connected", return_value=True
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await async_advance_sync_ticks(hass, freezer, 5)
            await hass.services.async_call(
                DOMAIN,
                SERVICE_HARD_REFRESH_USERCODES,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
            await async_advance_sync_ticks(hass, freezer, 5)
            codes = await entry.runtime_data.locks[entity_id].async_get_usercodes()
            await hass.config_entries.async_unload(entry.entry_id)

        assert fake_schlage_cloud.codes[mom] == (foreign_name, "9999")
        assert _deletes_of(fake_schlage_cloud, foreign_name) == []
        assert not any(
            data["code"] == "9999"
            for service, data in fake_schlage_cloud.calls
            if service == "add_code"
        )
        assert codes == {
            1: SlotCredential.unreadable(),
            2: SlotCredential.unreadable(),
            3: SlotCredential.empty(),
        }


def _slot_pins(cloud: FakeSchlageCloud, slot: int) -> list[str]:
    """Return the PINs of the codes tagged for ``slot`` on the cloud."""
    return [code for name, code in cloud.codes.values() if parse_tag(name)[0] == slot]


def _rename_slot_code(cloud: FakeSchlageCloud, slot: int, name: str | None) -> None:
    """Rename ``slot``'s code on the cloud, as the app would; None leaves it."""
    if name is None:
        return
    ((code_id, (_old, pin)),) = (
        (code_id, entry)
        for code_id, entry in cloud.codes.items()
        if parse_tag(entry[0])[0] == slot
    )
    cloud.codes[code_id] = (name, pin)


# The slot's code as LCM wrote it, or renamed in the app so the new code's
# name differs from the old one's.
_OLD_CODE_NAMES = pytest.mark.parametrize(
    "cloud_name",
    [
        pytest.param(None, id="same_name"),
        pytest.param(make_tagged_name(1, "Mom"), id="renamed"),
    ],
)


class TestChangingAPin:
    """A new PIN replaces the slot's old code, never sits beside or behind it."""

    @_OLD_CODE_NAMES
    async def test_an_old_code_the_cloud_will_not_delete_holds_the_new_pin(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
        cloud_name: str | None,
    ) -> None:
        """
        A cloud failure deleting the old code is no answer: retried, and no add.

        Swallowed, a same-name add was refused as "already exists" and taken
        for success, leaving the old PIN live and the slot shown in sync; a
        renamed slot gained the new code beside the old one.
        """
        async with _schlage_entry(hass, schlage_lock_entity) as entry:
            await async_advance_sync_ticks(hass, freezer, 5)
            _rename_slot_code(fake_schlage_cloud, 1, cloud_name)
            adds = fake_schlage_cloud.count("add_code")

            with fake_schlage_cloud.fail_deletes(times=None) as fault:
                await async_set_slot_pin(hass, entry, 1, "4321")
                await async_advance_sync_ticks(hass, freezer, 5)

            assert _slot_pins(fake_schlage_cloud, 1) == ["1234"]
            assert fake_schlage_cloud.count("add_code") == adds
            in_sync = in_sync_entity_id(hass, entry, 1, schlage_lock_entity.entity_id)
            assert hass.states.get(in_sync).state != STATE_ON
            assert fault.hits > 1

    @_OLD_CODE_NAMES
    async def test_a_failed_delete_is_retried_until_the_old_code_is_gone(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
        cloud_name: str | None,
    ) -> None:
        """A delete the cloud fails, then does, ends with the new PIN alone on the slot."""
        async with _schlage_entry(hass, schlage_lock_entity) as entry:
            await async_advance_sync_ticks(hass, freezer, 5)
            _rename_slot_code(fake_schlage_cloud, 1, cloud_name)
            # Fewer failures than a slot's tries, so the retries see it through.
            fault = fake_schlage_cloud.fail_deletes(times=2)

            await async_set_slot_pin(hass, entry, 1, "4321")
            await async_advance_sync_ticks(hass, freezer, 5)

            assert _slot_pins(fake_schlage_cloud, 1) == ["4321"]
            in_sync = in_sync_entity_id(hass, entry, 1, schlage_lock_entity.entity_id)
            assert hass.states.get(in_sync).state == STATE_ON
            assert fault.hits == 2
            assert lcm_issue_ids(hass, "slot_suspended") == []

    async def test_an_old_code_already_gone_does_not_hold_the_new_pin(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_schlage_cloud: FakeSchlageCloud,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """A slot whose code was deleted on the lock takes its new PIN: nothing to clear."""
        async with _schlage_entry(hass, schlage_lock_entity) as entry:
            await async_advance_sync_ticks(hass, freezer, 5)
            fake_schlage_cloud.codes = {
                code_id: code
                for code_id, code in fake_schlage_cloud.codes.items()
                if parse_tag(code[0])[0] != 1
            }
            # The cloud fails any delete it reaches; with no code to find,
            # core answers before reaching it.
            fake_schlage_cloud.fail_deletes(times=None)

            await async_set_slot_pin(hass, entry, 1, "4321")
            await async_advance_sync_ticks(hass, freezer, 5)

            assert _slot_pins(fake_schlage_cloud, 1) == ["4321"]
            in_sync = in_sync_entity_id(hass, entry, 1, schlage_lock_entity.entity_id)
            assert hass.states.get(in_sync).state == STATE_ON
