"""A password in the configuration, end to end on a Virtual lock."""

import json
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_CREDENTIAL_TYPE,
    ATTR_VALUE as ATTR_CREDENTIAL_VALUE,
    CONF_LOCKS,
    CONF_SLOT,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
    SERVICE_ADD_USER,
    SERVICE_CLEAR_CREDENTIAL,
    SERVICE_SET_CREDENTIAL,
    SUBENTRY_TYPE_USER,
)
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

from ...common import LOCK_1_ENTITY_ID, async_add_users
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


async def test_the_password_entity_stays_while_a_password_lock_remains(
    hass: HomeAssistant, virtual_lock_entity, mock_lock_config_entry
) -> None:
    """Removing a PIN-only lock leaves the entry's password lock, and the entity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [VIRTUAL_LOCK_ENTITY_ID, LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
                2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
            },
        },
        unique_id="test_virtual_and_pin_only",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    pin_only = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert CredentialType.PASSWORD not in pin_only.managed_credential_types
    assert _password_entity(hass, entry, 1) is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOCKS: [VIRTUAL_LOCK_ENTITY_ID]}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert LOCK_1_ENTITY_ID not in entry.runtime_data.locks
    for slot in (1, 2):
        entity_id = _password_entity(hass, entry, slot)
        assert entity_id is not None
        assert hass.states.get(entity_id) is not None

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_password_longer_than_a_state_is_refused(
    hass: HomeAssistant, lcm_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Over Home Assistant's 255-character state limit, refused before it is kept.

    Kept, it would make every later state write of its entity raise, quoting it.
    """
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    marker = "leak-marker-77ab"
    value = marker + "x" * (300 - len(marker))

    with pytest.raises(ServiceValidationError) as refused:
        await coordinator.async_request_credential_update(
            CredentialType.PASSWORD, value
        )
    await _settled(hass)

    assert not get_entry_config(lcm_config_entry).slot(1).get("password")
    assert "password" not in get_virtual_lock(hass, lcm_config_entry)._data["1"]
    assert marker not in str(refused.value)
    assert marker not in caplog.text


async def test_set_credential_sets_a_password(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The action writes the password, unstripped."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {
            CONF_NAME: "slot1",
            ATTR_CREDENTIAL_TYPE: "password",
            ATTR_CREDENTIAL_VALUE: " open sesame",
            "config_entry_id": lcm_config_entry.entry_id,
        },
        blocking=True,
    )

    assert get_entry_config(lcm_config_entry).slot(1)["password"] == " open sesame"


async def test_set_credential_refuses_a_blank_pin(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A PIN of spaces strips to nothing: refused, not taken as a clear."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_CREDENTIAL,
            {
                CONF_NAME: "slot1",
                ATTR_CREDENTIAL_TYPE: "pin",
                ATTR_CREDENTIAL_VALUE: "   ",
                "config_entry_id": lcm_config_entry.entry_id,
            },
            blocking=True,
        )

    assert get_entry_config(lcm_config_entry).slot(1)[CONF_PIN] == "1234"


async def test_clear_credential_clears_the_password_only(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The PIN stays, and so does the slot."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_CREDENTIAL,
        {
            CONF_NAME: "slot1",
            ATTR_CREDENTIAL_TYPE: "password",
            "config_entry_id": lcm_config_entry.entry_id,
        },
        blocking=True,
    )

    slot = get_entry_config(lcm_config_entry).slot(1)
    assert not slot.get("password")
    assert slot[CONF_PIN] == "1234"
    assert slot[CONF_ENABLED] is True


async def test_add_user_with_a_password_only(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A password is enough to be added enabled."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            "users": [{CONF_NAME: "Guest", "password": "welcome in"}],
            "config_entry_id": lcm_config_entry.entry_id,
        },
        blocking=True,
    )

    config = get_entry_config(lcm_config_entry)
    assert config.users["Guest"]["password"] == "welcome in"
    assert config.users["Guest"][CONF_ENABLED] is True


async def test_add_user_refuses_a_password_another_user_holds(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Duplicates are refused within the batch and against the entry."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "users": [
                    {CONF_NAME: "A", "password": "same"},
                    {CONF_NAME: "B", "password": "same"},
                ],
                "config_entry_id": lcm_config_entry.entry_id,
            },
            blocking=True,
        )


async def test_add_user_refuses_a_pin_a_user_in_the_entry_holds(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The duplicate check reaches the users already stored, not just the batch."""
    with pytest.raises(ServiceValidationError, match="'slot1' already has"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "users": [{CONF_NAME: "Guest", CONF_PIN: "1234"}],
                "config_entry_id": lcm_config_entry.entry_id,
            },
            blocking=True,
        )

    assert "Guest" not in get_entry_config(lcm_config_entry).users


async def test_add_user_refuses_a_password_outside_printable_ascii(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The character rule holds for a new user, and the error never quotes it."""
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "users": [{CONF_NAME: "Guest", "password": "café"}],
                "config_entry_id": lcm_config_entry.entry_id,
            },
            blocking=True,
        )

    assert "café" not in str(err.value)
    assert "Guest" not in get_entry_config(lcm_config_entry).users


async def _subentry_configure(hass: HomeAssistant, flow_id: str, user_input):
    """Submit to a subentry flow, waiting out an allocation's progress step."""
    result = await hass.config_entries.subentries.async_configure(flow_id, user_input)
    while result["type"] == FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(flow_id)
    return result


async def _add_user_form(hass: HomeAssistant, entry, path: str = "one"):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
    )
    return await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"next_step_id": path}
    )


async def _edit_user_form(hass: HomeAssistant, entry, slot: int):
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER),
        context={
            "source": "reconfigure",
            "subentry_id": subentry_id_for_slot(entry, slot),
        },
    )
    assert result["step_id"] == "reconfigure"
    return result


async def test_the_add_user_form_offers_a_password(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The entry's lock takes passwords: the form has the field."""
    result = await _add_user_form(hass, lcm_config_entry)

    assert "password" in {str(key) for key in result["data_schema"].schema}


async def test_the_password_field_is_labelled(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Both forms that show it name it: an unlabelled field shows its key."""
    strings = json.loads(
        Path("custom_components/lock_code_manager/strings.json").read_text()
    )
    steps = strings["config_subentries"]["user"]["step"]

    for result in (
        await _add_user_form(hass, lcm_config_entry),
        await _edit_user_form(hass, lcm_config_entry, 1),
    ):
        fields = {str(key) for key in result["data_schema"].schema}
        assert fields == set(steps[result["step_id"]]["data"])


async def test_a_user_with_only_a_password_can_be_added_enabled(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A password is a credential: enabled without a PIN."""
    result = await _add_user_form(hass, lcm_config_entry)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "Guest", CONF_ENABLED: True, "password": "welcome in"},
    )

    assert result["type"] == "create_entry"
    assert result["data"]["password"] == "welcome in"
    assert CONF_SLOT in result["data"]


async def test_a_password_outside_printable_ascii_is_refused_in_the_form(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Refused on its field, and the value appears in no error."""
    result = await _add_user_form(hass, lcm_config_entry)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "Guest", CONF_ENABLED: True, "password": "café-marker"},
    )

    assert result["errors"] == {"password": "invalid_password"}
    assert "café-marker" not in str(result.get("description_placeholders"))


async def test_another_users_password_is_refused_in_the_form(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Per type: slot 1's password, offered as a new user's password."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {
            CONF_NAME: "slot1",
            ATTR_CREDENTIAL_TYPE: "password",
            ATTR_CREDENTIAL_VALUE: "taken",
            "config_entry_id": lcm_config_entry.entry_id,
        },
        blocking=True,
    )
    result = await _add_user_form(hass, lcm_config_entry)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "Guest", CONF_ENABLED: True, "password": "taken"},
    )

    assert result["errors"] == {"password": "password_not_unique"}


async def test_a_password_may_equal_another_users_pin_in_the_form(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Slot 1's PIN as a new user's password is not a duplicate."""
    result = await _add_user_form(hass, lcm_config_entry)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "Guest", CONF_ENABLED: True, "password": "1234"},
    )

    assert result["type"] == "create_entry"


async def test_editing_a_user_offers_and_sets_their_password(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The edit form takes a password where the entry's locks take one."""
    result = await _edit_user_form(hass, lcm_config_entry, 1)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "slot1", CONF_ENABLED: True, CONF_PIN: "1234", "password": "pw"},
    )

    assert result["type"] == "abort"
    assert get_entry_config(lcm_config_entry).slot(1)["password"] == "pw"


async def test_pasted_users_may_carry_a_password(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The block path takes `password:`."""
    result = await _add_user_form(hass, lcm_config_entry, path="several")

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_USERS: {"Guest": {"password": "welcome in", "enabled": True}}},
    )

    assert result["type"] == "abort"
    assert get_entry_config(lcm_config_entry).users["Guest"]["password"] == (
        "welcome in"
    )


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"password": "café"}, id="password-character"),
        pytest.param({"password": "x" * 256}, id="password-length"),
        pytest.param({CONF_PIN: "1" * 256}, id="pin-length"),
    ],
)
async def test_pasted_users_obey_the_credential_rules(
    hass: HomeAssistant, lcm_config_entry, fields: dict[str, str]
) -> None:
    """The block path refuses what the form refuses."""
    result = await _add_user_form(hass, lcm_config_entry, path="several")

    result = await _subentry_configure(
        hass, result["flow_id"], {CONF_USERS: {"Guest": fields}}
    )

    assert result["errors"] == {"base": "invalid_config"}
    assert "Guest" not in get_entry_config(lcm_config_entry).users


async def test_pasted_users_sharing_a_password_are_refused(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Within the pasted block, per type."""
    result = await _add_user_form(hass, lcm_config_entry, path="several")

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_USERS: {"A": {"password": "same"}, "B": {"password": "same"}}},
    )

    assert result["errors"] == {"base": "password_not_unique"}
    assert result["description_placeholders"] == {"name": "B"}
