"""Config flow tests."""

import json
import logging
from pathlib import Path
import re
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, LockState
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.config_flow import _check_common_slots
from custom_components.lock_code_manager.const import (
    CONF_CODELESS,
    CONF_LOCKS,
    CONF_MEMBERS,
    CONF_NUM_USERS,
    CONF_USERS,
    DOMAIN,
    EXCLUDED_CONDITION_PLATFORMS,
    MAX_SEARCHED_SLOT,
)
from custom_components.lock_code_manager.domain.allocation import (
    LockQuerySkipped,
    build_lock_instance,
)
from custom_components.lock_code_manager.domain.config import EntryConfig
from custom_components.lock_code_manager.domain.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.locks import (
    resolve_member_provider_class,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)
from custom_components.lock_code_manager.providers.codeless import CodelessLock

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    MockLCMLock,
    async_discover_unclaimed_mqtt_lock,
    reading_for,
    register_codeless_lock,
)
from .providers.zigbee2mqtt.conftest import async_discover_z2m_lock

GET_ALL_CODES_PATCH = (
    "custom_components.lock_code_manager.config_flow._async_query_locks"
)


def _holding(*occupied: int):
    """Patch the lock read so it answers with exactly these slots occupied.

    The scope is honoured, the way a per-index provider honours it, so a test
    that widens the window sees the wider answer.
    """

    async def _read(self, slots=None):
        scope = self.managed_slots if slots is None else slots
        return {
            slot: (
                SlotCredential.known("0000")
                if slot in occupied
                else SlotCredential.empty()
            )
            for slot in scope
        }

    return patch.object(MockLCMLock, "async_get_usercodes", _read)


@pytest.fixture(name="bypass_entry_setup_and_unload", autouse=True)
def bypass_entry_setup_and_unload_fixture():
    """Bypass config entry setup."""
    with (
        patch(
            "custom_components.lock_code_manager.async_setup_entry", return_value=True
        ),
        patch(
            "custom_components.lock_code_manager.async_unload_entry", return_value=True
        ),
    ):
        yield


async def _start_config_flow(hass: HomeAssistant):
    """Start a config flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"

    return flow_id


async def _start_ui_config_flow(hass: HomeAssistant):
    """Start a UI based config flow."""
    flow_id = await _start_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "ui"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_USERS: 4}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert not result["last_step"]
    assert result["description_placeholders"]["user_num"] == 1

    return flow_id


async def _start_yaml_config_flow(hass: HomeAssistant):
    """Start a YAML based config flow.

    The yaml path allocates numbers now, so it reads the locks just as the
    guided path does; tests that submit users wrap the call in ``_holding``.
    """
    flow_id = await _start_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"

    return flow_id


async def _init_flow_to_user_step(hass: HomeAssistant) -> str:
    """Initialize a config flow and return the flow ID at the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    return result["flow_id"]


def _assert_fields_are_labelled(result, category: str) -> None:
    """
    Assert a form's fields and its labels name the same set of keys.

    ``ha-form`` falls back to the raw key when a field has no label, so a
    field the strings do not name reaches the user as ``condition`` rather
    than "Condition Entity". Comparing as sets catches the other half of the
    same mistake: a label still filed under the key a rename left behind
    names nothing, and is the only trace that the rename was half-done.

    The schema comes off the form the flow actually returned rather than
    from an import, so a step that builds its schema inline is covered the
    same as one that reuses a module constant.
    """
    strings = json.loads(
        Path("custom_components/lock_code_manager/strings.json").read_text()
    )
    step_id = result["step_id"]
    labelled = set(strings[category]["step"][step_id].get("data", {}))
    fields = {str(key.schema) for key in result["data_schema"].schema}
    assert fields == labelled, (
        f"{category} step {step_id}: fields {sorted(fields)} "
        f"but labels {sorted(labelled)}"
    )


async def test_every_config_flow_field_has_a_label(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Every field the setup flow shows is named by the strings."""
    flow_id = await _init_flow_to_user_step(hass)
    result = await hass.config_entries.flow.async_configure(flow_id)
    _assert_fields_are_labelled(result, "config")

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    _assert_fields_are_labelled(result, "config")

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_USERS: 1}
    )
    _assert_fields_are_labelled(result, "config")

    # The yaml path is the ui path's sibling, so reaching it takes a second
    # flow -- under a second name, because the first one holds the unique id.
    flow_id = await _init_flow_to_user_step(hass)
    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test2", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    _assert_fields_are_labelled(result, "config")


async def test_every_options_flow_field_has_a_label(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Every field the options flow shows is named by the strings."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    _assert_fields_are_labelled(result, "options")


async def test_every_reauth_field_has_a_label(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Every field the reauth flow shows is named by the strings."""
    lock_code_manager_config_entry.async_start_reauth(
        hass, context={"lock_entity_id": LOCK_1_ENTITY_ID}
    )
    await hass.async_block_till_done()
    [flow] = lock_code_manager_config_entry.async_get_active_flows(
        hass, {SOURCE_REAUTH}
    )
    # The in-progress flow carries no schema, so render the form to get one.
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    _assert_fields_are_labelled(result, "config")


async def test_config_flow_ui(hass: HomeAssistant, mock_lock_config_entry):
    """Test UI based config flow with slot number incrementing correctly."""
    flow_id = await _start_ui_config_flow(hass)

    pins = ["1234", "5678", "9012", "3456"]
    for i, pin in enumerate(pins):
        slot_num = i + 1
        is_last = slot_num == len(pins)

        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_NAME: f"User {slot_num}", CONF_ENABLED: True, CONF_PIN: pin},
        )

        if is_last:
            assert result["type"] == "create_entry"
        else:
            assert result["type"] == "form"
            assert result["step_id"] == "code_slot"
            assert result["last_step"] == (slot_num == len(pins) - 1)
            assert result["description_placeholders"]["user_num"] == slot_num + 1

    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_USERS: {
            "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
            "User 2": {CONF_ENABLED: True, CONF_PIN: "5678"},
            "User 3": {CONF_ENABLED: True, CONF_PIN: "9012"},
            "User 4": {CONF_ENABLED: True, CONF_PIN: "3456"},
        },
        # Numbers the user never chose: the lowest free on the lock, which
        # already holds codes at 1 and 2.
        CONF_SLOT_ASSIGNMENT: {
            "user 1": 3,
            "user 2": 4,
            "user 3": 5,
            "user 4": 6,
        },
    }


async def test_config_flow_ui_error(hass: HomeAssistant, mock_lock_config_entry):
    """Test error in UI based config flow."""
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: ""}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert not result["last_step"]
    assert result["errors"] == {CONF_PIN: "missing_pin_if_enabled"}


async def test_config_flow_yaml(hass: HomeAssistant, mock_lock_config_entry):
    """Test YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_USERS: {
                    "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
                    "User 2": {CONF_ENABLED: True, CONF_PIN: "5678"},
                }
            },
        )

    assert result["type"] == "create_entry"
    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_USERS: {
            "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
            "User 2": {CONF_ENABLED: True, CONF_PIN: "5678"},
        },
        # Allocated on the way out, because nobody picked them.
        CONF_SLOT_ASSIGNMENT: {"user 1": 1, "user 2": 2},
    }


async def test_config_flow_yaml_error(hass: HomeAssistant, mock_lock_config_entry):
    """Test error handling in YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: ""}}},
        )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"
    assert result["errors"] == {"base": "invalid_config"}


async def test_config_flow_ui_stores_a_padded_pin_stripped(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    A PIN typed with surrounding whitespace is persisted without it.

    A submitted code is stripped before it is matched, so padding kept on
    the stored side makes a credential nothing anybody can type will ever
    match -- and the guided form is where a stray space gets typed.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_USERS: 1}
    )
    assert result["step_id"] == "code_slot"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: " 1234 "}
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_USERS] == {
        "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}
    }


async def test_config_flow_ui_rejects_a_whitespace_only_pin(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    An enabled user whose PIN is only whitespace is refused, not stored.

    Stripping happens before the enabled-needs-a-PIN check, so a PIN that
    is empty once stripped fails that check rather than being written as a
    credential no keypad can produce.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 1})

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "   "}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert result["errors"] == {CONF_PIN: "missing_pin_if_enabled"}


async def test_config_flow_yaml_stores_a_padded_pin_stripped(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A pasted block's padded PIN is persisted stripped, as the form's is."""
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: " 1234 "}}},
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_USERS] == {
        "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}
    }


async def test_config_flow_yaml_rejects_a_whitespace_only_pin(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A pasted block's whitespace-only PIN on an enabled user is refused."""
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "   "}}},
        )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"
    assert result["errors"] == {"base": "invalid_config"}


async def test_options_flow(hass: HomeAssistant, mock_lock_config_entry):
    """Test options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    # The options flow now rejects the deprecated number_of_uses key, so build
    # the user-submitted config from scratch without it. Real entries have it
    # auto-stripped during migration before any options-flow interaction.
    new_config = {
        CONF_LOCKS: list(BASE_CONFIG[CONF_LOCKS]),
        CONF_USERS: {
            "test1": {CONF_PIN: "1234", CONF_ENABLED: True},
            "test2": {
                CONF_PIN: "5678",
                CONF_ENABLED: True,
                CONF_CONDITION: "calendar.test_1",
            },
            "User 3": {CONF_ENABLED: True, CONF_PIN: ""},
        },
    }
    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    flow_id = result["flow_id"]

    result = await hass.config_entries.options.async_configure(
        flow_id, user_input=new_config
    )

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "invalid_config"}

    # Give the enabled user a PIN and it saves.
    new_config[CONF_USERS]["User 3"][CONF_PIN] = "1234"
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input=new_config
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_USERS] == new_config[CONF_USERS]
    # Numbers are issued here, not submitted.
    assert set(result["data"][CONF_SLOT_ASSIGNMENT]) == {"test1", "test2", "user 3"}


async def test_config_flow_reauth(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test reauth flow for updating locks."""
    lock_code_manager_config_entry.async_start_reauth(
        hass, context={"lock_entity_id": LOCK_1_ENTITY_ID}
    )
    await hass.async_block_till_done()
    flows = list(
        lock_code_manager_config_entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    )
    assert len(flows) == 1
    [result] = flows

    assert result["step_id"] == "reauth_confirm"
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"


async def test_config_flow_reauth_form_refetch(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """The reauth form renders when re-fetched with no user input.

    The frontend re-invokes the current step with ``user_input=None`` to
    render the form when the user opens the flow, so the step must handle
    ``None`` and seed defaults from the entry's configured locks.
    """
    lock_code_manager_config_entry.async_start_reauth(
        hass, context={"lock_entity_id": LOCK_1_ENTITY_ID}
    )
    await hass.async_block_till_done()
    [flow] = lock_code_manager_config_entry.async_get_active_flows(
        hass, {SOURCE_REAUTH}
    )

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], None)

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert result["data_schema"]({})[CONF_LOCKS] == [
        LOCK_1_ENTITY_ID,
        LOCK_2_ENTITY_ID,
    ]


async def test_reauth_wins_over_stale_options(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """A reauth fix is not reverted by stale options saved while the entry was failed.

    While an entry is failed (e.g. a configured lock entity vanished after a
    Z-Wave exclusion) no update listener is registered, so an options-flow
    save just sits in ``options``. The data→options migration merges
    options-preferred, so a reauth that only wrote into ``data`` would lose
    to the stale options on the next load. The reauth write must therefore
    consume the pending options (options-preferred, its own input winning)
    and clear them.
    """
    entry = lock_code_manager_config_entry
    entry.async_start_reauth(hass, context={"lock_entity_id": LOCK_1_ENTITY_ID})
    await hass.async_block_till_done()

    # An options-flow save the failed entry could never process.
    stale_options = {**BASE_CONFIG, CONF_LOCKS: [LOCK_2_ENTITY_ID]}
    hass.config_entries.async_update_entry(entry, options=stale_options)

    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )
    assert result["reason"] == "locks_updated"
    await hass.async_block_till_done()

    # The reauth's lock list won and the stale options were consumed, so the
    # next data→options migration has nothing stale to prefer.
    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]
    assert not entry.options


async def test_config_flow_slots_already_configured(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Another entry's numbers are avoided, not reported as a conflict.

    Picking a number another entry manages used to be something the user
    could do and be told off for. Now allocation simply does not issue one.
    """
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 2": {CONF_ENABLED: False, CONF_PIN: "0123"}}},
        )

    assert result["type"] == "create_entry"
    taken = set(get_entry_config(lock_code_manager_config_entry).slot_numbers)
    assert not set(result["data"][CONF_SLOT_ASSIGNMENT].values()) & taken


async def test_config_flow_two_entries_same_locks(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test two entries that use same locks but different slots set up successfully."""
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 3": {CONF_ENABLED: False, CONF_PIN: "0123"}}},
        )
    assert result["type"] == "create_entry"


async def test_config_flow_ui_scheduler_entity_excluded(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Test that scheduler-component entities are rejected during config flow."""
    # Create a mock scheduler entity in registry
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "switch",
        "scheduler",  # platform
        "test_schedule",
        suggested_object_id="my_schedule",
    )
    hass.states.async_set("switch.my_schedule", "on")
    await hass.async_block_till_done()

    flow_id = await _start_ui_config_flow(hass)

    # Try to configure slot 1 with a scheduler entity as condition
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "User 1",
            CONF_ENABLED: True,
            CONF_PIN: "1234",
            CONF_CONDITION: "switch.my_schedule",
        },
    )

    # Should show error for excluded platform
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert result["errors"] == {CONF_CONDITION: "excluded_platform"}
    # Verify placeholder is set for the error message
    assert result["description_placeholders"].get("integration") == "scheduler"


# --- Existing-codes confirmation step tests ---


async def test_ui_setup_allocates_around_existing_codes(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Setup skips slots that already hold a code instead of asking about them.

    There is no prompt because there is nothing to overwrite: the numbers are
    chosen, not requested, so an occupied one is simply not offered. The
    confirmation the flow used to show existed because the user picked a range
    and might have picked one already in use.
    """
    flow_id = await _init_flow_to_user_step(hass)
    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_USERS: 2}
    )

    # Straight to the first user, with no confirmation in between.
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"

    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1111"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Alice", CONF_ENABLED: True, CONF_PIN: "2222"}
    )

    assert result["type"] == "create_entry"
    # Slots 1 and 2 are taken on the lock, so the users get 3 and 4. Which of
    # them gets which does not follow the order they were entered: numbering
    # is by name, so a caller passing an unordered collection cannot hand the
    # same configuration different credential indices from one run to the next.
    assert result["data"][CONF_SLOT_ASSIGNMENT] == {"alice": 3, "raman": 4}


async def test_ui_setup_refuses_when_a_lock_cannot_be_read(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A lock that did not answer makes occupancy unknown, and setup stops.

    Unreadable is not free. Issuing a number here could put a user's code
    over a credential programmed by hand on a lock that was merely
    unreachable.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch.object(
        MockLCMLock,
        "async_get_usercodes",
        AsyncMock(side_effect=LockDisconnected("asleep")),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    # Refused before a single name or PIN was collected.
    # A form, not an abort: a lock that is merely asleep must not end setup.
    assert result["type"] == "form"
    assert result["step_id"] == "ui"
    assert result["errors"] == {"base": "occupancy_unknown"}
    assert LOCK_1_ENTITY_ID in result["description_placeholders"]["locks"]


# --- _async_get_all_codes tests ---


# --- Options flow tests ---


async def _start_options_flow(
    hass: HomeAssistant,
    *,
    locks: list[str] | None = None,
    users: dict[str, dict] | None = None,
) -> tuple[str, MockConfigEntry]:
    """
    Create an options flow and return (flow_id, entry).

    Mirrors the existing config-flow test helpers: keeps the entry creation
    and options-flow init in one place so the individual tests stay focused
    on the behavior being checked.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: locks or [LOCK_1_ENTITY_ID],
            CONF_USERS: users or {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    return result["flow_id"], entry


async def test_options_flow_no_added_pairs_persists_immediately(
    hass: HomeAssistant, mock_lock_config_entry
):
    """No new (lock, slot) pairs -> skip scan and confirm step entirely."""
    flow_id, _ = await _start_options_flow(hass)

    # Submit the same locks/slots that already exist on the entry — no diff
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            },
        )

    assert result["type"] == "create_entry"


async def test_options_flow_added_pair_no_existing_code_persists(
    hass: HomeAssistant, mock_lock_config_entry
):
    """New (lock, slot) added but lock has no code there -> persist directly."""
    flow_id, _ = await _start_options_flow(hass)

    # Adding slot 2; lock has nothing in slot 2 (only slot 1)
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {
                    "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
                    "User 2": {CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "create_entry"


async def test_options_flow_added_pair_empty_code_persists(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Lock reports the new slot as EMPTY -> no confirm needed, persist."""
    flow_id, _ = await _start_options_flow(hass)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {
                    "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
                    "User 2": {CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "create_entry"


async def test_options_flow_invalid_yaml_shows_error(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Validation error in the YAML keeps the form open with the error."""
    flow_id, _ = await _start_options_flow(hass)

    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            # Enabled with no PIN is invalid per schema.
            CONF_USERS: {"Raman": {CONF_ENABLED: True}},
        },
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_config"}


# Slot capacity validation (issue #1398)


def _capacity_probe(**capabilities_mock_kwargs):
    """
    Decide what the test lock answers when the flow probes its capacity.

    Allocation builds a throwaway provider instance to ask the lock how many
    slots it has; ``auto_setup_mock_lock`` is what makes the test platform
    resolve to MockLCMLock in the first place.
    """
    return patch.object(
        MockLCMLock,
        "async_get_capabilities",
        new_callable=AsyncMock,
        **capabilities_mock_kwargs,
    )


def _capabilities_with_slots(num_slots: int) -> LockCapabilities:
    """Build PIN-capable capabilities advertising ``num_slots`` slots."""
    return LockCapabilities(
        supports_user_management=False,
        max_users=num_slots,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=num_slots,
                min_length=4,
                max_length=8,
                supports_learn=False,
            )
        },
    )


async def test_config_flow_yaml_rejects_slot_beyond_lock_capacity(
    hass: HomeAssistant, mock_lock_config_entry
):
    """More users than the lock can hold is caught before the entry exists."""
    flow_id = await _start_yaml_config_flow(hass)

    with _capacity_probe(return_value=_capabilities_with_slots(30)):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_USERS: {
                    f"User {n}": {CONF_ENABLED: True, CONF_PIN: "2222"}
                    for n in range(1, 32)
                }
            },
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "too_many_users"}
    assert result["description_placeholders"]["num_users"] == "31"
    assert result["description_placeholders"]["num_slots"] == "30"


async def test_config_flow_yaml_accepts_slot_within_capacity(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A slot inside the advertised range still creates the entry."""
    flow_id = await _start_yaml_config_flow(hass)

    with _capacity_probe(return_value=_capabilities_with_slots(30)):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 30": {CONF_ENABLED: True, CONF_PIN: "2222"}}},
        )

    assert result["type"] == "create_entry"


async def test_setup_refuses_when_a_lock_has_no_provider(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A lock we cannot build a provider for is unread, not empty.

    Lock Code Manager will still write to it once the entry loads, so
    allocation must not issue numbers it was never able to check against it.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch(
        "custom_components.lock_code_manager.domain.allocation.build_lock_instance",
        side_effect=LockQuerySkipped(LOCK_1_ENTITY_ID, managed=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    # A form, not an abort: a lock that is merely asleep must not end setup.
    assert result["type"] == "form"
    assert result["step_id"] == "ui"
    assert result["errors"] == {"base": "occupancy_unknown"}
    assert LOCK_1_ENTITY_ID in result["description_placeholders"]["locks"]


async def test_setup_ignores_a_lock_on_an_unsupported_platform(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A lock this integration will not write to cannot constrain numbering."""
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch(
        "custom_components.lock_code_manager.domain.allocation.build_lock_instance",
        side_effect=LockQuerySkipped(LOCK_1_ENTITY_ID, managed=False),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_setup_reads_the_locks_for_no_entry_at_all(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    The flow that creates an entry has none yet, and its reads say so.

    What allocation makes of a lock -- above all whether one it cannot build
    a provider for is still one credentials get written to -- is settled by
    the owning entry's configuration, so every read has to carry the entry it
    is made for. Setup is the one case where there is honestly nobody to
    name.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with reading_for() as read_for:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["step_id"] == "code_slot"
    assert read_for, "allocation issued numbers without reading a lock"
    assert all(entry is None for entry in read_for)


async def test_editing_reads_the_locks_for_the_entry_being_edited(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The editor names the entry whose locks it is reading."""
    flow_id, entry = await _start_options_flow(hass)

    with reading_for() as read_for:
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {
                    "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
                    "Newcomer": {CONF_ENABLED: True, CONF_PIN: "9999"},
                },
            },
        )

    assert result["type"] == "create_entry"
    assert read_for, "the editor allocated numbers without reading a lock"
    assert all(read is entry for read in read_for)


async def test_reauth_reads_the_locks_for_the_entry_it_is_repairing(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Swapping a lock is still a read the entry being repaired asked for."""
    lock_code_manager_config_entry.async_start_reauth(
        hass, context={"lock_entity_id": LOCK_1_ENTITY_ID}
    )
    await hass.async_block_till_done()
    [flow] = lock_code_manager_config_entry.async_get_active_flows(
        hass, {SOURCE_REAUTH}
    )

    with reading_for() as read_for:
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
        )

    assert result["reason"] == "locks_updated"
    assert read_for, "reauth accepted the new locks without reading them"
    assert all(read is lock_code_manager_config_entry for read in read_for)


async def test_setup_reads_only_as_far_as_it_has_to(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The window starts at the number of users and widens by what is in the way.

    Locks that answer one index per round trip make the width of this read
    its cost, so a lock is never walked to its advertised capacity to place a
    handful of users.
    """
    windows: list[list[int]] = []

    async def _read(self, slots=None):
        scope = sorted(self.managed_slots if slots is None else slots)
        windows.append(scope)
        return {
            slot: (
                SlotCredential.known("0000")
                if slot in (1, 2, 3)
                else SlotCredential.empty()
            )
            for slot in scope
        }

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch.object(MockLCMLock, "async_get_usercodes", _read):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )
        for name in ("Raman", "Alice"):
            result = await hass.config_entries.flow.async_configure(
                flow_id, {CONF_NAME: name, CONF_ENABLED: True, CONF_PIN: "1111"}
            )

    assert result["type"] == "create_entry"
    # Slots 1-3 are taken, so the two users land on 4 and 5.
    assert result["data"][CONF_SLOT_ASSIGNMENT] == {"alice": 4, "raman": 5}
    # Every number issued was one that was actually read -- not merely one
    # below the highest read, which would let a gap through if the read ever
    # stops being contiguous.
    assert set(result["data"][CONF_SLOT_ASSIGNMENT].values()) <= {
        slot for window in windows for slot in window
    }
    # Asked about 1-2, then only 3-4, then only 5: every index once, and
    # never about the lock's whole capacity.
    assert windows == [[1, 2], [3, 4], [5]]


async def test_setup_does_not_widen_when_nothing_is_in_the_way(
    hass: HomeAssistant, mock_lock_config_entry
):
    """An empty lock costs exactly one read of exactly the users asked for."""
    windows: list[list[int]] = []

    async def _read(self, slots=None):
        scope = sorted(self.managed_slots if slots is None else slots)
        windows.append(scope)
        return dict.fromkeys(scope, SlotCredential.empty())

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch.object(MockLCMLock, "async_get_usercodes", _read):
        await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 3})

    assert windows == [[1, 2, 3]]


async def test_config_flow_ui_rejects_more_users_than_the_lock_holds(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A count the locks cannot hold is refused where it can still be changed.

    Which users get configured does not change which numbers allocation
    issues, so the answer is known as soon as the count is.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(2))
    with probe_capabilities, _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 3}
        )

    # A form, not an abort: the user can lower the count and carry on.
    assert result["type"] == "form"
    assert result["step_id"] == "ui"
    assert result["errors"] == {"base": "too_many_users"}
    assert result["description_placeholders"]["num_users"] == "3"
    assert result["description_placeholders"]["num_slots"] == "2"

    with probe_capabilities, _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_a_refused_count_reports_only_what_it_can_stand_behind(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The refusal states the capacity and the count, and promises no maximum.

    It cannot promise one: occupancy is known only as far as the window that
    was read, so any "you may have N" computed from the lock's full capacity
    can be too high -- and the user who follows it is refused again.
    """
    strings = json.loads(
        Path("custom_components/lock_code_manager/strings.json").read_text()
    )
    message = strings["config"]["error"]["numbers_needed_exceed_capacity"]

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # Nine of the ten slots are taken, well past the window the count asks for.
    with (
        _capacity_probe(return_value=_capabilities_with_slots(10)),
        _holding(*range(1, 10)),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 8}
        )

    assert result["errors"] == {"base": "numbers_needed_exceed_capacity"}
    supplied = result["description_placeholders"]
    assert not {
        name for name in re.findall(r"\{(\w+)\}", message) if name not in supplied
    }
    # No count is offered, so there is none to be wrong about.
    assert "room" not in message and "room" not in supplied


async def test_config_flow_capacity_check_skipped_when_lock_unreachable(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    An unreachable lock must not block configuration.

    Capabilities need the lock awake, so a sleeping battery lock would
    otherwise make the flow unusable. The write-time check still covers it.
    """
    flow_id = await _start_yaml_config_flow(hass)

    with _capacity_probe(side_effect=LockDisconnected("lock asleep")):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 50": {CONF_ENABLED: True, CONF_PIN: "2222"}}},
        )

    assert result["type"] == "create_entry"


async def test_config_flow_capacity_check_skipped_when_capacity_unknown(
    hass: HomeAssistant, mock_lock_config_entry
):
    """``num_slots`` of 0 is "unknown", not "no slots", so it cannot reject."""
    flow_id = await _start_yaml_config_flow(hass)

    with _capacity_probe(return_value=_capabilities_with_slots(0)):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 50": {CONF_ENABLED: True, CONF_PIN: "2222"}}},
        )

    assert result["type"] == "create_entry"


async def test_config_flow_capacity_check_skipped_when_lock_allocates_index(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    A provider that allocates its own credential index is not slot-bounded.

    Matter picks the next free index, so a high slot number is legal there and
    the flow must not query capabilities or reject it.
    """
    flow_id = await _start_yaml_config_flow(hass)

    capabilities = AsyncMock(return_value=_capabilities_with_slots(30))
    with (
        patch.object(
            MockLCMLock,
            "credential_index_follows_slot",
            new_callable=PropertyMock,
            return_value=False,
        ),
        patch.object(MockLCMLock, "async_get_capabilities", capabilities),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 50": {CONF_ENABLED: True, CONF_PIN: "2222"}}},
        )

    assert result["type"] == "create_entry"
    # Bailing before the read is the point: the answer would be irrelevant.
    capabilities.assert_not_called()


async def test_config_flow_capacity_check_survives_unexpected_error(
    hass: HomeAssistant, mock_lock_config_entry, caplog: pytest.LogCaptureFixture
):
    """
    An unexpected provider error must not take the config flow down with it.

    Validation is advisory; a provider raising something outside the Lock Code
    Manager hierarchy should degrade to "not checked", not a broken flow.
    """
    flow_id = await _start_yaml_config_flow(hass)

    with _capacity_probe(side_effect=RuntimeError("provider blew up")):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"User 50": {CONF_ENABLED: True, CONF_PIN: "2222"}}},
        )

    assert result["type"] == "create_entry"
    assert "provider blew up" in caplog.text


async def test_config_flow_ui_accepts_a_name_containing_a_pipe(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A "|" in a name is ordinary text now, not a rejected character.

    It was rejected only while entity and device identifiers were keyed by the
    name and delimited by "|". They are keyed by the slot number, so nothing
    parses a name any more and the restriction had no reason left.
    """
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Ra|man", CONF_ENABLED: True, CONF_PIN: "1234"}
    )

    assert result["type"] == "form"
    assert not result["errors"]


async def test_config_flow_ui_rejects_a_missing_name(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A slot must be named, because the name is becoming the identity.

    Now the ONLY rule left in this branch: the separator rule that used to
    share it is gone, and removing it left the error path with no test at all.
    """
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "   ", CONF_ENABLED: True, CONF_PIN: "1234"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_NAME: "name_required"}


async def test_config_flow_ui_rejects_duplicate_name(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Two slots in one entry cannot share a name, ignoring case."""
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1234"}
    )
    assert result["type"] == "form"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "  raman  ", CONF_ENABLED: True, CONF_PIN: "5678"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_NAME: "name_not_unique"}


@pytest.mark.parametrize(
    ("users", "expected_error"),
    [
        (
            # Two keys, one person: the editor cannot represent a duplicate
            # name, but it can still represent two spellings of one.
            {
                "Raman": {CONF_ENABLED: True, CONF_PIN: "1234"},
                " raman ": {CONF_ENABLED: True, CONF_PIN: "5678"},
            },
            "name_not_unique",
        ),
        (
            {"   ": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            "name_required",
        ),
    ],
)
async def test_config_flow_yaml_enforces_name_rules(
    hass: HomeAssistant, mock_lock_config_entry, users, expected_error
):
    """The YAML path enforces the same name rules as the single-slot path.

    Without this the migration's repair could be undone by the very next
    submission, and the options flow -- the only way to edit slots after
    setup -- goes through the same validator.
    """
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_USERS: users}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected_error}


async def test_a_slot_keyed_block_is_rejected_not_reinterpreted(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A block still keyed by slot number is rejected, not silently accepted.

    The editor takes users keyed by name. A slot-keyed block submitted by
    hand -- or pasted from a previous release -- is a different shape, and
    accepting it would key users by the digits that used to be slots.
    """
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_USERS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}}}
        )

    assert result["type"] == "form"
    # Named for what it is, rather than the generic parse failure: those
    # digits would otherwise coerce into users literally called "1".
    assert result["errors"] == {"base": "users_keyed_by_slot"}


async def test_every_name_error_supplies_what_its_message_asks_for(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A message that names a placeholder must be given one.

    Home Assistant renders these through IntlMessageFormat, which raises on a
    missing argument -- so the user sees a translation error where the
    explanation should be. The user-facing paths have no slot number to give,
    which is why their wording must not ask for one.
    """
    strings = json.loads(
        Path("custom_components/lock_code_manager/strings.json").read_text()
    )

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    with _holding():
        await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 2})

    for user_input in (
        {CONF_NAME: "", CONF_ENABLED: True, CONF_PIN: "1111"},
        {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1111"},
        {CONF_NAME: "raman ", CONF_ENABLED: True, CONF_PIN: "2222"},
    ):
        with _holding():
            result = await hass.config_entries.flow.async_configure(flow_id, user_input)
        for key in (result.get("errors") or {}).values():
            message = strings["config"]["error"][key]
            supplied = result.get("description_placeholders") or {}
            missing = {
                name
                for name in re.findall(r"\{(\w+)\}", message)
                if name not in supplied
            }
            assert not missing, f"{key} renders {missing} with nothing to fill them"


async def test_an_impossible_count_is_refused_without_asking_a_lock(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A count no lock could hold must not be read for first.

    The window starts at the number of users, so a mistyped 500 would
    otherwise be 500 round trips on a lock that answers one index at a time
    -- each one holding the operation lock -- before the flow says the lock
    has three slots.
    """
    reads: list[int] = []

    async def _read(self, slots=None):
        scope = list(self.managed_slots if slots is None else slots)
        reads.append(max(scope, default=0))
        return dict.fromkeys(scope, SlotCredential.empty())

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(3))
    with (
        probe_capabilities,
        patch.object(MockLCMLock, "async_get_usercodes", _read),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 500}
        )

    assert result["errors"] == {"base": "too_many_users"}
    assert reads == [], "the lock was read before the count was refused"


async def test_claims_above_the_window_do_not_widen_the_read(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Numbers another entry holds far above the window cost nothing to read.

    Occupancy is counted within the window, not across everything known, so
    claims the window never reaches cannot push it wider. Counting them would
    make a lock with a high-numbered neighbour entry read far past what
    placing these users needs.
    """
    windows: list[int] = []

    async def _read(self, slots=None):
        scope = list(self.managed_slots if slots is None else slots)
        windows.append(max(scope, default=0))
        return dict.fromkeys(scope, SlotCredential.empty())

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with (
        patch(
            "custom_components.lock_code_manager.domain.allocation.get_managed_slots",
            return_value=set(range(90, 100)),
        ),
        patch.object(MockLCMLock, "async_get_usercodes", _read),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 3}
        )

    assert result["step_id"] == "code_slot"
    assert windows == [3], "another entry's distant claims widened the read"


async def test_a_lock_that_allocates_its_own_index_is_not_asked(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Occupancy is not read from a lock whose contents cannot constrain it.

    Where the lock picks its own credential index, what it holds says nothing
    about which slot number is free -- so asking spends a round trip, or one
    per index on some providers, on an answer nothing reads.
    """
    reads: list[int] = []

    async def _read(self, slots=None):
        scope = list(self.managed_slots if slots is None else slots)
        reads.append(max(scope, default=0))
        return dict.fromkeys(scope, SlotCredential.empty())

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with (
        patch.object(
            MockLCMLock,
            "credential_index_follows_slot",
            new_callable=PropertyMock,
            return_value=False,
        ),
        patch.object(MockLCMLock, "async_get_usercodes", _read),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["step_id"] == "code_slot"
    assert reads == [], "a lock that cannot constrain the numbering was read"


@pytest.mark.parametrize(
    ("target", "boom"),
    [
        ("build_lock_instance", RuntimeError("provider blew up")),
        ("async_get_usercodes", TimeoutError("node asleep")),
    ],
)
async def test_a_lock_that_fails_unexpectedly_is_unknown_not_empty(
    hass: HomeAssistant, mock_lock_config_entry, target: str, boom: Exception
):
    """Any failure to answer has to read as unknown, not as an empty lock.

    Only errors this integration defines are promised; a provider can still
    raise something else. Letting that escape kills the flow, and the one
    reading it must never become "no numbers are taken".
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    if target == "build_lock_instance":
        patcher = patch(
            "custom_components.lock_code_manager.domain.allocation.build_lock_instance",
            side_effect=boom,
        )
    else:
        patcher = patch.object(MockLCMLock, target, AsyncMock(side_effect=boom))

    with patcher:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    # A form, not an abort: a lock that is merely asleep must not end setup.
    assert result["type"] == "form"
    assert result["step_id"] == "ui"
    assert result["errors"] == {"base": "occupancy_unknown"}
    assert LOCK_1_ENTITY_ID in result["description_placeholders"]["locks"]


async def test_a_count_that_fits_can_still_need_numbers_that_do_not(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Codes already on the lock push the last user past the capacity.

    Three users fit in four slots, so the bare count is accepted. Widening
    around the two codes already there needs a fifth number, and that is
    where it is refused.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # Three users fit in four slots; slots 1 and 2 are taken, so they would
    # land on 3, 4 and 5 -- and 5 does not exist.
    with _capacity_probe(return_value=_capabilities_with_slots(4)), _holding(1, 2):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 3}
        )

    assert result["errors"] == {"base": "numbers_needed_exceed_capacity"}
    # The count the user chose, and the number their last user would need.
    # Reporting either in the other's place makes the sentence false.
    assert result["description_placeholders"]["num_users"] == "3"
    assert result["description_placeholders"]["needed"] == "5"
    assert result["description_placeholders"]["num_slots"] == "4"


async def test_choosing_the_ui_path_reads_no_lock_on_the_way_in(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Only the path that needs a full read pays for one.

    Picking locks used to read every one of them up front, for the benefit of
    a confirmation only the YAML path shows. The path that chooses numbers
    itself reads what it needs, when it knows how much of the lock that is.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    assert result["step_id"] == "choose_path"

    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    windows: list[list[int]] = []

    async def _read(self, slots=None):
        scope = sorted(self.managed_slots if slots is None else slots)
        windows.append(scope)
        return {slot: SlotCredential.empty() for slot in scope}

    with patch.object(MockLCMLock, "async_get_usercodes", _read):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 1}
        )

    assert result["step_id"] == "code_slot"
    # Only the numbers it is about to issue were read, not the lock's range.
    assert windows == [[1]]


async def test_a_nearly_full_lock_is_read_once_through(
    hass: HomeAssistant, mock_lock_config_entry
):
    """No index is asked about twice, however many passes it takes.

    Widening from the count re-reading everything each pass costs a lock
    holding codes low down several times its own capacity to place a couple
    of users -- on the providers that answer one index per round trip, that
    is the cost this whole approach exists to avoid.
    """
    asked: list[int] = []
    occupied = set(range(1, 30))

    async def _read(self, slots=None):
        scope = sorted(self.managed_slots if slots is None else slots)
        asked.extend(scope)
        return {
            slot: (
                SlotCredential.known("0000")
                if slot in occupied
                else SlotCredential.empty()
            )
            for slot in scope
        }

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch.object(MockLCMLock, "async_get_usercodes", _read):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["step_id"] == "code_slot"
    # The users land on 30 and 31, so 31 indices are read -- each exactly once.
    assert sorted(asked) == list(range(1, 32))
    assert len(asked) == len(set(asked)), "an index was read more than once"

    for name in ("Raman", "Alice"):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: name, CONF_ENABLED: True, CONF_PIN: "1111"}
        )
    assert result["type"] == "create_entry"
    assert set(result["data"][CONF_SLOT_ASSIGNMENT].values()) <= set(asked)


async def test_numbers_another_entry_manages_are_stepped_over(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """A slot another entry holds on the same lock is not handed out twice.

    This replaced a loud refusal (`slots_already_configured`) with quietly
    routing around it, so it is the whole of what stops two entries writing
    the same credential index -- and the case that matters is a neighbour
    holding a LOW number the new users have to step over.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # The existing entry manages slots 1 and 2 on this lock; the lock itself
    # holds nothing, so only the neighbour's claim can push the users up.
    with _holding():
        await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 2})
        for name in ("Raman", "Alice"):
            result = await hass.config_entries.flow.async_configure(
                flow_id, {CONF_NAME: name, CONF_ENABLED: True, CONF_PIN: "1111"}
            )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOT_ASSIGNMENT] == {"alice": 3, "raman": 4}


async def test_a_blank_yaml_name_names_the_slot_it_came_from(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The YAML path can say which slot failed, and its message does."""
    strings = json.loads(
        Path("custom_components/lock_code_manager/strings.json").read_text()
    )
    flow_id = await _start_yaml_config_flow(hass)

    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"   ": {CONF_ENABLED: True, CONF_PIN: "1234"}}},
        )

    assert result["errors"] == {"base": "name_required"}
    assert result["description_placeholders"]["name"] == "   "
    message = strings["config"]["error"]["name_required"]
    assert not {
        name
        for name in re.findall(r"\{(\w+)\}", message)
        if name not in result["description_placeholders"]
    }


async def test_an_empty_lock_numbers_users_from_one(
    hass: HomeAssistant, mock_lock_config_entry
):
    """With nothing in the way, the users take the lowest numbers there are.

    Every other assignment test has codes or a neighbouring entry holding the
    low numbers, so none of them would notice allocation starting from
    somewhere other than 1 -- and starting higher hands out numbers no lock
    was ever read for.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with _holding():
        await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 3})
        for name in ("Raman", "Alice", "Wren"):
            result = await hass.config_entries.flow.async_configure(
                flow_id, {CONF_NAME: name, CONF_ENABLED: True, CONF_PIN: "1111"}
            )

    assert result["type"] == "create_entry"
    assert sorted(result["data"][CONF_SLOT_ASSIGNMENT].values()) == [1, 2, 3]


async def test_a_refused_count_comes_back_in_the_box(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The form returns holding what was refused, not its default.

    Otherwise the user who asked for eight is handed the default back and has
    to work out what they typed before they can adjust it.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with _capacity_probe(return_value=_capabilities_with_slots(2)), _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 8}
        )

    assert result["errors"] == {"base": "too_many_users"}
    suggested = [
        key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key == CONF_NUM_USERS and key.description
    ]
    assert suggested == [8]


async def test_the_search_stops_at_the_end_of_the_lock(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A lock cannot hand back a slot it does not have.

    Past its last slot every index answers "occupied" -- there is nothing
    there to report as free -- so a search with no end walks upward forever
    finding nothing. It stops where the lock says its numbers stop.
    """
    highest_asked: list[int] = []

    async def _read(self, slots=None):
        scope = sorted(self.managed_slots if slots is None else slots)
        highest_asked.extend(scope)
        # Ten real slots, all taken; anything beyond simply is not there.
        return {
            slot: (
                SlotCredential.known("0000")
                if slot <= 10
                else SlotCredential.unreadable()
            )
            for slot in scope
        }

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with (
        patch.object(MockLCMLock, "async_get_max_slot", AsyncMock(return_value=10)),
        patch.object(MockLCMLock, "async_get_usercodes", _read),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["errors"] == {"base": "numbers_needed_exceed_capacity"}
    # Two users on a lock whose ten slots are all taken: the second would
    # need number 12, and the lock stops at 10.
    assert result["description_placeholders"]["needed"] == "12"
    assert result["description_placeholders"]["num_slots"] == "10"
    assert max(highest_asked) <= 10, "the search read past the end of the lock"


async def test_the_smallest_lock_bounds_the_search(
    hass: HomeAssistant, mock_lock_config_entry
):
    """Every lock in an entry gets the same numbers, so the smallest wins."""
    flow_id = await _init_flow_to_user_step(hass)
    await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]},
    )
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    limits = {LOCK_1_ENTITY_ID: 30, LOCK_2_ENTITY_ID: 4}

    async def _max_slot(self):
        # Keyed by lock rather than call order, so a reordering or an extra
        # call cannot quietly hand back a different bound than the one this
        # test is about.
        return limits[self.lock.entity_id]

    with (
        patch.object(MockLCMLock, "async_get_max_slot", _max_slot),
        _holding(1, 2, 3),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    # Three taken means the pair would need numbers 4 and 5, and the smaller
    # lock stops at 4.
    assert result["errors"] == {"base": "numbers_needed_exceed_capacity"}
    assert result["description_placeholders"]["num_slots"] == "4"
    assert result["description_placeholders"]["lock"] == LOCK_2_ENTITY_ID


async def test_a_count_past_the_range_is_refused_before_reading(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The count itself is bounded, not only each widening after it.

    A count that already runs past the lock's last slot walks off the end on
    the way in, and on a lock that reads past-end as free it would be handed
    every one of those numbers.
    """
    reads: list[int] = []

    async def _read(self, slots=None):
        scope = sorted(self.managed_slots if slots is None else slots)
        reads.extend(scope)
        return dict.fromkeys(scope, SlotCredential.empty())

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with (
        patch.object(MockLCMLock, "async_get_max_slot", AsyncMock(return_value=5)),
        patch.object(MockLCMLock, "async_get_usercodes", _read),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 20}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "too_many_users"}
    assert result["description_placeholders"]["num_slots"] == "5"
    assert result["description_placeholders"]["lock"] == LOCK_1_ENTITY_ID
    assert reads == [], "the lock was read past its own range"


async def test_the_last_number_a_lock_holds_is_usable(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The bound is the highest usable number, not one past it."""
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # Four of five slots taken: the one user must land on 5, the last one.
    with (
        patch.object(MockLCMLock, "async_get_max_slot", AsyncMock(return_value=5)),
        _holding(1, 2, 3, 4),
    ):
        await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 1})
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1111"}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOT_ASSIGNMENT] == {"raman": 5}


async def test_a_lock_that_allocates_its_own_index_does_not_bound_the_search(
    hass: HomeAssistant, mock_lock_config_entry
):
    """What a self-indexing lock holds cannot cap another lock's numbering.

    Without the skip, a Matter lock advertising a handful of credentials
    would silently cap every slot number in the entry.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with (
        patch.object(
            MockLCMLock,
            "credential_index_follows_slot",
            new_callable=PropertyMock,
            return_value=False,
        ),
        patch.object(MockLCMLock, "async_get_max_slot", AsyncMock(return_value=2)),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 8}
        )

    # Its answer of 2 is not consulted, so eight users are fine.
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_no_lock_that_can_answer_leaves_our_own_limit(
    hass: HomeAssistant, mock_lock_config_entry
):
    """With nothing able to say, the refusal is ours and says so.

    Naming it as a lock's capacity would tell the user to re-interview a lock
    over a number the lock never reported.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # An ordinary lock that simply does not report a range -- the common
    # case, not an exotic one. It must not be named as the source of a limit
    # it never gave, or the user is sent to re-interview it over a number it
    # never reported.
    with patch.object(MockLCMLock, "async_get_max_slot", AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: MAX_SEARCHED_SLOT + 1}
        )

    assert result["errors"] == {"base": "search_limit_reached"}
    assert result["description_placeholders"]["max_slot"] == str(MAX_SEARCHED_SLOT)
    assert "lock" not in result["description_placeholders"]


async def test_a_lock_that_reports_its_range_is_the_one_named(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Only a lock that answered is blamed, and only the smallest one.

    Ties are ordinary -- two locks of a kind answer alike -- so the same lock
    has to be named every time rather than whichever came first.
    """
    flow_id = await _init_flow_to_user_step(hass)
    await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "test", CONF_LOCKS: [LOCK_2_ENTITY_ID, LOCK_1_ENTITY_ID]},
    )
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # Both answer the same, so the name cannot come from iteration order.
    with patch.object(MockLCMLock, "async_get_max_slot", AsyncMock(return_value=4)):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 9}
        )

    assert result["errors"] == {"base": "too_many_users"}
    assert result["description_placeholders"]["num_slots"] == "4"
    assert result["description_placeholders"]["lock"] == min(
        [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]
    )


@pytest.mark.parametrize(
    ("reason", "managed"),
    [("not in the entity registry", True), ("an unsupported platform", False)],
)
async def test_a_lock_that_cannot_be_built_says_whether_it_is_ours(
    hass: HomeAssistant, mock_lock_config_entry, reason: str, managed: bool
) -> None:
    """
    Whether Lock Code Manager writes to it decides whether it bounds numbers.

    A lock this integration writes to constrains allocation even when it
    cannot be read; one on a platform it does not support constrains nothing,
    because nothing will ever be written there.
    """
    ent_reg = er.async_get(hass)
    if managed:
        entity_id = "lock.never_registered"
    else:
        entity_id = ent_reg.async_get_or_create(
            "lock", "some_other_integration", "unique"
        ).entity_id

    with pytest.raises(LockQuerySkipped) as raised:
        build_lock_instance(hass, dr.async_get(hass), ent_reg, None, entity_id)

    assert raised.value.managed is managed


async def test_a_skipped_lock_read_names_the_entry_that_asked(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Giving up on a lock has to say who gave up on it.

    Locks are shared between entries, so the lock alone does not identify
    the read that stopped: whoever reads the log has to be able to tell
    which entry's configuration to go and look at. Setup is the one caller
    with honestly nobody to name.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    def _asked_by(config_entry) -> str:
        caplog.clear()
        with caplog.at_level(logging.WARNING), pytest.raises(LockQuerySkipped):
            build_lock_instance(
                hass, dev_reg, ent_reg, config_entry, "lock.never_registered"
            )
        [record] = [
            record
            for record in caplog.records
            if record.name.endswith("domain.allocation")
        ]
        asked_by, lock_entity_id = record.args
        assert lock_entity_id == "lock.never_registered"
        return asked_by

    assert (
        _asked_by(lock_code_manager_config_entry)
        == lock_code_manager_config_entry.entry_id
    )
    assert _asked_by(None) == "New entry"


async def test_setup_refuses_when_a_lock_cannot_be_read(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An unreadable lock stops allocation rather than being assumed empty.

    Issuing a number a lock never answered about could overwrite a credential
    somebody programmed by hand.
    """
    flow_id = await _start_yaml_config_flow(hass)

    async def _unreadable(self, slots=None):
        raise LockCodeManagerError("lock is asleep")

    with patch.object(MockLCMLock, "async_get_usercodes", _unreadable):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_USERS: {"Raman": {CONF_ENABLED: True, CONF_PIN: "1234"}}},
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "occupancy_unknown"}


async def test_editing_refuses_when_a_lock_cannot_be_read(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The editor answers to the same refusal setup does."""
    flow_id, _ = await _start_options_flow(hass)

    async def _unreadable(self, slots=None):
        raise LockCodeManagerError("lock is asleep")

    with patch.object(MockLCMLock, "async_get_usercodes", _unreadable):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {
                    "User 1": {CONF_ENABLED: True, CONF_PIN: "1234"},
                    "Newcomer": {CONF_ENABLED: True, CONF_PIN: "9999"},
                },
            },
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "occupancy_unknown"}


async def test_a_lock_whose_config_entry_is_gone_is_skipped(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A registry row can outlive the entry that created it."""
    ent_reg = er.async_get(hass)
    orphan = ent_reg.async_get_or_create(
        "lock", "test", "orphaned", config_entry=mock_lock_config_entry
    )
    ent_reg.async_update_entity(orphan.entity_id, config_entry_id=None)

    with pytest.raises(LockQuerySkipped) as raised:
        build_lock_instance(hass, dr.async_get(hass), ent_reg, None, orphan.entity_id)

    # Ours, so it still bounds the numbers even unread.
    assert raised.value.managed is True


async def test_reauth_reports_a_slot_another_entry_already_manages(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    Swapping a lock can collide an existing slot set with another entry's.

    Nobody picks numbers any more, but reauth moves an entry's existing
    numbers onto a different lock, where they may already be spoken for.
    """
    other = MockConfigEntry(
        domain=DOMAIN,
        title="other",
        unique_id="other",
        data={
            CONF_LOCKS: [LOCK_2_ENTITY_ID],
            CONF_USERS: {"Someone": {CONF_ENABLED: True, CONF_PIN: "4321"}},
            CONF_SLOT_ASSIGNMENT: {"someone": 1},
        },
    )
    other.add_to_hass(hass)

    errors, placeholders = _check_common_slots(
        hass,
        [LOCK_2_ENTITY_ID],
        get_entry_config(lock_code_manager_config_entry).slot_numbers,
        lock_code_manager_config_entry,
    )

    assert errors == {"base": "slots_already_configured"}
    assert placeholders["entry_title"] == "other"


async def test_an_entry_may_reuse_a_number_it_just_released(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An entry's own numbers are not a constraint on itself.

    The ones it keeps are held by tenure; the one it is releasing in the very
    same submission is free. Counting them as taken pushed every replacement
    onto a higher number, so an entry edited enough times would run out of
    room on a lock with plenty left.
    """
    flow_id, _ = await _start_options_flow(hass)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                # "User 1" held slot 1 and is gone.
                CONF_USERS: {"Replacement": {CONF_ENABLED: True, CONF_PIN: "5555"}},
            },
        )

    assert result["data"][CONF_SLOT_ASSIGNMENT] == {"replacement": 1}


async def test_another_entry_still_holds_its_numbers(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Excluding an entry from its own claims must not excuse anyone else's."""
    flow_id, _ = await _start_options_flow(hass, locks=[LOCK_1_ENTITY_ID])

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {"Newcomer": {CONF_ENABLED: True, CONF_PIN: "5555"}},
            },
        )

    taken = get_entry_config(lock_code_manager_config_entry).slot_numbers
    assert not set(result["data"][CONF_SLOT_ASSIGNMENT].values()) & set(taken)


async def test_editing_keeps_what_the_form_never_asked_about(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An entry carries more than this form knows about.

    Building the saved data by hand drops every key the form does not ask
    for, silently, on the first edit.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        unique_id="carries-extra",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
            "written_by_something_else": {"kept": True},
        },
    )
    entry.add_to_hass(hass)
    started = await hass.config_entries.options.async_init(entry.entry_id)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            started["flow_id"],
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            },
        )

    assert result["data"]["written_by_something_else"] == {"kept": True}


async def test_the_editor_refuses_a_condition_entity_the_guided_path_would(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    One field, one rule, whichever route writes it.

    Some integrations' switches and binary sensors do not describe access at
    all, so the guided path refuses them. An editor that accepted them would
    be a way around that, and the entity would gate a credential on a state
    that means something else entirely.
    """
    ent_reg = er.async_get(hass)
    excluded = ent_reg.async_get_or_create(
        "switch", next(iter(EXCLUDED_CONDITION_PLATFORMS)), "unique"
    )

    flow_id = await _start_yaml_config_flow(hass)
    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_USERS: {
                    "Raman": {
                        CONF_ENABLED: True,
                        CONF_PIN: "1234",
                        CONF_CONDITION: excluded.entity_id,
                    }
                }
            },
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "excluded_platform"}
    assert result["description_placeholders"]["name"] == "Raman"


async def test_reauth_reports_a_lock_too_small_for_the_existing_slots(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    Swapping in a smaller lock is refused, naming it and its capacity.

    Reauth is the one place an already-valid slot set can stop fitting: the
    numbers were checked against the lock they were issued for, and reauth
    moves them onto a different one. Accepting it would leave every slot past
    the new lock's range writing forever against an index it does not have.
    """
    entry = lock_code_manager_config_entry
    entry.async_start_reauth(hass, context={"lock_entity_id": LOCK_1_ENTITY_ID})
    await hass.async_block_till_done()

    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    with _capacity_probe(return_value=_capabilities_with_slots(1)):
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["errors"] == {"base": "slot_out_of_range"}
    assert result["description_placeholders"]["num_slots"] == "1"
    assert result["description_placeholders"]["out_of_range_slots"] == "2"


def _suggested_values(result) -> dict[str, object]:
    """Read back what a re-shown form offers the user for each field."""
    return {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if key.description and "suggested_value" in key.description
    }


async def test_user_step_rejects_a_lock_the_entity_registry_does_not_know(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A lock with no registry row is refused where it is chosen, not after setup.

    The picker offers every lock entity now, which puts an entity with no
    registry row -- a YAML lock with no unique ID -- in front of the user for
    the first time. It can never work: the entry keys declarations and
    devices by registry id, and setup refuses the roster over exactly this.
    Accepted here it did not even reach that refusal; it got three steps
    further and failed with ``occupancy_unknown``, telling the user to go
    wake a lock that was never going to answer.
    """
    hass.states.async_set("lock.yaml_only", LockState.LOCKED)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID, "lock.yaml_only"]},
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "lock_not_registered"}
    assert result["description_placeholders"] == {
        "unregistered_locks": "lock.yaml_only"
    }
    assert _suggested_values(result) == {
        CONF_NAME: "test",
        CONF_LOCKS: [LOCK_1_ENTITY_ID, "lock.yaml_only"],
    }


async def test_reauth_rejects_a_lock_the_entity_registry_does_not_know(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Reauth refuses it too, including the one it was started over.

    This is the one refusal that is not grandfathered. A missing lock is why
    setup failed and why reauth is open, so waving through the lock the entry
    already holds would let the user submit the form unchanged, save, reload,
    fail again and land back in reauth with nothing said.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: ["lock.yaml_only"],
            CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
        },
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass, context={"lock_entity_id": "lock.yaml_only"})
    await hass.async_block_till_done()
    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: ["lock.yaml_only"]}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "lock_not_registered"}
    assert result["description_placeholders"]["unregistered_locks"] == "lock.yaml_only"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"


async def test_user_step_rejects_unclaimed_mqtt_lock(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    Selecting an mqtt lock no provider claims is refused at submit time.

    The entity selector can only filter by integration, so an mqtt lock from
    an unsupported bridge is offered; nothing but this check stands between
    picking it and a setup that fails after the entry exists.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id]},
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_LOCKS: "unsupported_mqtt_lock"}
    assert result["description_placeholders"] == {"locks": unclaimed.entity_id}
    # The refusal comes back holding the name, which means the check ran
    # before the step consumed it.
    assert _suggested_values(result) == {
        CONF_NAME: "test",
        CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id],
    }

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"


async def test_user_step_accepts_claimed_mqtt_lock(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """A Zigbee2MQTT lock is an mqtt lock a provider does claim, so it passes."""
    z2m_lock = await async_discover_z2m_lock(hass)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [z2m_lock.entity_id]}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"


async def test_options_flow_rejects_unclaimed_mqtt_lock(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """Adding an unclaimed mqtt lock to an existing entry is refused too."""
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id],
                CONF_USERS: users,
            },
        )

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_LOCKS: "unsupported_mqtt_lock"}
    assert result["description_placeholders"]["locks"] == unclaimed.entity_id

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users},
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOCKS] == [LOCK_1_ENTITY_ID]


async def test_options_flow_saves_around_a_grandfathered_unclaimed_lock(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    An entry that already holds an unclaimed lock can still be edited.

    Entries configured before selection-time validation existed can be
    carrying one, and the options form re-renders the whole lock list every
    time -- so validating all of it meant the entry could never be saved
    again. Changing a PIN was refused because of a lock the user was not
    touching, with no way out but hand-editing storage.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    locks = [LOCK_1_ENTITY_ID, unclaimed.entity_id]
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: locks,
            CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
        },
    )
    entry.add_to_hass(hass)
    started = await hass.config_entries.options.async_init(entry.entry_id)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={
                CONF_LOCKS: locks,
                CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "9999"}},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOCKS] == locks
    assert result["data"][CONF_USERS]["User 1"][CONF_PIN] == "9999"


async def test_options_flow_still_refuses_a_newly_added_unclaimed_lock(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    Grandfathering is per lock, not per entry.

    An entry already carrying one unclaimed lock must not become a place
    where any other one can be added unchecked.
    """
    grandfathered = await async_discover_unclaimed_mqtt_lock(hass)
    newcomer = await async_discover_unclaimed_mqtt_lock(hass, suffix="_two")
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID, grandfathered.entity_id],
            CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
        },
    )
    entry.add_to_hass(hass)
    started = await hass.config_entries.options.async_init(entry.entry_id)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={
                CONF_LOCKS: [
                    LOCK_1_ENTITY_ID,
                    grandfathered.entity_id,
                    newcomer.entity_id,
                ],
                CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            },
        )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_LOCKS: "unsupported_mqtt_lock"}
    assert result["description_placeholders"]["locks"] == newcomer.entity_id


async def test_options_flow_renders_lock_and_users_errors_together(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    One submission that is wrong in two ways comes back complaining about both.

    The lock check accumulates into the same error dict the users validation
    writes to, under a different key, instead of short-circuiting it. Without
    that, fixing the lock would only reveal the next complaint, so the user
    pays a round trip per mistake.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    flow_id, _ = await _start_options_flow(hass)

    result = await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id],
            # Enabled with no PIN is invalid per schema.
            CONF_USERS: {"Raman": {CONF_ENABLED: True}},
        },
    )

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {
        CONF_LOCKS: "unsupported_mqtt_lock",
        "base": "invalid_config",
    }
    assert result["description_placeholders"]["locks"] == unclaimed.entity_id


async def test_reauth_rejects_unclaimed_mqtt_lock(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    mqtt_mock,
    mqtt_teardown,
) -> None:
    """Reauth is a lock-selection step too, so it applies the same rule."""
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    entry = lock_code_manager_config_entry
    entry.async_start_reauth(hass, context={"lock_entity_id": LOCK_1_ENTITY_ID})
    await hass.async_block_till_done()

    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [unclaimed.entity_id]}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {CONF_LOCKS: "unsupported_mqtt_lock"}
    assert result["description_placeholders"]["locks"] == unclaimed.entity_id


async def test_reauth_completes_around_a_grandfathered_unclaimed_lock(
    hass: HomeAssistant,
    mock_lock_config_entry,
    mqtt_mock,
    mqtt_teardown,
) -> None:
    """
    Reauth is the recovery path, so an old unclaimed lock must not block it.

    The form is pre-filled with the entry's whole lock list, so re-validating
    all of it meant an entry carrying one unclaimed lock could never finish a
    reauth -- the exact entry most likely to be in reauth, and the swap it is
    asking for is on a different lock entirely.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **BASE_CONFIG,
            CONF_LOCKS: [LOCK_1_ENTITY_ID, unclaimed.entity_id],
        },
        unique_id="grandfathered_reauth",
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass, context={"lock_entity_id": LOCK_1_ENTITY_ID})
    await hass.async_block_till_done()

    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [LOCK_2_ENTITY_ID, unclaimed.entity_id]}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"


async def test_options_flow_preserves_member_declarations(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    Editing users through the options flow keeps the member declarations.

    The flow rebuilds the whole entry from an EntryConfig and writes it, and
    it never asks about members, so anything it does not carry forward is
    erased by the one write a user reaches from the UI.
    """
    ent_reg = er.async_get(hass)
    lock_1 = ent_reg.async_get(LOCK_1_ENTITY_ID)
    assert lock_1
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**BASE_CONFIG, CONF_MEMBERS: {lock_1.id: {"placeholder": True}}},
        unique_id="Mock Title",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with _holding():
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={
                CONF_LOCKS: list(BASE_CONFIG[CONF_LOCKS]),
                CONF_USERS: {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}},
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_MEMBERS] == {lock_1.id: {"placeholder": True}}
    # The edit the declaration had to survive actually landed.
    assert set(result["data"][CONF_USERS]) == {"test1"}


# --- Locks nothing claims, that the user declares codeless (issue #1484) ---


async def _menu_about(result, lock_entry, step_id: str = "codeless") -> None:
    """Assert a flow result is the codeless question, naming this one lock."""
    assert result["type"] == "menu"
    assert result["step_id"] == step_id
    assert result["description_placeholders"] == {"lock": lock_entry.entity_id}


async def test_the_lock_picker_offers_every_lock(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The picker is not an allowlist of the platforms that resolve to a provider.

    A lock nothing claims may still be one Lock Code Manager can manage by
    holding its codes, and the question that settles it is asked after the
    submission -- which a picker that hides the lock makes unaskable.
    """
    flow_id = await _init_flow_to_user_step(hass)
    result = await hass.config_entries.flow.async_configure(flow_id)

    [locks_field] = [
        key for key in result["data_schema"].schema if str(key) == CONF_LOCKS
    ]
    config = result["data_schema"].schema[locks_field].config

    assert config["domain"] == [LOCK_DOMAIN]
    assert "filter" not in config


async def test_the_user_step_asks_about_a_lock_nothing_claims(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Confirming records the declaration on the entry it creates."""
    codeless = register_codeless_lock(hass)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [codeless.entity_id]}
    )
    await _menu_about(result, codeless)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_confirm"}
    )

    # The answer re-submits what it was asked about, so the flow carries on
    # from where it would have been.
    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"

    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 1})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_MEMBERS] == {codeless.id: {CONF_CODELESS: True}}


async def test_a_lock_a_provider_claims_is_never_asked_about(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The question is only for the locks whose answer is not already known."""
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    assert result["step_id"] == "choose_path"


async def test_an_unclaimed_mqtt_lock_is_refused_rather_than_asked_about(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    mqtt keeps its own refusal.

    Its dispatch is per device, so an unclaimed one means "this bridge is
    not one Lock Code Manager speaks" -- a gap that may close later, not a
    lock with no code storage. Offering the declaration there would strand
    that lock's credentials in a Lock Code Manager store on the day its
    bridge became supported.
    """
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [unclaimed.entity_id]}
    )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_LOCKS: "unsupported_mqtt_lock"}


async def test_declining_lands_back_on_the_form_that_asked(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Declining is not a dead end: the way out is to choose different locks.

    The refusal names them and the form comes back holding what was
    submitted, so the selection can be changed rather than retyped.
    """
    codeless = register_codeless_lock(hass)
    flow_id = await _init_flow_to_user_step(hass)

    await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id]},
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_decline"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_LOCKS: "codeless_declined"}
    assert result["description_placeholders"] == {"locks": codeless.entity_id}
    assert _suggested_values(result) == {
        CONF_NAME: "test",
        CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id],
    }

    # Submitting the same locks again asks nothing new -- the answer stands
    # for this flow -- and refuses again. Dropping the lock is what clears it.
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id]},
    )

    assert result["errors"] == {CONF_LOCKS: "codeless_declined"}

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"


async def test_the_options_flow_asks_about_a_lock_added_later(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A lock that arrives after setup gets the same question setup would ask."""
    codeless = register_codeless_lock(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}
    locks = [LOCK_1_ENTITY_ID, codeless.entity_id]

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: locks, CONF_USERS: users}
        )
    await _menu_about(result, codeless)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_confirm"}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOCKS] == locks
    assert result["data"][CONF_MEMBERS] == {codeless.id: {CONF_CODELESS: True}}


async def test_the_options_flow_lets_a_declaration_be_taken_back(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An entry that already declares a lock is asked again, and can answer no.

    Asking only about locks nobody has answered for would leave a
    declaration with no way back: there is no second provider to hand the
    lock to, so taking it back means dropping the lock, and the user has to
    be able to reach that decision from the form.
    """
    codeless = register_codeless_lock(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            **BASE_CONFIG,
            CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id],
            CONF_MEMBERS: {codeless.id: {CONF_CODELESS: True}},
        },
    )
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id],
                CONF_USERS: users,
            },
        )
    await _menu_about(result, codeless)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_decline"}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["errors"] == {CONF_LOCKS: "codeless_declined"}

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    # Answering no is what undoes it, so the entry stops declaring anything.
    assert result["data"][CONF_MEMBERS] == {}


async def test_a_declared_member_a_provider_now_claims_is_still_asked_about(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A declaration must have an exit even after its platform gains a provider.

    Dispatch never takes a declared member back -- deliberately, so an
    upgrade cannot move somebody's credentials onto a device they never
    agreed to write to -- which leaves the form as the only way out. Asking
    only about the locks nothing claims made that exit unreachable: the
    member kept resolving to the Lock Code Manager store while no form ever
    mentioned it.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={**BASE_CONFIG, CONF_MEMBERS: {claimed.id: {CONF_CODELESS: True}}},
    )
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )
    # Its own question, not the one a lock nothing claims gets: declining
    # here hands the lock to the provider that claims it now and saves,
    # rather than refusing the submission.
    await _menu_about(result, claimed, "codeless_reconsider")

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_decline"}
        )

    # Not refused: something claims this lock now, so declining hands it to
    # that provider rather than leaving a lock nothing can manage.
    assert result["type"] == "create_entry"
    assert result["data"][CONF_LOCKS] == [LOCK_1_ENTITY_ID]
    # Read the way dispatch reads it, not as a dict. What declining buys is
    # the provider that claims the lock now taking it back; an assertion on
    # the stored shape passes just as well while the member goes on
    # resolving to the Lock Code Manager store.
    assert (
        resolve_member_provider_class(
            dr.async_get(hass), EntryConfig.from_mapping(result["data"]), claimed
        )
        is MockLCMLock
    )


async def test_an_answer_about_a_lock_the_same_visit_drops_is_not_stored(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A yes is not written back about a lock the submission no longer holds.

    The answer outlives the question it answered: it is cached for the life
    of the flow, so a re-submission refused for some unrelated reason -- a
    sibling lock too asleep to report its occupancy -- comes back with the
    yes still held. Dropping the lock is the obvious reaction to that
    refusal, and storing the answer anyway leaves a declaration about a
    member the entry does not have, waiting to decide the provider for
    whatever is added under that registry id next.
    """
    codeless = register_codeless_lock(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id],
                CONF_USERS: users,
            },
        )
    await _menu_about(result, codeless)

    async def _unreadable(self, slots=None):
        raise LockCodeManagerError("lock is asleep")

    # Yes -- and the re-submission it causes fails over the sibling lock.
    with patch.object(MockLCMLock, "async_get_usercodes", _unreadable):
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_confirm"}
        )

    assert result["errors"] == {"base": "occupancy_unknown"}

    # So the lock that was answered about is dropped instead.
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_MEMBERS] == {}

    # And the lock re-added later is a lock nobody has answered for, rather
    # than one that resolves to the Lock Code Manager store unasked.
    hass.config_entries.async_update_entry(entry, data=result["data"], options={})
    started = await hass.config_entries.options.async_init(entry.entry_id)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id],
                CONF_USERS: users,
            },
        )

    await _menu_about(result, codeless)


async def test_dropping_a_lock_drops_what_was_declared_about_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A declaration about a member the entry no longer holds is not carried forward.

    Nothing reads it while the lock is gone, so the cost is invisible until
    the entry has collected one husk per lock anybody ever removed.
    """
    codeless = register_codeless_lock(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            **BASE_CONFIG,
            CONF_LOCKS: [LOCK_1_ENTITY_ID, codeless.entity_id],
            CONF_MEMBERS: {codeless.id: {CONF_CODELESS: True}},
        },
    )
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users},
        )

    # Nothing was asked -- the lock simply left -- and the declaration left
    # with it.
    assert result["type"] == "create_entry"
    assert result["data"][CONF_MEMBERS] == {}


async def test_re_adding_a_dropped_lock_does_not_resurrect_its_declaration(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A lock the entry forgot about comes back as a lock nobody has answered for.

    Written with a lock a provider claims, because that is where the stale
    declaration has teeth: dispatch reads the declaration FIRST, so one left
    behind decides the provider for a lock with real code storage. It is
    survivable only because the flow now asks about declared members too --
    which is the tell this test reads. A lock re-added with nothing declared
    about it saves in one round trip; one carrying a husk stops to ask a
    question the user already answered, about a lock they never declared.
    """
    reused = er.async_get(hass).async_get(LOCK_2_ENTITY_ID)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={**BASE_CONFIG, CONF_MEMBERS: {reused.id: {CONF_CODELESS: True}}},
    )
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    dropped = await hass.config_entries.options.async_init(entry.entry_id)
    with _holding():
        result = await hass.config_entries.options.async_configure(
            dropped["flow_id"],
            user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users},
        )
    assert result["data"][CONF_MEMBERS] == {}
    hass.config_entries.async_update_entry(entry, data=result["data"], options={})

    re_added = await hass.config_entries.options.async_init(entry.entry_id)
    with _holding():
        result = await hass.config_entries.options.async_configure(
            re_added["flow_id"],
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
                CONF_USERS: users,
            },
        )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_MEMBERS] == {}


async def test_the_options_flow_asks_nothing_when_no_lock_needs_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Editing users on an entry of ordinary locks is one round trip, as before."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)

    started = await hass.config_entries.options.async_init(entry.entry_id)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            started["flow_id"],
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_USERS: {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}},
            },
        )

    assert result["type"] == "create_entry"


async def test_reauth_asks_about_a_lock_nothing_claims(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Reauth renders the same picker, so it is a third way in.

    Without the question, somebody repairing one broken lock could swap in a
    codeless one and land straight back in reauth, with nothing telling them
    why.
    """
    codeless = register_codeless_lock(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
        },
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass, context={"lock_entity_id": LOCK_1_ENTITY_ID})
    await hass.async_block_till_done()
    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [codeless.entity_id]}
    )
    await _menu_about(result, codeless)

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "codeless_confirm"}
    )
    await hass.async_block_till_done()

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"
    assert entry.data[CONF_LOCKS] == [codeless.entity_id]
    assert entry.data[CONF_MEMBERS] == {codeless.id: {CONF_CODELESS: True}}


async def test_an_answer_applies_only_to_the_member_it_was_asked_about(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Declining a lock just added leaves an unrelated declared member alone.

    The set asked about mixes two populations: a lock nothing claims that
    this submission adds, and every member the entry already declares. One
    Yes/No over both meant an answer aimed at the newcomer was recorded
    against the other member too -- so saying "no, don't manage that new
    lock" stripped the declaration off a lock somebody had said must never
    be written to, and handed it straight back to its provider.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={**BASE_CONFIG, CONF_MEMBERS: {claimed.id: {CONF_CODELESS: True}}},
    )
    entry.add_to_hass(hass)
    newcomer = register_codeless_lock(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]

    # Both locks are asked about, one question each, in the order submitted.
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id,
            user_input={
                CONF_LOCKS: [LOCK_1_ENTITY_ID, newcomer.entity_id],
                CONF_USERS: users,
            },
        )
    await _menu_about(result, claimed, "codeless_reconsider")

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_confirm"}
        )
    await _menu_about(result, newcomer)

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_decline"}
        )

    # The refusal names the newcomer and nothing else.
    assert result["errors"] == {CONF_LOCKS: "codeless_declined"}
    assert result["description_placeholders"]["locks"] == newcomer.entity_id

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )

    # Read the way dispatch reads it: what matters is that the untouched
    # member still resolves to the Lock Code Manager store, which an
    # assertion on the stored shape alone would not settle.
    assert result["type"] == "create_entry"
    assert (
        resolve_member_provider_class(
            dr.async_get(hass), EntryConfig.from_mapping(result["data"]), claimed
        )
        is CodelessLock
    )


async def test_taking_a_declaration_back_is_sized_against_the_real_lock(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Capacity is checked against the provider the lock is about to become.

    Reading the STORED declaration made a pending answer invisible to the
    very validation the answer triggers a re-run of: the lock still resolved
    to the Lock Code Manager store -- no capacity and nothing in it -- so a
    configuration far too large for the real lock saved without a word, and
    every user past the lock's last slot silently failed to be written from
    then on.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    users = {
        name: {CONF_PIN: pin, CONF_ENABLED: True}
        for name, pin in (("Ada", "1111"), ("Bea", "2222"), ("Cal", "3333"))
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_MEMBERS: {claimed.id: {CONF_CODELESS: True}},
            CONF_USERS: users,
            CONF_SLOT_ASSIGNMENT: {"ada": 1, "bea": 2, "cal": 3},
        },
    )
    entry.add_to_hass(hass)

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]
    # Two slots is fewer than the three users, so only a check that reaches
    # the real lock can refuse this.
    probe = _capacity_probe(return_value=_capabilities_with_slots(2))

    with probe, _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )
    await _menu_about(result, claimed, "codeless_reconsider")

    with probe, _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_decline"}
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "too_many_users"}
    assert result["description_placeholders"]["num_slots"] == "2"
    # Nothing was written, so the member is still what it was.
    assert get_entry_config(entry).is_codeless(claimed)


async def test_both_lock_refusals_render_from_one_submission(
    hass: HomeAssistant, mock_lock_config_entry, mqtt_mock, mqtt_teardown
) -> None:
    """
    A selection with one of each problem says so once, not over two visits.

    Refusing on the first and returning hid the second entirely: the user
    fixed what they were shown, submitted again, and was refused again for
    something that had been wrong all along.
    """
    hass.states.async_set("lock.yaml_only", LockState.LOCKED)
    unclaimed = await async_discover_unclaimed_mqtt_lock(hass)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_NAME: "test",
            CONF_LOCKS: [LOCK_1_ENTITY_ID, "lock.yaml_only", unclaimed.entity_id],
        },
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {
        "base": "lock_not_registered",
        CONF_LOCKS: "unsupported_mqtt_lock",
    }
    # Named apart, so one flat mapping renders both messages.
    assert result["description_placeholders"]["unregistered_locks"] == "lock.yaml_only"
    assert result["description_placeholders"]["locks"] == unclaimed.entity_id


# --- One lock, one answer, however many configurations manage it ---


def _sibling_declaring(
    hass: HomeAssistant, lock_entry: er.RegistryEntry, *, declares: bool
) -> MockConfigEntry:
    """
    Add a second entry that holds a lock, declaring it or not.

    Its user sits well clear of the slot numbers the entries under test use,
    so what refuses a submission here is the disagreement about the lock
    rather than the slot both entries wanted.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Upstairs Codes",
        unique_id="upstairs_codes",
        data={
            CONF_LOCKS: [lock_entry.entity_id],
            CONF_MEMBERS: {lock_entry.id: {CONF_CODELESS: True}} if declares else {},
            CONF_USERS: {"Ada": {CONF_PIN: "1111", CONF_ENABLED: True}},
            CONF_SLOT_ASSIGNMENT: {"ada": 9},
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_lock_another_entry_declares_is_asked_about_here_too(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Agreeing has to be an answer this entry can give.

    A lock a provider claims is otherwise never asked about, so an entry
    adding one that another entry declares codeless could only ever
    contradict it -- and would then be refused with no answer available that
    was not. The question is asked because a sibling already answered it,
    not because dispatch came up empty.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    _sibling_declaring(hass, claimed, declares=True)
    flow_id = await _init_flow_to_user_step(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await _menu_about(result, claimed, "codeless_reconsider")

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_confirm"}
    )
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 1})
    with _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "Bea", CONF_ENABLED: True, CONF_PIN: "1234"}
        )

    assert result["type"] == "create_entry"
    # Read the way dispatch reads it: the entry agrees with its sibling
    # about what the member IS, not merely about what to store.
    assert (
        resolve_member_provider_class(
            dr.async_get(hass), EntryConfig.from_mapping(result["data"]), claimed
        )
        is CodelessLock
    )


async def test_a_second_configuration_may_not_contradict_the_first(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Two entries answering differently is refused, and the other one is named.

    Whether a lock keeps codes of its own is a fact about the device, so the
    disagreement is not a configuration but a contradiction -- and one with
    teeth, because the two answers resolve to two credential stores over one
    entity and a Personal Identification Number lands in whichever the
    caller happened to reach. The refusal names the other configuration
    because reconciling it is the only way through.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    _sibling_declaring(hass, claimed, declares=True)
    flow_id = await _init_flow_to_user_step(hass)

    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_decline"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_LOCKS: "codeless_conflict"}
    assert result["description_placeholders"] == {
        "locks": f"{LOCK_1_ENTITY_ID} (Upstairs Codes)"
    }
    # Nothing was created, so there is no second store for the refusal to
    # have been merely cosmetic about.
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_declaring_a_lock_another_entry_holds_undeclared_is_refused(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The refusal runs in both directions, because the contradiction does.

    A lock another entry manages through its own integration is one that has
    code storage, whatever this entry is about to say -- so declaring it here
    would leave the same entity resolving two ways round. The sibling is
    built by hand because no flow will produce this pair; it is the shape
    storage can still carry, and the guard has to hold for it.
    """
    codeless = register_codeless_lock(hass)
    _sibling_declaring(hass, codeless, declares=False)
    flow_id = await _init_flow_to_user_step(hass)

    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [codeless.entity_id]}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_confirm"}
    )

    assert result["errors"] == {CONF_LOCKS: "codeless_conflict"}
    assert result["description_placeholders"] == {
        "locks": f"{codeless.entity_id} (Upstairs Codes)"
    }


async def test_the_options_flow_may_not_take_back_what_a_sibling_declares(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Handing a shared lock back is a decision both configurations have to make.

    Taking the declaration back here alone would leave the sibling holding
    the lock's codes in Lock Code Manager while this entry started writing
    them to the device -- the same entity, two stores. Removing the lock
    from one of the two is the way through, which is what the refusal says.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    _sibling_declaring(hass, claimed, declares=True)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={**BASE_CONFIG, CONF_MEMBERS: {claimed.id: {CONF_CODELESS: True}}},
    )
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )
    await _menu_about(result, claimed, "codeless_reconsider")

    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_decline"}
        )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_LOCKS: "codeless_conflict"}
    # The declaration is still the one the entry runs on, so the member
    # keeps resolving to the store the sibling is also reading.
    assert get_entry_config(entry).is_codeless(claimed)


async def test_the_entry_being_edited_does_not_contradict_itself(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An entry's own stored answer is the one being replaced, not an obstacle.

    The sibling scan reads every OTHER entry; reading this one too would
    refuse every answer that changed anything, so a declaration could never
    be taken back at all -- the exit the menu exists to offer.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={**BASE_CONFIG, CONF_MEMBERS: {claimed.id: {CONF_CODELESS: True}}},
    )
    entry.add_to_hass(hass)
    users = {"test1": {CONF_PIN: "1234", CONF_ENABLED: True}}

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]
    with _holding():
        await hass.config_entries.options.async_configure(
            flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
        )
    with _holding():
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "codeless_decline"}
        )

    assert result["type"] == "create_entry"
    assert (
        resolve_member_provider_class(
            dr.async_get(hass), EntryConfig.from_mapping(result["data"]), claimed
        )
        is MockLCMLock
    )


async def test_reauth_may_not_contradict_a_sibling_either(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Reauth renders the same picker, so it is a third way to introduce the pair.

    The entry being repaired is left out of the scan the way the edited entry
    is -- it is the one whose answer is being rewritten -- so what refuses
    here is genuinely another configuration.
    """
    codeless = register_codeless_lock(hass)
    _sibling_declaring(hass, codeless, declares=False)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_USERS: {"User 1": {CONF_ENABLED: True, CONF_PIN: "1234"}},
            CONF_SLOT_ASSIGNMENT: {"user 1": 1},
        },
    )
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass, context={"lock_entity_id": LOCK_1_ENTITY_ID})
    await hass.async_block_till_done()
    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})

    await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [codeless.entity_id]}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "codeless_confirm"}
    )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_LOCKS: "codeless_conflict"}
    # The repair did not land, so the entry still holds the lock it came in
    # with rather than one two configurations disagree about.
    assert entry.data[CONF_LOCKS] == [LOCK_1_ENTITY_ID]


async def test_a_configuration_that_does_not_manage_the_lock_has_no_say(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Only entries that HOLD the member answer for it.

    A declaration left behind by an entry that no longer lists the lock is
    not an opinion about anything -- nothing resolves it -- so reading one
    would refuse an answer no live provider contradicts, and a lock nothing
    else manages could never be declared while any other configuration
    existed.
    """
    codeless = register_codeless_lock(hass)
    elsewhere = MockConfigEntry(
        domain=DOMAIN,
        title="Garage Codes",
        unique_id="garage_codes",
        data={
            CONF_LOCKS: [LOCK_2_ENTITY_ID],
            CONF_USERS: {"Ada": {CONF_PIN: "1111", CONF_ENABLED: True}},
            CONF_SLOT_ASSIGNMENT: {"ada": 9},
        },
    )
    elsewhere.add_to_hass(hass)
    flow_id = await _init_flow_to_user_step(hass)

    await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "test", CONF_LOCKS: [codeless.entity_id]}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_confirm"}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"
