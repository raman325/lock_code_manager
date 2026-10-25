"""A password in the configuration, end to end on a Virtual lock."""

from homeassistant.const import CONF_ENABLED
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.ha.entry import available_credential_types
from custom_components.lock_code_manager.ha.runtime import lock_registry
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.model.queries import get_entry_config

from .conftest import VIRTUAL_LOCK_ENTITY_ID, get_virtual_lock

PASSWORD_1 = CredentialAddress(1, CredentialType.PASSWORD)


async def _settled(hass: HomeAssistant) -> None:
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    await hass.async_block_till_done()
    await worker.settled()


async def test_an_entry_with_a_password_lock_offers_passwords(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The entry's types are the union of what its locks take."""
    assert available_credential_types(lcm_config_entry) == {
        CredentialType.PIN,
        CredentialType.PASSWORD,
    }


async def test_a_configured_password_reaches_the_lock(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Set through the coordinator, the password is written beside the PIN."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(
        CredentialType.PASSWORD, "open sesame"
    )
    await _settled(hass)

    lock = get_virtual_lock(hass, lcm_config_entry)
    assert lock._data["1"]["password"] == "open sesame"
    assert lock._data["1"]["code"] == "1234"
    assert get_entry_config(lcm_config_entry).slot(1)["password"] == "open sesame"


async def test_a_password_is_kept_as_typed(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Passwords are not stripped (spec decision 7)."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(CredentialType.PASSWORD, " pw ")
    await _settled(hass)

    assert get_virtual_lock(hass, lcm_config_entry)._data["1"]["password"] == " pw "


async def test_clearing_the_password_leaves_the_slot_on(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The PIN remains: the slot stays enabled (spec decision 4)."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
    await _settled(hass)

    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "")
    await _settled(hass)

    assert coordinator.is_enabled is True
    assert coordinator.is_active is True
    assert "password" not in get_virtual_lock(hass, lcm_config_entry)._data["1"]


async def test_clearing_the_last_credential_turns_the_slot_off(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """With only a password left, clearing it disables the slot."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await _settled(hass)
    assert coordinator.is_enabled is True  # the password remains

    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "")

    assert coordinator.is_enabled is False
    assert get_entry_config(lcm_config_entry).slot(1)[CONF_ENABLED] is False


async def test_a_password_only_user_is_in_sync(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """No PIN, a password: the PIN address is kept clear, the password set."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await _settled(hass)

    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    assert worker.slots[pin_address(1)].want is None
    assert worker.slots[PASSWORD_1].want == "pw"
    assert get_virtual_lock(hass, lcm_config_entry)._data["1"] == {
        "password": "pw",
        "name": "slot1",
    }
