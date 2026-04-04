"""Config flow tests."""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.config_flow import _async_get_unmanaged_codes
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
    """Test YAML based config flow."""
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
    """Test UI based config flow."""
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


# --- Lock reset step tests ---

LOCK_RESET_PATCH = (
    "custom_components.lock_code_manager.config_flow._async_get_unmanaged_codes"
)


async def _init_flow_to_user_step(hass: HomeAssistant) -> str:
    """Initialize a config flow and return the flow ID at the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    return result["flow_id"]


async def test_lock_reset_no_unmanaged_codes_skips(hass: HomeAssistant):
    """Test that lock reset step is skipped when no unmanaged codes exist."""
    with patch(LOCK_RESET_PATCH, return_value=({}, {})):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    # Should skip lock_reset and go straight to choose_path
    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"


async def test_lock_reset_readable_codes_clear(hass: HomeAssistant):
    """Test clearing unmanaged readable codes."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    unmanaged = {LOCK_1_ENTITY_ID: {3: "9999", 4: "8888"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

        assert result["type"] == "menu"
        assert result["step_id"] == "lock_reset"
        assert "lock_reset_clear" in result["menu_options"]
        assert "lock_reset_adopt" in result["menu_options"]

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_clear"}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"
    assert mock_clear.call_count == 2


async def test_lock_reset_readable_codes_adopt(hass: HomeAssistant):
    """Test adopting unmanaged readable codes creates slots in config."""
    unmanaged = {LOCK_1_ENTITY_ID: {3: "9999", 5: "7777"}}
    lock_instances = {LOCK_1_ENTITY_ID: AsyncMock()}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

        assert result["type"] == "menu"
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_adopt"}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"

    # Verify adopted slots by completing the flow through YAML
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "yaml"

    # Submit the adopted slots (they were pre-populated in flow data)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_SLOTS: {
                3: {CONF_ENABLED: True, CONF_PIN: "9999", CONF_NAME: "Slot 3"},
                5: {CONF_ENABLED: True, CONF_PIN: "7777", CONF_NAME: "Slot 5"},
            }
        },
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][3] == {
        CONF_ENABLED: True,
        CONF_PIN: "9999",
        CONF_NAME: "Slot 3",
    }
    assert result["data"][CONF_SLOTS][5] == {
        CONF_ENABLED: True,
        CONF_PIN: "7777",
        CONF_NAME: "Slot 5",
    }


async def test_lock_reset_masked_codes_only_clear_or_cancel(hass: HomeAssistant):
    """Test that only clear and cancel are shown for masked-only codes."""
    unmanaged = {LOCK_1_ENTITY_ID: {3: SlotCode.UNKNOWN, 4: SlotCode.UNKNOWN}}
    lock_instances = {LOCK_1_ENTITY_ID: AsyncMock()}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "lock_reset"
    assert "lock_reset_clear" in result["menu_options"]
    assert "lock_reset_cancel" in result["menu_options"]
    assert "lock_reset_adopt" not in result["menu_options"]


async def test_lock_reset_cancel_aborts(hass: HomeAssistant):
    """Test that cancel aborts the config flow."""
    unmanaged = {LOCK_1_ENTITY_ID: {3: "9999"}}
    lock_instances = {LOCK_1_ENTITY_ID: AsyncMock()}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

        assert result["type"] == "menu"
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_cancel"}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "lock_reset_cancelled"


async def test_lock_reset_mixed_codes_adopt_clears_masked(hass: HomeAssistant):
    """Test that adopt with mixed codes adopts readable and clears masked."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    unmanaged = {LOCK_1_ENTITY_ID: {3: "9999", 4: SlotCode.UNKNOWN}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

        assert result["type"] == "menu"
        assert result["step_id"] == "lock_reset"
        # Both adopt and clear should be available for mixed codes
        assert "lock_reset_adopt" in result["menu_options"]

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_adopt"}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"
    # Only the masked slot (4) should have been cleared
    mock_clear.assert_called_once_with(4, source="direct")


async def test_lock_reset_clear_missing_lock_instance(hass: HomeAssistant):
    """Test that clear skips locks without a lock_instance."""
    unmanaged = {
        LOCK_1_ENTITY_ID: {3: "9999"},
        LOCK_2_ENTITY_ID: {5: "1111"},
    }
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    # Only LOCK_1 has an instance; LOCK_2 is missing from lock_instances
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_clear"}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"
    # Only slot 3 on LOCK_1 should have been cleared; LOCK_2 was skipped
    mock_clear.assert_called_once_with(3, source="direct")


async def test_lock_reset_clear_exception_during_clear(hass: HomeAssistant):
    """Test that an exception during slot clearing is caught and logged."""
    mock_clear = AsyncMock(side_effect=RuntimeError("device unavailable"))
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    unmanaged = {LOCK_1_ENTITY_ID: {3: "9999"}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_clear"}
        )

    # Flow should continue to choose_path despite the exception
    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"
    mock_clear.assert_called_once_with(3, source="direct")


async def test_lock_reset_adopt_pin_conflict(hass: HomeAssistant):
    """Test that conflicting PINs across locks keep the first-seen PIN."""
    # Two locks have the same slot but different PINs
    unmanaged = {
        LOCK_1_ENTITY_ID: {3: "9999"},
        LOCK_2_ENTITY_ID: {3: "1111"},
    }
    lock_instances = {
        LOCK_1_ENTITY_ID: AsyncMock(),
        LOCK_2_ENTITY_ID: AsyncMock(),
    }

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_adopt"}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"

    # Verify adopted slots: only the first-seen PIN should be kept
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    assert result["step_id"] == "yaml"

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {3: {CONF_ENABLED: True, CONF_PIN: "9999", CONF_NAME: "Slot 3"}}},
    )
    assert result["type"] == "create_entry"
    # The first-seen PIN ("9999" from LOCK_1) should be kept
    assert result["data"][CONF_SLOTS][3][CONF_PIN] == "9999"


async def test_lock_reset_adopt_missing_lock_instance_for_masked(hass: HomeAssistant):
    """Test that masked slot clear is skipped when lock_instance is missing."""
    unmanaged = {
        LOCK_1_ENTITY_ID: {3: "9999"},
        LOCK_2_ENTITY_ID: {5: SlotCode.UNKNOWN},
    }
    # Only LOCK_1 has an instance; LOCK_2 masked slot should be skipped
    lock_instances = {LOCK_1_ENTITY_ID: AsyncMock()}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_adopt"}
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"


async def test_lock_reset_adopt_exception_clearing_masked(hass: HomeAssistant):
    """Test that exceptions while clearing a masked slot are caught."""
    mock_clear = AsyncMock(side_effect=RuntimeError("device unavailable"))
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    unmanaged = {LOCK_1_ENTITY_ID: {3: "9999", 4: SlotCode.UNKNOWN}}
    lock_instances = {LOCK_1_ENTITY_ID: mock_lock}

    with patch(LOCK_RESET_PATCH, return_value=(unmanaged, lock_instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        assert result["step_id"] == "lock_reset"

        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "lock_reset_adopt"}
        )

    # Flow should continue despite the exception during masked slot clear
    assert result["type"] == "menu"
    assert result["step_id"] == "choose_path"
    # The masked slot (4) clear was attempted and failed
    mock_clear.assert_called_once_with(4, source="direct")


async def test_async_get_unmanaged_codes_exception(hass: HomeAssistant):
    """Test _async_get_unmanaged_codes catches exception from usercodes fetch."""
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
        result, instances = await _async_get_unmanaged_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    # Exception should be caught; result should be empty
    assert result == {}
    assert instances == {}


async def test_async_get_unmanaged_codes_returns_unmanaged(hass: HomeAssistant):
    """Test _async_get_unmanaged_codes returns only unmanaged non-empty codes."""
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

    # Create an LCM config entry that manages slot 1 on this lock
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
        result, instances = await _async_get_unmanaged_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    # Slot 1 is managed by the LCM entry, slot 4 is empty
    # Only slot 3 should be returned as unmanaged
    assert LOCK_1_ENTITY_ID in result
    assert result[LOCK_1_ENTITY_ID] == {3: "9999"}
    assert LOCK_1_ENTITY_ID in instances
