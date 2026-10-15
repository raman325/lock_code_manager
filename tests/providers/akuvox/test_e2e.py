"""Full lifecycle E2E tests for Akuvox lock provider."""

from datetime import timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.model.credentials import (
    CredentialRef,
    CredentialType,
    WriteResult,
    credential_from_slot,
)
from custom_components.lock_code_manager.model.models import SlotCredential
from custom_components.lock_code_manager.providers._util import (
    make_tagged_name,
    parse_tag,
)
from custom_components.lock_code_manager.providers.akuvox import (
    AKUVOX_DOMAIN,
    AkuvoxLock,
)
from custom_components.lock_code_manager.sync.slot import LIMIT
from tests.common import (
    async_advance_sync_ticks,
    async_switch_slot,
    lcm_issue_ids,
    user_subentries,
)
from tests.providers.helpers import register_mock_service

from .conftest import AKUVOX_LCM_CONFIG_SLOTS, make_user
from .fake_service import FakeAkuvoxDevice


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Akuvox provider."""

    async def test_provider_discovered_as_akuvox(
        self,
        hass: HomeAssistant,
        e2e_lcm_config_entry: MockConfigEntry,
        akuvox_lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Akuvox lock and creates an AkuvoxLock."""
        lock = e2e_lcm_config_entry.runtime_data.locks.get(akuvox_lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, AkuvoxLock)
        assert lock.domain == AKUVOX_DOMAIN
        assert lock.supports_code_slot_events is False
        assert lock.usercode_scan_interval == timedelta(minutes=2)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_akuvox_lock.coordinator is not None


class TestSetAndClearCredentials:
    """Verify set/clear operations call the correct Akuvox services."""

    async def test_set_credential_new(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a credential on an empty slot and verify add_user service was called."""
        akuvox_mock_services["add_user"].reset_mock()
        result = await e2e_akuvox_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("9999")),
            "9999",
            name="E2E Guest",
            source="direct",
        )

        assert result is WriteResult.CONFIRMED
        assert akuvox_mock_services["add_user"].call_count >= 1

    async def test_set_credential_existing(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a credential on an occupied slot and verify modify_user service was called."""
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report an existing tagged user
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "lcm:1:Existing", "1234")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        akuvox_mock_services["modify_user"].reset_mock()
        result = await e2e_akuvox_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("5678")),
            "5678",
            name="Updated",
            source="direct",
        )

        assert result is WriteResult.CONFIRMED
        assert akuvox_mock_services["modify_user"].call_count >= 1

    async def test_delete_credential(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Delete a credential and verify delete_user service was called."""
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report a tagged user to delete
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "lcm:1:Guest", "1234")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        akuvox_mock_services["delete_user"].reset_mock()
        result = await e2e_akuvox_lock.async_delete_credential(
            CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        )

        assert result is True
        assert akuvox_mock_services["delete_user"].call_count >= 1

    async def test_base_orchestration_set_credential(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """The worker's write, async_set_usercode, routes through base → async_set_credential."""
        akuvox_mock_services["add_user"].reset_mock()
        await e2e_akuvox_lock.async_set_usercode(1, "9999", "E2E Guest", source="sync")

        assert akuvox_mock_services["add_user"].call_count >= 1

    async def test_base_orchestration_clear_credential(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """The worker's clear, async_clear_usercode, routes through base → async_delete_credential."""
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report a tagged user to delete
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "lcm:1:Guest", "1234")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        akuvox_mock_services["delete_user"].reset_mock()
        await e2e_akuvox_lock.async_clear_usercode(1)

        assert akuvox_mock_services["delete_user"].call_count >= 1


class TestGetUsers:
    """Verify reading users (usercodes) from the Akuvox lock."""

    async def test_get_users_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        async_get_users returns mapped slot data from Akuvox.

        After initial setup with empty users, both managed slots should
        be EMPTY. When the mock reports a tagged user with a Personal
        Identification Number on slot 1, that slot should report the PIN value.
        """
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report a tagged user on slot 1
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "lcm:1:Guest", "4321")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        users = await e2e_akuvox_lock.async_get_users()
        user_map = {u.user_id: u for u in users}

        assert user_map[1].pin_credentials[0].state == SlotCredential.known("4321")
        assert user_map[2].pin_credentials[0].state is SlotCredential.empty()

    async def test_get_usercodes_projection(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """The base async_get_usercodes projection produces the same slot->SlotCredential dict."""
        entity_id = akuvox_lock_entity.entity_id

        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "lcm:1:Guest", "4321")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        codes = await e2e_akuvox_lock.async_get_usercodes()

        assert codes[1] == SlotCredential.known("4321")
        assert codes[2] is SlotCredential.empty()

    async def test_get_usercodes_empty(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
    ) -> None:
        """All managed slots are EMPTY when the device has no users."""
        codes = await e2e_akuvox_lock.async_get_usercodes()

        assert codes[1] is SlotCredential.empty()
        assert codes[2] is SlotCredential.empty()


class TestOpenBugs:
    """Open Akuvox bugs, each pinned by a strict xfail on its own assertion."""

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason=(
            "open: the tag pass hands an unmanaged user to any slot an entry "
            "lists, a disabled entry's included"
        ),
    )
    async def test_a_homeowner_is_not_tagged_into_a_disabled_entry_s_slot(
        self,
        hass: HomeAssistant,
        fake_akuvox_device: FakeAkuvoxDevice,
        akuvox_lock_entity: er.RegistryEntry,
    ) -> None:
        """Slot 3 belongs to a disabled entry's user, so it is not free to adopt a user."""
        fake_akuvox_device.add(make_tagged_name(1, "slot1"), "1234")
        fake_akuvox_device.add(make_tagged_name(2, "slot2"), "5678")
        mom = fake_akuvox_device.add("Mom", "9999")
        MockConfigEntry(
            domain=DOMAIN,
            data={CONF_LOCKS: [akuvox_lock_entity.entity_id]},
            subentries_data=user_subentries(
                {3: {CONF_NAME: "slot3", CONF_PIN: "5555", CONF_ENABLED: True}}
            ),
            unique_id="test_akuvox_disabled",
            disabled_by=ConfigEntryDisabler.USER,
        ).add_to_hass(hass)
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [akuvox_lock_entity.entity_id],
                CONF_SLOTS: AKUVOX_LCM_CONFIG_SLOTS,
            },
            unique_id="test_akuvox_batch6",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        name = next(u["name"] for u in fake_akuvox_device.users if u["id"] == mom)
        await hass.config_entries.async_unload(entry.entry_id)

        assert parse_tag(name)[0] != 3

    async def test_a_delete_the_device_keeps_ignoring_is_reported(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_akuvox_device: FakeAkuvoxDevice,
        akuvox_lock_entity: er.RegistryEntry,
    ) -> None:
        """A user turned off whose delete never sticks is suspended with a repair."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [akuvox_lock_entity.entity_id],
                CONF_SLOTS: AKUVOX_LCM_CONFIG_SLOTS,
            },
            unique_id="test_akuvox_batch6",
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)
        if fake_akuvox_device.pin_of(make_tagged_name(1, "slot1")) != "1234":
            pytest.fail("slot 1's user never reached the device")
        fake_akuvox_device.ignore("delete_user")

        await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
        await async_advance_sync_ticks(hass, freezer, 30)
        deletes = fake_akuvox_device.count("delete_user")
        suspended = lcm_issue_ids(hass, "slot_suspended")
        await hass.config_entries.async_unload(entry.entry_id)

        assert deletes <= LIMIT + 1
        assert suspended
