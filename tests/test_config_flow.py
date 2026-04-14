"""Config flow tests."""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.config_flow import _async_get_all_codes
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    DOMAIN,
)
from custom_components.lock_code_manager.models import SlotCode

from .common import BASE_CONFIG, LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID


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

    with patch(
        "custom_components.lock_code_manager.config_flow._async_get_all_codes",
        return_value=({}, {}),
    ):
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
        flow_id, {CONF_NUM_SLOTS: 4, CONF_START_SLOT: 1}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert not result["last_step"]
    assert result["description_placeholders"]["slot_num"] == 1

    return flow_id


async def _start_yaml_config_flow(hass: HomeAssistant):
    """Start a YAML based config flow."""
    flow_id = await _start_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"

    return flow_id


async def test_config_flow_ui(hass: HomeAssistant):
    """Test UI based config flow with slot number incrementing correctly."""
    flow_id = await _start_ui_config_flow(hass)

    pins = ["1234", "5678", "9012", "3456"]
    for i, pin in enumerate(pins):
        slot_num = i + 1
        is_last = slot_num == len(pins)

        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_ENABLED: True, CONF_PIN: pin}
        )

        if is_last:
            assert result["type"] == "create_entry"
        else:
            assert result["type"] == "form"
            assert result["step_id"] == "code_slot"
            assert result["last_step"] == (slot_num == len(pins) - 1)
            assert result["description_placeholders"]["slot_num"] == slot_num + 1

    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_ENABLED: True, CONF_PIN: "1234"},
            2: {CONF_ENABLED: True, CONF_PIN: "5678"},
            3: {CONF_ENABLED: True, CONF_PIN: "9012"},
            4: {CONF_ENABLED: True, CONF_PIN: "3456"},
        },
    }


async def test_config_flow_ui_error(hass: HomeAssistant):
    """Test error in UI based config flow."""
    flow_id = await _start_ui_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: ""}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert not result["last_step"]
    assert result["errors"] == {CONF_PIN: "missing_pin_if_enabled"}


async def test_config_flow_yaml(hass: HomeAssistant):
    """Test YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_SLOTS: {
                1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                2: {CONF_ENABLED: True, CONF_PIN: "5678"},
            }
        },
    )

    assert result["type"] == "create_entry"
    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_ENABLED: True, CONF_PIN: "1234"},
            2: {CONF_ENABLED: True, CONF_PIN: "5678"},
        },
    }


async def test_config_flow_yaml_error(hass: HomeAssistant):
    """Test error handling in YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: ""}}},
    )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"
    assert result["errors"] == {"base": "invalid_config"}


async def test_options_flow(hass: HomeAssistant):
    """Test options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][3] = {CONF_ENABLED: True, CONF_PIN: ""}
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

    new_config[CONF_SLOTS][3] = {CONF_ENABLED: True, CONF_PIN: "1234"}
    result = await hass.config_entries.options.async_configure(
        flow_id, user_input=new_config
    )

    assert result["type"] == "create_entry"
    assert result["data"] == new_config


async def test_config_flow_reauth(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test reauth flow for updating locks."""
    lock_code_manager_config_entry.async_start_reauth(
        hass, context={"lock_entity_id": LOCK_1_ENTITY_ID}
    )
    await hass.async_block_till_done()
    flows = [
        flow
        for flow in lock_code_manager_config_entry.async_get_active_flows(
            hass, {SOURCE_REAUTH}
        )
    ]
    assert len(flows) == 1
    [result] = flows

    assert result["step_id"] == "reauth"
    flow_id = result["flow_id"]

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "locks_updated"


async def test_config_flow_slots_already_configured(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test slots already configured error."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {2: {CONF_ENABLED: False, CONF_PIN: "0123"}}},
    )
    assert result["errors"] == {"base": "slots_already_configured"}


async def test_config_flow_two_entries_same_locks(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test two entries that use same locks but different slots set up successfully."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {3: {CONF_ENABLED: False, CONF_PIN: "0123"}}},
    )
    assert result["type"] == "create_entry"


async def test_config_flow_ui_scheduler_entity_excluded(hass: HomeAssistant):
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
        {CONF_ENABLED: True, CONF_PIN: "1234", CONF_ENTITY_ID: "switch.my_schedule"},
    )

    # Should show error for excluded platform
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert result["errors"] == {CONF_ENTITY_ID: "excluded_platform"}
    # Verify placeholder is set for the error message
    assert result["description_placeholders"].get("integration") == "scheduler"


# --- Per-slot existing code handling tests ---

GET_ALL_CODES_PATCH = (
    "custom_components.lock_code_manager.config_flow._async_get_all_codes"
)


async def _init_flow_to_user_step(hass: HomeAssistant) -> str:
    """Initialize a config flow and return the flow ID at the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    return result["flow_id"]


# --- UI path tests ---


async def test_code_slot_readable_code_prefilled(hass: HomeAssistant):
    """Test that a readable existing code is prefilled and message shown."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {1: "1234"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"

    # Go to UI path
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    assert result["step_id"] == "ui"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    assert result["step_id"] == "code_slot"
    assert (
        "detected and prefilled"
        in result["description_placeholders"]["existing_code_msg"]
    )

    # Submit slot 1 — clear is deferred until entry creation
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "1234"}
    )

    assert result["step_id"] == "code_slot"
    mock_clear.assert_not_called()

    # Submit slot 2 to complete flow and trigger deferred clear
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "5678"}
    )

    assert result["type"] == "create_entry"
    mock_clear.assert_called_once_with(1, source="direct")


async def test_code_slot_unreadable_code_message(hass: HomeAssistant):
    """Test that an unreadable existing code shows appropriate message."""
    existing = {LOCK_1_ENTITY_ID: {1: SlotCode.UNKNOWN}}
    lock_instances = {LOCK_1_ENTITY_ID: AsyncMock()}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    assert result["step_id"] == "code_slot"
    assert (
        "could not be read" in result["description_placeholders"]["existing_code_msg"]
    )


async def test_code_slot_conflict_code_message(hass: HomeAssistant):
    """Test that conflicting codes across locks shows conflict message."""
    existing = {
        LOCK_1_ENTITY_ID: {1: "1234"},
        LOCK_2_ENTITY_ID: {1: "5678"},
    }
    lock_instances = {
        LOCK_1_ENTITY_ID: AsyncMock(),
        LOCK_2_ENTITY_ID: AsyncMock(),
    }

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    assert result["step_id"] == "code_slot"
    assert "Different PINs" in result["description_placeholders"]["existing_code_msg"]


async def test_code_slot_no_existing_code(hass: HomeAssistant):
    """Test that no existing code results in empty message."""
    with patch(GET_ALL_CODES_PATCH, return_value=({}, {})):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    assert result["step_id"] == "code_slot"
    assert result["description_placeholders"]["existing_code_msg"] == ""


async def test_code_slot_auto_clear_skips_messages(hass: HomeAssistant):
    """Test that auto_clear_existing skips existing code messages and clears on entry creation."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {1: "1234"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_NUM_SLOTS: 1, CONF_START_SLOT: 1, "auto_clear_existing": True},
    )

    assert result["step_id"] == "code_slot"
    # With auto_clear, no existing code message shown
    assert result["description_placeholders"]["existing_code_msg"] == ""

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "5678"}
    )

    assert result["type"] == "create_entry"
    # Existing code should have been auto-cleared after entry creation
    mock_clear.assert_called_once_with(1, source="direct")


async def test_yaml_auto_clear_skips_review(hass: HomeAssistant):
    """Test that auto_clear_existing in YAML path skips slot review."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {1: "9999"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}},
            "auto_clear_existing": True,
        },
    )

    # Should go directly to create_entry, no yaml_slot_review
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"
    mock_clear.assert_called_once_with(1, source="direct")


# --- YAML path tests ---


async def test_yaml_slot_review_adopt_readable(hass: HomeAssistant):
    """Test that adopting a readable code in YAML review updates the slot config."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {1: "9999"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    assert result["step_id"] == "yaml"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: False, CONF_PIN: "0000"}}},
    )

    assert result["step_id"] == "yaml_slot_review"
    assert "detected" in result["description_placeholders"]["existing_code_msg"]

    # Adopt the existing PIN
    result = await hass.config_entries.flow.async_configure(flow_id, {"adopt": True})

    assert result["type"] == "create_entry"
    # PIN should be adopted from the existing code
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "9999"
    assert result["data"][CONF_SLOTS][1][CONF_ENABLED] is True
    mock_clear.assert_called_once_with(1, source="direct")


async def test_yaml_slot_review_skip_readable(hass: HomeAssistant):
    """Test that declining adoption keeps the original YAML PIN."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {1: "9999"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "0000"}}},
    )

    assert result["step_id"] == "yaml_slot_review"

    # Decline adoption
    result = await hass.config_entries.flow.async_configure(flow_id, {"adopt": False})

    assert result["type"] == "create_entry"
    # Original YAML PIN should be kept
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "0000"
    # Clear should still be called (user is taking over the slot)
    mock_clear.assert_called_once_with(1, source="direct")


async def test_yaml_no_review_when_no_existing_codes(hass: HomeAssistant):
    """Test that YAML flow goes directly to create_entry with no existing codes."""
    with patch(GET_ALL_CODES_PATCH, return_value=({}, {})):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    assert result["step_id"] == "yaml"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"


async def test_yaml_slot_review_unreadable(hass: HomeAssistant):
    """Test YAML review with unreadable code shows info and clears."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {1: SlotCode.UNKNOWN}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    assert result["step_id"] == "yaml_slot_review"
    assert (
        "could not be read" in result["description_placeholders"]["existing_code_msg"]
    )

    # Submit empty form (no adopt option for unreadable)
    result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"
    mock_clear.assert_called_once_with(1, source="direct")


async def test_yaml_slot_review_conflict(hass: HomeAssistant):
    """Test YAML review with conflicting codes across locks."""
    mock_clear_1 = AsyncMock(return_value=True)
    mock_clear_2 = AsyncMock(return_value=True)
    mock_lock_1 = AsyncMock()
    mock_lock_1.async_internal_clear_usercode = mock_clear_1
    mock_lock_2 = AsyncMock()
    mock_lock_2.async_internal_clear_usercode = mock_clear_2
    existing = {
        LOCK_1_ENTITY_ID: {1: "1234"},
        LOCK_2_ENTITY_ID: {1: "5678"},
    }
    lock_instances = {
        LOCK_1_ENTITY_ID: mock_lock_1,
        LOCK_2_ENTITY_ID: mock_lock_2,
    }

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "0000"}}},
    )

    assert result["step_id"] == "yaml_slot_review"
    assert "Different PINs" in result["description_placeholders"]["existing_code_msg"]

    result = await hass.config_entries.flow.async_configure(flow_id, {})

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "0000"
    mock_clear_1.assert_called_once_with(1, source="direct")
    mock_clear_2.assert_called_once_with(1, source="direct")


async def test_code_slot_mixed_readable_unreadable(hass: HomeAssistant):
    """Test that mixed readable+unreadable across locks returns conflict status."""
    existing = {
        LOCK_1_ENTITY_ID: {1: "1234"},
        LOCK_2_ENTITY_ID: {1: SlotCode.UNKNOWN},
    }
    lock_instances = {
        LOCK_1_ENTITY_ID: AsyncMock(),
        LOCK_2_ENTITY_ID: AsyncMock(),
    }

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 1, CONF_START_SLOT: 1}
    )

    assert result["step_id"] == "code_slot"
    assert "Different PINs" in result["description_placeholders"]["existing_code_msg"]


# --- _async_get_all_codes tests ---


async def test_async_get_all_codes_exception(hass: HomeAssistant):
    """Test _async_get_all_codes catches exception from usercodes fetch."""
    mock_instance = MagicMock()
    mock_instance.async_internal_get_usercodes = AsyncMock(
        side_effect=RuntimeError("node not ready")
    )
    mock_lock_cls = MagicMock(return_value=mock_instance)

    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "lock", "zwave_js", "test_lock_1", suggested_object_id="test_1"
    )
    dev_reg = dr.async_get(hass)

    with (
        patch(
            "custom_components.lock_code_manager.config_flow.INTEGRATIONS_CLASS_MAP",
            {"zwave_js": mock_lock_cls},
        ),
        patch.object(
            hass.config_entries,
            "async_get_entry",
            return_value=MockConfigEntry(domain="zwave_js"),
        ),
    ):
        result, instances = await _async_get_all_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    # Exception should be caught; result should be empty
    assert result == {}
    assert instances == {}


async def test_async_get_all_codes_returns_all_non_empty(hass: HomeAssistant):
    """Test _async_get_all_codes returns all non-empty codes including managed ones."""
    mock_instance = MagicMock()
    mock_instance.async_internal_get_usercodes = AsyncMock(
        return_value={1: "1234", 3: "9999", 4: SlotCode.EMPTY}
    )
    mock_lock_cls = MagicMock(return_value=mock_instance)

    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "lock", "zwave_js", "test_lock_1", suggested_object_id="test_1"
    )
    dev_reg = dr.async_get(hass)

    # Create an existing Lock Code Manager config entry that manages slot 1
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234", CONF_NAME: "S1"}},
        },
    )
    lcm_entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.lock_code_manager.config_flow.INTEGRATIONS_CLASS_MAP",
            {"zwave_js": mock_lock_cls},
        ),
        patch.object(
            hass.config_entries,
            "async_get_entry",
            return_value=MockConfigEntry(domain="zwave_js"),
        ),
    ):
        result, instances = await _async_get_all_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    # _async_get_all_codes returns ALL non-empty codes (not filtered by managed status)
    # Slot 4 is empty, so only slots 1 and 3 should be returned
    assert LOCK_1_ENTITY_ID in result
    assert result[LOCK_1_ENTITY_ID] == {1: "1234", 3: "9999"}
    assert LOCK_1_ENTITY_ID in instances
