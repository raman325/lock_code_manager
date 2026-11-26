"""Tests for the Yale Access Bluetooth lock provider."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lock import LockState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.model.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    SlotCredential,
    WriteResult,
    credential_from_slot,
)
from custom_components.lock_code_manager.model.exceptions import (
    CodeRejectedError,
    LockCodeManagerProviderError,
    LockOperationFailed,
)
from custom_components.lock_code_manager.providers.yalexs_ble import (
    EVENT_LOCK_ACTIVITY,
    YALEXS_BLE_DOMAIN,
    YaleXSBLELock,
)
from tests.providers.helpers import (
    ProviderNativeTransportContractTests,
    ServiceProviderConnectionTests,
    register_mock_service,
)

from .conftest import LOCK_ENTITY_ID


def _pin_cred(slot: int, pin: str) -> Credential:
    """Build a known-PIN credential for a slot."""
    return credential_from_slot(slot, SlotCredential.known(pin))


def _cred_ref(slot: int) -> CredentialRef:
    """Build a CredentialRef for a slot."""
    return CredentialRef(user_id=slot, type=CredentialType.PIN, slot=slot)


def _status_handler(occupied: set[int]) -> AsyncMock:
    """Answer get_lock_credential_status as the integration does, per entity."""

    async def _status(call):
        slot = call.data["credential_index"]
        return {
            call.data["entity_id"]: {
                "credential_exists": slot in occupied,
                "name": None,
            }
        }

    return AsyncMock(side_effect=_status)


# ---------------------------------------------------------------------------
# Connection and availability
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for the Yale Access Bluetooth provider."""


@pytest.mark.parametrize(
    ("state", "available"),
    [
        pytest.param(LockState.LOCKED, True, id="locked"),
        pytest.param(STATE_UNKNOWN, True, id="unknown"),
        pytest.param(STATE_UNAVAILABLE, False, id="unavailable"),
        pytest.param(None, False, id="no_state"),
    ],
)
async def test_device_available_follows_the_lock_entity(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    state: str | None,
    available: bool,
) -> None:
    """The lock is available as its entity is; no slot is read to find out."""
    status = _status_handler(set())
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "get_lock_credential_status", status)
    if state is not None:
        hass.states.async_set(yalexs_ble_lock.lock.entity_id, state)

    assert await yalexs_ble_lock.async_is_device_available() is available
    status.assert_not_awaited()


class TestNativeTransportContract(ProviderNativeTransportContractTests):
    """A native OSError from the status action surfaces as LockDisconnected."""

    native_transport_read_service = "get_lock_credential_status"

    @pytest.fixture
    def provider_lock(
        self,
        yalexs_ble_lock: YaleXSBLELock,
        simple_lcm_config_entry: MockConfigEntry,
    ) -> YaleXSBLELock:
        """A lock with managed slots, so the read reaches the action."""
        return yalexs_ble_lock


# ---------------------------------------------------------------------------
# Constants and capabilities
# ---------------------------------------------------------------------------


async def test_provider_constants(yalexs_ble_lock: YaleXSBLELock) -> None:
    """Domain, code slot events and a modest scan interval."""
    assert yalexs_ble_lock.domain == YALEXS_BLE_DOMAIN
    assert yalexs_ble_lock.supports_code_slot_events is True
    assert yalexs_ble_lock.usercode_scan_interval.total_seconds() == 15 * 60
    # One Bluetooth exchange per slot, so the deadline scales with the walk.
    assert yalexs_ble_lock.per_exchange_budget == 300.0


async def test_capabilities_are_pin_only(yalexs_ble_lock: YaleXSBLELock) -> None:
    """PINs only, slots 1-250 and 4-8 digits, as the integration validates."""
    capabilities = await yalexs_ble_lock.async_get_capabilities()
    assert capabilities.supports_user_management is False
    assert set(capabilities.credential_types) == {CredentialType.PIN}
    pin = capabilities.credential_types[CredentialType.PIN]
    assert (pin.num_slots, pin.min_length, pin.max_length) == (250, 4, 8)


# ---------------------------------------------------------------------------
# async_get_users
# ---------------------------------------------------------------------------


async def test_get_users_reads_each_managed_slot(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """An occupied slot is unreadable (write-only PINs); an empty one is empty."""
    status = _status_handler({1})
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "get_lock_credential_status", status)

    codes = await yalexs_ble_lock.async_get_usercodes()

    assert codes == {1: SlotCredential.unreadable(), 2: SlotCredential.empty()}
    asked = sorted(
        call.args[0].data["credential_index"] for call in status.await_args_list
    )
    assert asked == [1, 2]


async def test_get_users_no_managed_slots(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """With no managed slots nothing is read from the lock."""
    status = _status_handler(set())
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "get_lock_credential_status", status)

    assert await yalexs_ble_lock.async_get_users() == []
    status.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({LOCK_ENTITY_ID: {"name": None}}, id="no_credential_exists"),
        pytest.param({LOCK_ENTITY_ID: "nope"}, id="entity_not_a_dict"),
    ],
)
async def test_get_users_malformed_status_response(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    simple_lcm_config_entry: MockConfigEntry,
    response: dict,
) -> None:
    """A status answer without credential_exists is a provider error."""
    register_mock_service(
        hass,
        YALEXS_BLE_DOMAIN,
        "get_lock_credential_status",
        AsyncMock(return_value=response),
    )

    with pytest.raises(LockCodeManagerProviderError):
        await yalexs_ble_lock.async_get_users()


# ---------------------------------------------------------------------------
# async_set_credential / async_delete_credential
# ---------------------------------------------------------------------------


async def test_set_credential_programs_the_slot(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """The PIN, slot and name go to set_lock_credential."""
    set_handler = AsyncMock(return_value=None)
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "set_lock_credential", set_handler)

    result = await yalexs_ble_lock.async_set_credential(
        3, _pin_cred(3, "1234"), "1234", name="Guest"
    )

    assert result is WriteResult.CONFIRMED
    assert set_handler.await_args.args[0].data == {
        "entity_id": LOCK_ENTITY_ID,
        "credential_type": "pin",
        "credential_index": 3,
        "credential_data": "1234",
        "name": "Guest",
    }


async def test_set_credential_without_a_name(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """No name is sent when the user has none."""
    set_handler = AsyncMock(return_value=None)
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "set_lock_credential", set_handler)
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "clear_lock_credential", AsyncMock(return_value=None)
    )

    await yalexs_ble_lock.async_set_credential(
        3, _pin_cred(3, "1234"), "1234", name=None
    )

    assert "name" not in set_handler.await_args.args[0].data


async def test_set_credential_refusal_is_the_slots(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """The integration's validation error (e.g. PIN in another slot) is a refusal."""
    refusal = ServiceValidationError(
        translation_domain=YALEXS_BLE_DOMAIN, translation_key="pin_already_in_use"
    )
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "set_lock_credential", AsyncMock(side_effect=refusal)
    )

    with pytest.raises(CodeRejectedError) as caught:
        await yalexs_ble_lock.async_set_credential(
            3, _pin_cred(3, "1234"), "1234", name="Guest"
        )
    assert caught.value.code_slot == 3


def _communication_error() -> HomeAssistantError:
    """What core raises when the lock cannot be reached (OSError, BleakError, timeout)."""
    return HomeAssistantError(
        translation_domain=YALEXS_BLE_DOMAIN, translation_key="communication_error"
    )


async def test_set_credential_communication_error_is_a_failed_operation(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """Core's communication_error is no refusal: the operation failed."""
    register_mock_service(
        hass,
        YALEXS_BLE_DOMAIN,
        "set_lock_credential",
        AsyncMock(side_effect=_communication_error()),
    )

    with pytest.raises(LockOperationFailed):
        await yalexs_ble_lock.async_set_credential(
            3, _pin_cred(3, "1234"), "1234", name="Guest"
        )


async def test_get_users_communication_error_is_a_failed_operation(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A slot the lock could not be asked about fails the read."""
    register_mock_service(
        hass,
        YALEXS_BLE_DOMAIN,
        "get_lock_credential_status",
        AsyncMock(side_effect=_communication_error()),
    )

    with pytest.raises(LockOperationFailed):
        await yalexs_ble_lock.async_get_users()


async def test_set_credential_without_a_name_clears_the_old_name_first(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """A nameless set clears the slot first, so the old holder's name is dropped."""
    order: list[str] = []
    clear_handler = AsyncMock(side_effect=lambda call: order.append("clear"))
    set_handler = AsyncMock(side_effect=lambda call: order.append("set"))
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "clear_lock_credential", clear_handler
    )
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "set_lock_credential", set_handler)

    await yalexs_ble_lock.async_set_credential(
        3, _pin_cred(3, "1234"), "1234", name=None
    )

    assert order == ["clear", "set"]
    assert clear_handler.await_args.args[0].data["credential_index"] == 3


async def test_set_credential_with_a_name_does_not_clear_first(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """A named set needs no clear: the name replaces the old one."""
    clear_handler = AsyncMock(return_value=None)
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "clear_lock_credential", clear_handler
    )
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "set_lock_credential", AsyncMock(return_value=None)
    )

    await yalexs_ble_lock.async_set_credential(
        3, _pin_cred(3, "1234"), "1234", name="Guest"
    )

    clear_handler.assert_not_awaited()


async def test_delete_credential_already_empty(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """An empty slot is not cleared again."""
    clear_handler = AsyncMock(return_value=None)
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "get_lock_credential_status", _status_handler(set())
    )
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "clear_lock_credential", clear_handler
    )

    assert await yalexs_ble_lock.async_delete_credential(_cred_ref(3)) is False
    clear_handler.assert_not_awaited()


async def test_delete_credential_clears_an_occupied_slot(
    hass: HomeAssistant, yalexs_ble_lock: YaleXSBLELock
) -> None:
    """An occupied slot is cleared with clear_lock_credential."""
    clear_handler = AsyncMock(return_value=None)
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "get_lock_credential_status", _status_handler({3})
    )
    register_mock_service(
        hass, YALEXS_BLE_DOMAIN, "clear_lock_credential", clear_handler
    )

    assert await yalexs_ble_lock.async_delete_credential(_cred_ref(3)) is True
    assert clear_handler.await_args.args[0].data == {
        "entity_id": LOCK_ENTITY_ID,
        "credential_type": "pin",
        "credential_index": 3,
    }


# ---------------------------------------------------------------------------
# Code slot events from yalexs_ble_lock_activity
# ---------------------------------------------------------------------------

_KEYPAD_UNLOCK = {
    "entity_id": LOCK_ENTITY_ID,
    "source": "pin",
    "slot": 3,
    "master_code": False,
}


async def test_keypad_unlock_records_the_slots_use(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    yalexs_ble_config_entry: MockConfigEntry,
) -> None:
    """A keypad unlock with a slot is recorded as that slot's PIN being used."""
    await yalexs_ble_lock.async_setup(yalexs_ble_config_entry)
    with patch.object(yalexs_ble_lock, "async_fire_code_slot_event") as fire:
        hass.bus.async_fire(EVENT_LOCK_ACTIVITY, _KEYPAD_UNLOCK)
        await hass.async_block_till_done()
    fire.assert_called_once_with(3, to_locked=False, credential_type=CredentialType.PIN)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param({**_KEYPAD_UNLOCK, "entity_id": "lock.other"}, id="other_lock"),
        pytest.param({**_KEYPAD_UNLOCK, "source": "manual", "slot": None}, id="manual"),
        pytest.param({**_KEYPAD_UNLOCK, "slot": None}, id="keypad_lock_button"),
        pytest.param(
            {**_KEYPAD_UNLOCK, "slot": None, "master_code": True}, id="master_code"
        ),
    ],
)
async def test_activity_without_a_users_slot_is_ignored(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    yalexs_ble_config_entry: MockConfigEntry,
    data: dict,
) -> None:
    """Other locks, manual operations, the lock button and the master code record nothing."""
    await yalexs_ble_lock.async_setup(yalexs_ble_config_entry)
    with patch.object(yalexs_ble_lock, "async_fire_code_slot_event") as fire:
        hass.bus.async_fire(EVENT_LOCK_ACTIVITY, data)
        await hass.async_block_till_done()
    fire.assert_not_called()


async def test_setup_is_idempotent_and_unload_stops_listening(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    yalexs_ble_config_entry: MockConfigEntry,
) -> None:
    """A second setup adds no second listener; unload removes it."""
    await yalexs_ble_lock.async_setup(yalexs_ble_config_entry)
    await yalexs_ble_lock.async_setup(yalexs_ble_config_entry)
    with patch.object(yalexs_ble_lock, "async_fire_code_slot_event") as fire:
        hass.bus.async_fire(EVENT_LOCK_ACTIVITY, _KEYPAD_UNLOCK)
        await hass.async_block_till_done()
        assert fire.call_count == 1

        await yalexs_ble_lock.async_unload(False)
        hass.bus.async_fire(EVENT_LOCK_ACTIVITY, _KEYPAD_UNLOCK)
        await hass.async_block_till_done()
        assert fire.call_count == 1


async def test_hard_refresh_reads_managed_slots_and_the_asked_ones(
    hass: HomeAssistant,
    yalexs_ble_lock: YaleXSBLELock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A hard refresh for an unmanaged slot reads it as well as the managed ones."""
    status = _status_handler({7})
    register_mock_service(hass, YALEXS_BLE_DOMAIN, "get_lock_credential_status", status)

    codes = await yalexs_ble_lock.async_hard_refresh_codes([7])

    assert codes[7] == SlotCredential.unreadable()
    asked = sorted(
        call.args[0].data["credential_index"] for call in status.await_args_list
    )
    assert asked == [1, 2, 7]


async def test_activity_for_the_main_entity_reaches_the_secure_mode_entity(
    hass: HomeAssistant, yalexs_ble_config_entry: MockConfigEntry
) -> None:
    """
    The event carries the main lock entity; a provider on the secure-mode entity still hears it.

    Both entities belong to the lock's device, which is what is matched.
    """
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=yalexs_ble_config_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:01")},
    )
    entity_reg = er.async_get(hass)
    entity_reg.async_get_or_create(
        "lock",
        YALEXS_BLE_DOMAIN,
        "main",
        config_entry=yalexs_ble_config_entry,
        device_id=device.id,
        suggested_object_id="front_door",
    )
    secure = entity_reg.async_get_or_create(
        "lock",
        YALEXS_BLE_DOMAIN,
        "secure",
        config_entry=yalexs_ble_config_entry,
        device_id=device.id,
        suggested_object_id="front_door_secure_mode",
    )
    lock = YaleXSBLELock(
        hass, dr.async_get(hass), entity_reg, yalexs_ble_config_entry, secure
    )
    await lock.async_setup(yalexs_ble_config_entry)
    with patch.object(lock, "async_fire_code_slot_event") as fire:
        hass.bus.async_fire(
            EVENT_LOCK_ACTIVITY,
            {**_KEYPAD_UNLOCK, "entity_id": "lock.front_door", "device_id": device.id},
        )
        hass.bus.async_fire(
            EVENT_LOCK_ACTIVITY,
            {**_KEYPAD_UNLOCK, "entity_id": "lock.other", "device_id": "another"},
        )
        await hass.async_block_till_done()
    fire.assert_called_once_with(3, to_locked=False, credential_type=CredentialType.PIN)
    await lock.async_unload(False)


async def test_smart_code_handle_has_99_pin_slots(
    hass: HomeAssistant, yalexs_ble_config_entry: MockConfigEntry
) -> None:
    """The Smart Code Handle (CERES) refuses slot 100 and up, so it has 99 slots."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=yalexs_ble_config_entry.entry_id,
        connections={(dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:02")},
        model="CERES",
    )
    entity_reg = er.async_get(hass)
    entity = entity_reg.async_get_or_create(
        "lock",
        YALEXS_BLE_DOMAIN,
        "handle",
        config_entry=yalexs_ble_config_entry,
        device_id=device.id,
    )
    lock = YaleXSBLELock(
        hass, dr.async_get(hass), entity_reg, yalexs_ble_config_entry, entity
    )

    capabilities = await lock.async_get_capabilities()

    assert capabilities.credential_types[CredentialType.PIN].num_slots == 99
