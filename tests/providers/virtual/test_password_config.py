"""A password in the configuration, end to end on a Virtual lock."""

import json
from pathlib import Path
from unittest.mock import patch

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
from homeassistant.helpers import (
    entity_registry as er,
    issue_registry as ir,
)

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
from custom_components.lock_code_manager.ha.callbacks import EntityCallbackRegistry
from custom_components.lock_code_manager.ha.entry import available_credential_types
from custom_components.lock_code_manager.ha.runtime import lock_registry
from custom_components.lock_code_manager.model.config import build_slot_unique_id
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.model.exceptions import (
    CodeRejectedError,
)
from custom_components.lock_code_manager.model.queries import (
    get_entry_config,
    subentry_id_for_slot,
)
from custom_components.lock_code_manager.providers.virtual import VirtualLock
from custom_components.lock_code_manager.sync.slot import Status

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


async def test_a_blank_password_is_refused(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Spaces alone are refused, not kept as typed, and nothing is stored."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError, match="Password must not be blank"):
        await coordinator.async_request_credential_update(
            CredentialType.PASSWORD, "   "
        )

    assert "password" not in get_entry_config(lcm_config_entry).slot(1)


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

    with pytest.raises(ServiceValidationError, match="already has this password$"):
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


async def test_reconciling_again_adds_no_second_entity(
    hass: HomeAssistant, lcm_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """Each slot has one password entity, however often the entry reconciles."""
    await lcm_config_entry.runtime_data.callbacks.async_reconcile_entities()
    await hass.async_block_till_done()

    assert "does not generate unique IDs" not in caplog.text
    assert _password_entity(hass, lcm_config_entry, 1) is not None


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


async def test_a_reload_leaves_the_password_entities_to_the_slot_teardown(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """An unload is no lock leaving the entry: the lock-removed handler stands aside."""
    original = EntityCallbackRegistry.async_remove_entity
    with patch.object(
        EntityCallbackRegistry,
        "async_remove_entity",
        autospec=True,
        side_effect=original,
    ) as remove_entity:
        assert await hass.config_entries.async_reload(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

    assert not [
        call for call in remove_entity.call_args_list if "password" in call.args[1]
    ]
    assert _password_entity(hass, lcm_config_entry, 1) is not None


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


def _second_virtual_lock(hass: HomeAssistant, provider_entry: MockConfigEntry) -> str:
    """Register another Virtual lock on the provider's entry; return its id."""
    lock_entity = er.async_get(hass).async_get_or_create(
        "lock", "virtual", "test_virtual_2", config_entry=provider_entry
    )
    hass.states.async_set(lock_entity.entity_id, "locked")
    return lock_entity.entity_id


async def _set_up_entry(hass: HomeAssistant, locks: list[str]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: locks,
            CONF_SLOTS: {
                1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
                2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
            },
        },
        unique_id="test_password_reconcile",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _submit_locks(hass: HomeAssistant, entry, locks: list[str]) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_LOCKS: locks}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()


async def test_removing_every_password_lock_at_once_removes_the_entities(
    hass: HomeAssistant,
    virtual_lock_entity,
    virtual_provider_entry,
    mock_lock_config_entry,
) -> None:
    """Two password locks leaving in one submit: no slot keeps the entity."""
    second = _second_virtual_lock(hass, virtual_provider_entry)
    entry = await _set_up_entry(
        hass, [VIRTUAL_LOCK_ENTITY_ID, second, LOCK_1_ENTITY_ID]
    )
    assert all(
        CredentialType.PASSWORD
        in entry.runtime_data.locks[lock].managed_credential_types
        for lock in (VIRTUAL_LOCK_ENTITY_ID, second)
    )
    await entry.runtime_data.slot_coordinators[1].async_request_credential_update(
        CredentialType.PASSWORD, "pw"
    )
    await hass.async_block_till_done()
    assert all(_password_entity(hass, entry, slot) is not None for slot in (1, 2))

    await _submit_locks(hass, entry, [LOCK_1_ENTITY_ID])

    assert list(entry.runtime_data.locks) == [LOCK_1_ENTITY_ID]
    for slot in (1, 2):
        assert _password_entity(hass, entry, slot) is None
    assert get_entry_config(entry).slot(1)["password"] == "pw"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_swapping_password_locks_in_one_submit_keeps_one_entity_per_slot(
    hass: HomeAssistant,
    virtual_lock_entity,
    virtual_provider_entry,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One password lock out and another in: each slot ends with one live entity."""
    second = _second_virtual_lock(hass, virtual_provider_entry)
    entry = await _set_up_entry(hass, [VIRTUAL_LOCK_ENTITY_ID, LOCK_1_ENTITY_ID])
    before = {slot: _password_entity(hass, entry, slot) for slot in (1, 2)}
    assert all(before.values())

    await _submit_locks(hass, entry, [second, LOCK_1_ENTITY_ID])

    assert set(entry.runtime_data.locks) == {second, LOCK_1_ENTITY_ID}
    callbacks = entry.runtime_data.callbacks
    for slot in (1, 2):
        entity_id = _password_entity(hass, entry, slot)
        assert entity_id is not None
        state = hass.states.get(entity_id)
        assert state is not None
        assert state.attributes["mode"] == "password"
        assert f"{slot}|password" in callbacks.remove_entity
    assert "does not generate unique IDs" not in caplog.text
    assert "already exists" not in caplog.text

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


async def test_clearing_a_password_the_user_does_not_hold_writes_nothing(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Nothing to clear: the user's stored data is left as it was."""
    subentry_id = subentry_id_for_slot(lcm_config_entry, 1)
    before = dict(lcm_config_entry.subentries[subentry_id].data)
    assert "password" not in before

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

    assert dict(lcm_config_entry.subentries[subentry_id].data) == before


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


async def test_add_user_takes_a_password_in_the_flat_shape(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The top-level shape carries a password as it carries a PIN."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_ADD_USER,
        {
            CONF_NAME: "Guest",
            "password": "welcome in",
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
    with pytest.raises(ServiceValidationError, match="cannot have the password "):
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


async def test_add_user_names_the_pin_when_both_credentials_are_taken(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Credentials are checked in one fixed order, PIN first, every run."""
    with pytest.raises(ServiceValidationError, match="cannot have the PIN 'A'"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "users": [
                    {CONF_NAME: "A", CONF_PIN: "9999", "password": "same"},
                    {CONF_NAME: "B", CONF_PIN: "9999", "password": "same"},
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


async def test_a_blank_password_is_refused_in_the_form(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Spaces alone are refused on the password field."""
    result = await _add_user_form(hass, lcm_config_entry)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "Guest", CONF_ENABLED: True, "password": "   "},
    )

    assert result["errors"] == {"password": "invalid_password"}


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

    assert result["errors"] == {"base": "password_not_unique_in_block"}
    assert result["description_placeholders"] == {"name": "B"}


async def test_pasted_users_sharing_both_credentials_are_told_the_pin(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Credentials are checked in one fixed order, PIN first, every run."""
    result = await _add_user_form(hass, lcm_config_entry, path="several")

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {
            CONF_USERS: {
                "A": {CONF_PIN: "9999", "password": "same"},
                "B": {CONF_PIN: "9999", "password": "same"},
            }
        },
    )

    assert result["errors"] == {"base": "pin_not_unique_in_block"}
    assert result["description_placeholders"] == {"name": "B"}


async def test_the_password_field_follows_the_pin(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The two credentials sit together in the form."""
    result = await _add_user_form(hass, lcm_config_entry)

    assert [str(key) for key in result["data_schema"].schema] == [
        CONF_NAME,
        CONF_PIN,
        "password",
        CONF_ENABLED,
        "condition",
    ]


async def _set_slot1_password(hass: HomeAssistant, entry, value: str) -> None:
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        {
            CONF_NAME: "slot1",
            ATTR_CREDENTIAL_TYPE: "password",
            ATTR_CREDENTIAL_VALUE: value,
            "config_entry_id": entry.entry_id,
        },
        blocking=True,
    )


async def test_emptying_an_offered_password_in_the_edit_form_removes_it(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """The form showed the password, so leaving it empty clears it."""
    await _set_slot1_password(hass, lcm_config_entry, "pw")
    result = await _edit_user_form(hass, lcm_config_entry, 1)

    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "slot1", CONF_ENABLED: True, CONF_PIN: "1234"},
    )

    assert result["type"] == "abort"
    assert not get_entry_config(lcm_config_entry).slot(1).get("password")


async def test_emptying_every_credential_of_an_enabled_user_is_refused(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Neither the PIN nor the password left: the edit is refused."""
    await _set_slot1_password(hass, lcm_config_entry, "pw")
    result = await _edit_user_form(hass, lcm_config_entry, 1)

    result = await _subentry_configure(
        hass, result["flow_id"], {CONF_NAME: "slot1", CONF_ENABLED: True}
    )

    assert result["errors"] == {"base": "missing_credential_if_enabled"}
    assert get_entry_config(lcm_config_entry).slot(1)["password"] == "pw"


def _refuse_passwords(real):
    async def _set(self, code_slot, credential_type, value, **kwargs):
        if credential_type is CredentialType.PASSWORD:
            raise CodeRejectedError(code_slot, self.lock.entity_id, "too simple")
        return await real(self, code_slot, credential_type, value, **kwargs)

    return _set


def _disabled_issue(hass: HomeAssistant, entry, slot: int = 1) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_disabled_{entry.entry_id}_{slot}"
    )


async def test_a_refused_password_turns_the_user_off_beside_a_working_pin(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Any refused value turns the user off, whatever its type (decision 11)."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    worker = lock_registry(hass).worker(VIRTUAL_LOCK_ENTITY_ID)
    assert worker is not None
    await _settled(hass)
    assert worker.status(pin_address(1)) == (Status.IN_SYNC, None)
    with patch.object(
        VirtualLock,
        "async_set_slot_credential",
        _refuse_passwords(VirtualLock.async_set_slot_credential),
    ):
        await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
        await _settled(hass)

    assert coordinator.is_enabled is False
    issue = _disabled_issue(hass, lcm_config_entry)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert "refused the password for slot" in issue.translation_placeholders["reason"]
    assert "too simple" in issue.translation_placeholders["reason"]


async def test_a_refused_only_credential_turns_the_slot_off(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """A password-only user whose password is refused is turned off."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    assert coordinator.is_enabled is False
    with patch.object(
        VirtualLock,
        "async_set_slot_credential",
        _refuse_passwords(VirtualLock.async_set_slot_credential),
    ):
        await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
        await coordinator.async_request_active_toggle(True)
        assert coordinator.is_enabled is True
        await _settled(hass)

    assert coordinator.is_enabled is False
    issue = _disabled_issue(hass, lcm_config_entry)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert "refused the password for slot" in issue.translation_placeholders["reason"]


async def test_a_new_user_with_a_refused_password_is_turned_off(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """
    A new user's PIN and refused password: off, as an existing user would be.

    The worker writes a new slot's password before its PIN, so the refusal
    arrives before the PIN is on the lock.
    """
    with patch.object(
        VirtualLock,
        "async_set_slot_credential",
        _refuse_passwords(VirtualLock.async_set_slot_credential),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADD_USER,
            {
                "users": [{CONF_NAME: "Guest", CONF_PIN: "2468", "password": "pw"}],
                "config_entry_id": lcm_config_entry.entry_id,
            },
            blocking=True,
        )
        await _settled(hass)

    slot = get_entry_config(lcm_config_entry).assignment.slot("Guest")
    assert slot is not None
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[slot]
    assert coordinator.is_enabled is False
    issue = _disabled_issue(hass, lcm_config_entry, slot)
    assert issue is not None
    assert issue.translation_placeholders is not None
    assert "refused the password for slot" in issue.translation_placeholders["reason"]


async def test_slot_disabled_is_answered_once_the_slot_is_in_sync(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """
    A refusal's repair on a slot that stays on is answered by sync alone.

    Raised with no coordinator to turn the slot off, or persisted by an
    earlier release: no re-enable is coming to answer it.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"slot_disabled_{lcm_config_entry.entry_id}_1",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="slot_disabled",
        translation_placeholders={"slot_num": "1", "reason": "x"},
    )
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "pw")
    await _settled(hass)

    assert coordinator.is_enabled is True
    assert get_virtual_lock(hass, lcm_config_entry)._data["1"]["password"] == "pw"
    assert _disabled_issue(hass, lcm_config_entry) is None


def _refuse_pin(pin: str):
    real = VirtualLock.async_set_slot_credential

    async def _set(self, code_slot, credential_type, value, **kwargs):
        if credential_type is CredentialType.PIN and value == pin:
            raise CodeRejectedError(code_slot, self.lock.entity_id, "too simple")
        return await real(self, code_slot, credential_type, value, **kwargs)

    return _set


async def test_re_enabling_answers_slot_disabled(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """
    The repair is answered when the user turns the slot back on.

    Refused again, the same PIN raises a fresh repair the usual way.
    """
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    with patch.object(
        VirtualLock,
        "async_set_slot_credential",
        _refuse_pin("2468"),
    ):
        await coordinator.async_request_credential_update(CredentialType.PIN, "2468")
        await _settled(hass)
        assert coordinator.is_enabled is False
        assert _disabled_issue(hass, lcm_config_entry) is not None

        await coordinator.async_request_active_toggle(True)
        assert coordinator.is_enabled is True
        assert _disabled_issue(hass, lcm_config_entry) is None

        await _settled(hass)

    assert coordinator.is_enabled is False
    assert _disabled_issue(hass, lcm_config_entry) is not None


async def test_re_enabling_in_the_edit_form_answers_slot_disabled(
    hass: HomeAssistant, lcm_config_entry
) -> None:
    """Every route back on answers the refusal, the edit form's included."""
    coordinator = lcm_config_entry.runtime_data.slot_coordinators[1]
    with patch.object(
        VirtualLock,
        "async_set_slot_credential",
        _refuse_pin("2468"),
    ):
        await coordinator.async_request_credential_update(CredentialType.PIN, "2468")
        await _settled(hass)
    assert coordinator.is_enabled is False
    assert _disabled_issue(hass, lcm_config_entry) is not None

    result = await _edit_user_form(hass, lcm_config_entry, 1)
    result = await _subentry_configure(
        hass,
        result["flow_id"],
        {CONF_NAME: "slot1", CONF_ENABLED: True, CONF_PIN: "2468"},
    )
    assert result["type"] == "abort"

    assert coordinator.is_enabled is True
    assert _disabled_issue(hass, lcm_config_entry) is None
    await _settled(hass)
    assert get_virtual_lock(hass, lcm_config_entry)._data["1"]["code"] == "2468"
    assert _disabled_issue(hass, lcm_config_entry) is None
