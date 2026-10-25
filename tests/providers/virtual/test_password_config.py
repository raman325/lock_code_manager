"""A password in the configuration, end to end on a Virtual lock."""

import pytest

from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import CONF_LOCKS, DOMAIN
from custom_components.lock_code_manager.ha.entry import available_credential_types
from custom_components.lock_code_manager.ha.runtime import lock_registry
from custom_components.lock_code_manager.model.config import build_slot_unique_id
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.model.queries import (
    get_entry_config,
    subentry_id_for_slot,
)

from ...common import async_add_users
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


async def test_a_password_alone_can_enable_the_slot(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Enabling needs a credential of any type."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await coordinator.async_request_active_toggle(False)

    await coordinator.async_request_active_toggle(True)

    assert coordinator.is_enabled is True


async def test_another_users_password_is_refused(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Duplicates are per type: another user's password is refused."""
    slot_coordinators = lcm_config_entry.runtime_data.slot_coordinators
    await slot_coordinators[2].async_request_credential_update(
        CredentialType.PASSWORD, "shared"
    )

    with pytest.raises(ServiceValidationError):
        await slot_coordinators[1].async_request_credential_update(
            CredentialType.PASSWORD, "shared"
        )


async def test_a_password_may_equal_a_pin(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A password the same string as another user's PIN is not a duplicate."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "5678")


def _password_entity(hass: HomeAssistant, entry, slot: int) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        TEXT_DOMAIN, DOMAIN, build_slot_unique_id(entry.entry_id, slot, "password")
    )


async def test_each_slot_gets_a_password_entity(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The entry offers passwords: every slot has the entity, in password mode."""
    for slot in (1, 2):
        entity_id = _password_entity(hass, lcm_config_entry, slot)
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["mode"] == "password"


async def test_a_user_added_later_gets_a_password_entity(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A new slot in an entry that already offers passwords comes with one."""
    await async_add_users(
        hass, lcm_config_entry, {"slot3": {CONF_ENABLED: True, CONF_PIN: "3333"}}
    )
    await hass.async_block_till_done()
    assert 3 in get_entry_config(lcm_config_entry).slot_numbers

    entity_id = _password_entity(hass, lcm_config_entry, 3)
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None


async def test_a_slot_number_reused_gets_a_password_entity_again(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A removed user's number, issued again, comes with the entity again."""
    subentry_id = subentry_id_for_slot(lcm_config_entry, 2)
    assert subentry_id is not None
    hass.config_entries.async_remove_subentry(lcm_config_entry, subentry_id)
    await hass.async_block_till_done()
    assert _password_entity(hass, lcm_config_entry, 2) is None

    await async_add_users(
        hass, lcm_config_entry, {"again": {CONF_ENABLED: True, CONF_PIN: "2222"}}
    )
    await hass.async_block_till_done()
    assert get_entry_config(lcm_config_entry).name_for(2) == "again"

    entity_id = _password_entity(hass, lcm_config_entry, 2)
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None


async def test_setting_the_password_entity_reaches_the_lock(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The entity writes through the coordinator."""
    entity_id = _password_entity(hass, lcm_config_entry, 1)

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: "open sesame"},
        blocking=True,
    )
    await _settled(hass)

    assert get_virtual_lock(hass, lcm_config_entry)._data["1"]["password"] == (
        "open sesame"
    )


async def test_a_refused_password_never_shows_in_the_error_or_logs(
    hass: HomeAssistant, lcm_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """The leak test: a marker submitted through the entity appears nowhere."""
    entity_id = _password_entity(hass, lcm_config_entry, 1)
    marker = "leak-marker-91c2-é"

    with pytest.raises(ServiceValidationError) as refused:
        await hass.services.async_call(
            TEXT_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: marker},
            blocking=True,
        )

    assert "leak-marker-91c2" not in str(refused.value)
    assert "leak-marker-91c2" not in caplog.text


async def test_the_password_entity_leaves_with_the_last_password_lock(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Removing the entry's only password lock removes the entity; the value stays."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
    await _settled(hass)

    result = await hass.config_entries.options.async_init(lcm_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOCKS: []}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert VIRTUAL_LOCK_ENTITY_ID not in lcm_config_entry.runtime_data.locks
    assert _password_entity(hass, lcm_config_entry, 1) is None
    assert _password_entity(hass, lcm_config_entry, 2) is None
    assert get_entry_config(lcm_config_entry).slot(1)["password"] == "pw"
