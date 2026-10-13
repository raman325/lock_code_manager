"""Full lifecycle E2E tests for Schlage lock provider."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
)
from custom_components.lock_code_manager.domain.credentials import (
    CredentialRef,
    CredentialType,
    WriteResult,
    credential_from_slot,
    pin_address,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._util import (
    make_tagged_name,
    parse_tag,
)
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
)
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_set_slot_pin,
    in_sync_entity_id,
    lcm_issue_ids,
    user_subentries,
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

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_schlage_lock.coordinator is not None


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
        """Set via async_internal_set_usercode routes through base → async_set_credential."""
        schlage_mock_services["add_code"].reset_mock()
        await e2e_schlage_lock.async_internal_set_usercode(1, "9999", "Test User")

        # internal returns None (fires coordinator refresh) — check service call
        assert schlage_mock_services["add_code"].call_count >= 1
        add_call = schlage_mock_services["add_code"].call_args[0][0]
        assert add_call.data["name"] == "lcm:1:Test User"
        assert add_call.data["code"] == "9999"

    async def test_coordinator_reflects_set_credential(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        After a coordinator refresh, data reflects the lock state.

        Schlage is not a push provider, so the coordinator reads state
        from the lock via get_codes. When the mock reports a tagged code
        on slot 1, the coordinator data should show UNREADABLE_CODE.
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

        await e2e_schlage_lock.coordinator.async_refresh()

        assert (
            e2e_schlage_lock.coordinator.data.get(pin_address(1))
            is SlotCredential.unreadable()
        )
        assert (
            e2e_schlage_lock.coordinator.data.get(pin_address(2))
            is SlotCredential.empty()
        )

    async def test_coordinator_reflects_clear_credential(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """After a coordinator refresh with empty codes, all slots are EMPTY."""
        entity_id = schlage_lock_entity.entity_id

        # Ensure get_codes returns empty
        schlage_mock_services["get_codes"] = AsyncMock(return_value={entity_id: {}})
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )

        await e2e_schlage_lock.coordinator.async_refresh()

        assert (
            e2e_schlage_lock.coordinator.data.get(pin_address(1))
            is SlotCredential.empty()
        )
        assert (
            e2e_schlage_lock.coordinator.data.get(pin_address(2))
            is SlotCredential.empty()
        )


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


class TestOpenBugs:
    """Open Schlage bugs, each pinned by a strict xfail."""

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "open: the tag pass hands an unmanaged code to any slot an entry "
            "lists, a disabled entry's included"
        ),
    )
    async def test_a_homeowners_code_is_not_tagged_into_a_disabled_entrys_slot(
        self,
        hass: HomeAssistant,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Slot 3 belongs to a disabled entry's user, so it is not free to adopt a code."""
        entity_id = schlage_lock_entity.entity_id
        schlage_mock_services["get_codes"].return_value = {
            entity_id: {
                "a": {"name": make_tagged_name(1, "slot1"), "code": "1234"},
                "b": {"name": make_tagged_name(2, "slot2"), "code": "5678"},
                "c": {"name": "Mom", "code": "9999"},
            }
        }
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [entity_id]},
            subentries_data=user_subentries(
                {3: {CONF_NAME: "slot3", CONF_PIN: "5555", CONF_ENABLED: True}}
            ),
            unique_id="test_schlage_disabled",
            disabled_by=ConfigEntryDisabler.USER,
        ).add_to_hass(hass)
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [entity_id], CONF_SLOTS: SCHLAGE_LCM_CONFIG_SLOTS},
            unique_id="test_schlage_e2e",
        )
        entry.add_to_hass(hass)

        with patch.object(
            SchlageLock, "async_is_integration_connected", return_value=True
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            tagged = [
                parse_tag(call.args[0].data["name"])[0]
                for call in schlage_mock_services["add_code"].await_args_list
            ]
            await hass.config_entries.async_unload(entry.entry_id)

        assert 3 not in tagged


@contextmanager
def _schlage_entry(
    hass: HomeAssistant, lock_entity: er.RegistryEntry
) -> Iterator[MockConfigEntry]:
    """
    Add an entry on the Schlage lock, slots 1 and 2.

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
        yield entry


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
        was deleted and added again every cycle.
        """
        fake_schlage_cloud.read_lag = 2
        with _schlage_entry(hass, schlage_lock_entity) as entry:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await async_advance_sync_ticks(hass, freezer, 10)
            # A write is a delete of the name, then an add (one each per slot).
            writes = len(fake_schlage_cloud.calls) - fake_schlage_cloud.count(
                "get_codes"
            )
            await async_advance_sync_ticks(hass, freezer, 10)

            pins = sorted(code for _name, code in fake_schlage_cloud.codes.values())
            assert pins == sorted(
                c[CONF_PIN] for c in SCHLAGE_LCM_CONFIG_SLOTS.values()
            )
            assert fake_schlage_cloud.count("add_code") == len(SCHLAGE_LCM_CONFIG_SLOTS)
            assert (
                len(fake_schlage_cloud.calls) - fake_schlage_cloud.count("get_codes")
                == writes
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
            await hass.config_entries.async_unload(entry.entry_id)

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
        with _schlage_entry(hass, schlage_lock_entity) as entry:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await async_advance_sync_ticks(hass, freezer, 5)
            fake_schlage_cloud.read_lag = 3 * MAX_SYNC_ATTEMPTS

            await async_set_slot_pin(hass, entry, 1, "4321")
            await async_advance_sync_ticks(hass, freezer, 30)
            # The refused add left the slot empty; the next poll sees that.
            await async_advance_clock(hass, freezer, 6 * 60)
            await async_advance_sync_ticks(hass, freezer, 5)

            assert lcm_issue_ids(hass, "slot_suspended") == []
            assert "4321" in (code for _n, code in fake_schlage_cloud.codes.values())
            await hass.config_entries.async_unload(entry.entry_id)

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
        with _schlage_entry(hass, schlage_lock_entity) as entry:
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            await async_advance_sync_ticks(hass, freezer, 10)

            assert lcm_issue_ids(hass, "slot_suspended") == []
            assert sorted(
                code for _n, code in fake_schlage_cloud.codes.values()
            ) == sorted(c[CONF_PIN] for c in SCHLAGE_LCM_CONFIG_SLOTS.values())
            await hass.config_entries.async_unload(entry.entry_id)
