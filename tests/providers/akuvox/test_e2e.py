"""Full lifecycle E2E tests for Akuvox lock provider."""

from datetime import timedelta
from unittest.mock import AsyncMock

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import (
    ATTR_ENTITY_ID,
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
    SERVICE_HARD_REFRESH_USERCODES,
)
from custom_components.lock_code_manager.model.credentials import (
    CredentialRef,
    CredentialType,
    SlotCredential,
    WriteResult,
    credential_from_slot,
)
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


# Names a user made outside Lock Code Manager might carry: a plain name, and
# the bare-digit and compact shapes only Matter writes as tags.
_FOREIGN_NAMES = pytest.mark.parametrize("foreign_name", ["Mom", "1", "lcm2"])


def _foreign_writes(device: FakeAkuvoxDevice, user_id: str) -> list[tuple[str, dict]]:
    """Return the writes the device saw addressed to ``user_id``."""
    return [
        (service, data)
        for service, data in device.calls
        if service in ("modify_user", "delete_user") and data.get("id") == user_id
    ]


class TestForeignUsers:
    """A user without Lock Code Manager's tag is never read, changed or deleted."""

    @pytest.fixture
    def lcm_entry(self, hass: HomeAssistant, akuvox_lock_entity: er.RegistryEntry):
        """Return an entry on the Akuvox lock for slots 1 to 3, not yet set up."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [akuvox_lock_entity.entity_id],
                # Slot 3 is off, so a slot stays free for a hard refresh too.
                CONF_SLOTS: AKUVOX_LCM_CONFIG_SLOTS
                | {3: {CONF_NAME: "slot3", CONF_PIN: "4321", CONF_ENABLED: False}},
            },
            unique_id="test_akuvox_foreign",
        )
        entry.add_to_hass(hass)
        return entry

    @_FOREIGN_NAMES
    async def test_a_foreign_user_survives_setup_and_hard_refresh(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_akuvox_device: FakeAkuvoxDevice,
        akuvox_lock_entity: er.RegistryEntry,
        lcm_entry: MockConfigEntry,
        foreign_name: str,
    ) -> None:
        """
        A homeowner's user with a PIN keeps its name and PIN, and is no slot.

        Setup and every hard refresh renamed untagged users into free slots,
        taking over codes Lock Code Manager never made.
        """
        mom = fake_akuvox_device.add(foreign_name, "9999")
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_HARD_REFRESH_USERCODES,
            {ATTR_ENTITY_ID: akuvox_lock_entity.entity_id},
            blocking=True,
        )
        await async_advance_sync_ticks(hass, freezer, 5)
        lock = lcm_entry.runtime_data.locks[akuvox_lock_entity.entity_id]
        codes = await lock.async_get_usercodes()
        await hass.config_entries.async_unload(lcm_entry.entry_id)

        assert _foreign_writes(fake_akuvox_device, mom) == []
        user = next(u for u in fake_akuvox_device.users if u["id"] == mom)
        assert (user["name"], user["private_pin"]) == (foreign_name, "9999")
        assert codes == {
            1: SlotCredential.known("1234"),
            2: SlotCredential.known("5678"),
            3: SlotCredential.empty(),
        }

    async def test_a_pin_a_foreign_user_holds_is_added_beside_it(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_akuvox_device: FakeAkuvoxDevice,
        lcm_entry: MockConfigEntry,
    ) -> None:
        """
        Slot 1's PIN, already a homeowner's, is a new user; theirs is untouched.

        The integration does not check PINs for duplicates, so a device that
        takes them holds both users.
        """
        mom = fake_akuvox_device.add("Mom", AKUVOX_LCM_CONFIG_SLOTS[1][CONF_PIN])
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)
        await hass.config_entries.async_unload(lcm_entry.entry_id)

        assert _foreign_writes(fake_akuvox_device, mom) == []
        assert fake_akuvox_device.pin_of("Mom") == "1234"
        assert fake_akuvox_device.pin_of(make_tagged_name(1, "slot1")) == "1234"

    async def test_a_pin_the_device_refuses_as_a_duplicate_is_held(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_akuvox_device: FakeAkuvoxDevice,
        lcm_entry: MockConfigEntry,
    ) -> None:
        """
        A device refusing slot 1's PIN as a homeowner's is retried, then held.

        The device's refusal reaches Home Assistant as the integration's
        generic device error, not a validation error, so it is a failed
        write: retried a bounded number of times, then the slot is held with
        a repair. The homeowner's user is never touched.
        """
        mom = fake_akuvox_device.add("Mom", AKUVOX_LCM_CONFIG_SLOTS[1][CONF_PIN])
        fake_akuvox_device.refuse_pin(AKUVOX_LCM_CONFIG_SLOTS[1][CONF_PIN])
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)
        slot_1_adds = [
            data
            for service, data in fake_akuvox_device.calls
            if service == "add_user" and parse_tag(data["name"])[0] == 1
        ]
        suspended = lcm_issue_ids(hass, "slot_suspended")
        await hass.config_entries.async_unload(lcm_entry.entry_id)

        assert 1 <= len(slot_1_adds) <= LIMIT + 1
        assert suspended
        assert _foreign_writes(fake_akuvox_device, mom) == []
        assert fake_akuvox_device.pin_of("Mom") == "1234"
        assert fake_akuvox_device.pin_of(make_tagged_name(2, "slot2")) == "5678"


class TestRefusedWrite:
    """
    The integration's validation error is the device refusing the write.

    A user the device says the cloud provisioned, though its ``source_type``
    marks it local, cannot be changed or deleted: the integration raises
    ``ServiceValidationError`` for it, every time. Taken as no answer, the
    write was sent again on every retry until the slot's breaker tripped.
    """

    @pytest.fixture
    def lcm_entry(self, hass: HomeAssistant, akuvox_lock_entity: er.RegistryEntry):
        """Return an entry on the Akuvox lock for slots 1 and 2, not yet set up."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [akuvox_lock_entity.entity_id],
                CONF_SLOTS: AKUVOX_LCM_CONFIG_SLOTS,
            },
            unique_id="test_akuvox_refused",
        )
        entry.add_to_hass(hass)
        return entry

    async def test_a_refused_pin_disables_the_slot_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_akuvox_device: FakeAkuvoxDevice,
        lcm_entry: MockConfigEntry,
    ) -> None:
        """Slot 1's PIN change is refused once and the slot is turned off."""
        user = fake_akuvox_device.add(
            make_tagged_name(1, "slot1"), "0000", source="Cloud"
        )
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 30)
        modifies = [
            data
            for service, data in fake_akuvox_device.calls
            if service == "modify_user" and data["id"] == user
        ]
        disabled = lcm_issue_ids(hass, "slot_disabled")
        await hass.config_entries.async_unload(lcm_entry.entry_id)

        assert len(modifies) == 1
        assert disabled
        assert fake_akuvox_device.pin_of(make_tagged_name(1, "slot1")) == "0000"

    async def test_a_refused_delete_holds_the_slot_once(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        fake_akuvox_device: FakeAkuvoxDevice,
        lcm_entry: MockConfigEntry,
    ) -> None:
        """Slot 1's delete is refused once and the slot is held with a repair."""
        fake_akuvox_device.add(make_tagged_name(1, "slot1"), "1234", source="Cloud")
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()
        await async_advance_sync_ticks(hass, freezer, 5)

        await async_switch_slot(hass, lcm_entry, 1, SERVICE_TURN_OFF)
        await async_advance_sync_ticks(hass, freezer, 30)
        deletes = fake_akuvox_device.count("delete_user")
        suspended = lcm_issue_ids(hass, "slot_suspended")
        await hass.config_entries.async_unload(lcm_entry.entry_id)

        assert deletes == 1
        assert suspended
        assert fake_akuvox_device.pin_of(make_tagged_name(1, "slot1")) == "1234"
