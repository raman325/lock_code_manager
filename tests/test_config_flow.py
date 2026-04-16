"""Config flow tests."""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.config_flow import (
    LockCodeManagerFlowHandler,
    _async_get_all_codes,
)
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    DOMAIN,
)
from custom_components.lock_code_manager.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
)
from custom_components.lock_code_manager.models import SlotCode

from .common import BASE_CONFIG, LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID

GET_ALL_CODES_PATCH = (
    "custom_components.lock_code_manager.config_flow._async_get_all_codes"
)


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

    with patch(GET_ALL_CODES_PATCH, return_value=({}, {})):
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


async def _init_flow_to_user_step(hass: HomeAssistant) -> str:
    """Initialize a config flow and return the flow ID at the user step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    return result["flow_id"]


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


# --- Existing-codes confirmation step tests ---


async def test_ui_existing_codes_confirm_clear(hass: HomeAssistant):
    """UI path: existing codes detected -> confirm -> clear -> create entry."""
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

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    assert result["step_id"] == "ui"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    # Should show the confirmation menu
    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    assert result["description_placeholders"]["slots"] == "1"

    # Confirm clearing — should proceed to slot config
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_clear"}
    )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert result["description_placeholders"]["slot_num"] == 1

    # Configure slot 1
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "1234"}
    )

    assert result["step_id"] == "code_slot"
    mock_clear.assert_not_called()

    # Configure slot 2 -> create entry and clear deferred
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_ENABLED: True, CONF_PIN: "5678"}
    )

    assert result["type"] == "create_entry"
    mock_clear.assert_called_once_with(1, source="direct")


async def test_ui_existing_codes_confirm_cancel(hass: HomeAssistant):
    """UI path: existing codes detected -> confirm -> cancel -> abort."""
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
        flow_id, {CONF_NUM_SLOTS: 2, CONF_START_SLOT: 1}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"

    # Cancel -> abort, no clear
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_cancel"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "existing_codes_cancelled"
    mock_clear.assert_not_called()


async def test_yaml_existing_codes_confirm_clear(hass: HomeAssistant):
    """YAML path: existing codes detected -> confirm -> clear -> create entry."""
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
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    assert result["description_placeholders"]["slots"] == "1"

    # Confirm clearing -> create entry and clear
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_clear"}
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"
    mock_clear.assert_called_once_with(1, source="direct")


async def test_yaml_existing_codes_confirm_cancel(hass: HomeAssistant):
    """YAML path: existing codes detected -> confirm -> cancel -> abort."""
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
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"

    # Cancel -> abort, no clear
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_cancel"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "existing_codes_cancelled"
    mock_clear.assert_not_called()


async def test_ui_no_existing_codes_skips_confirm(hass: HomeAssistant):
    """UI path: no existing codes -> skip confirm step entirely."""
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
        flow_id, {CONF_NUM_SLOTS: 1, CONF_START_SLOT: 1}
    )

    # Goes directly to code_slot, no confirm step
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_yaml_no_existing_codes_skips_confirm(hass: HomeAssistant):
    """YAML path: no existing codes -> create_entry directly without confirm."""
    with patch(GET_ALL_CODES_PATCH, return_value=({}, {})):
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

    # Goes directly to create_entry, no confirm step
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"


async def test_ui_existing_codes_confirm_lists_multiple_slots(hass: HomeAssistant):
    """Confirm step shows all slots with existing codes, sorted."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = AsyncMock()
    mock_lock.async_internal_clear_usercode = mock_clear
    existing = {LOCK_1_ENTITY_ID: {3: "1234", 1: "5678"}}
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
        flow_id, {CONF_NUM_SLOTS: 5, CONF_START_SLOT: 1}
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    # Slots are sorted in the placeholder
    assert result["description_placeholders"]["slots"] == "1, 3"


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


async def test_async_get_all_codes_provider_failure_logs_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """Provider raising LockCodeManagerProviderError logs WARNING (not DEBUG).

    Distinguishes a real failure (LockDisconnected, a provider error) from
    the expected setup-time skip cases (missing entity / unsupported
    platform / missing config entry) so users see actionable signal when a
    lock is unreachable.
    """
    mock_instance = MagicMock()
    mock_instance.async_internal_get_usercodes = AsyncMock(
        side_effect=LockDisconnected("lock offline")
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
        caplog.at_level("WARNING"),
    ):
        result, instances = await _async_get_all_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    assert result == {}
    assert instances == {}
    # Surfaced at WARNING (not DEBUG): failure should be visible in logs
    assert any(
        record.levelname == "WARNING" and LOCK_1_ENTITY_ID in record.message
        for record in caplog.records
    )


async def test_async_get_all_codes_bare_base_error_logs_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """Defensive: a provider raising the bare base LockCodeManagerError still warns.

    All in-tree providers raise LockCodeManagerProviderError, but a third-party
    or not-yet-migrated provider could raise the bare base. We catch and warn
    rather than letting it fall through to the generic Exception arm (which
    would log a confusing traceback for what is really a known failure mode).
    """
    mock_instance = MagicMock()
    mock_instance.async_internal_get_usercodes = AsyncMock(
        side_effect=LockCodeManagerError("bare base")
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
        caplog.at_level("WARNING"),
    ):
        result, instances = await _async_get_all_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    assert result == {}
    assert instances == {}
    # Should be a clean WARNING (no traceback), since this is a known
    # failure mode — not the generic Exception arm
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        LOCK_1_ENTITY_ID in r.message and "bare base" in r.message for r in warnings
    )
    assert all(r.exc_info is None for r in warnings)


async def test_async_get_all_codes_returns_all_codes(hass: HomeAssistant):
    """Test _async_get_all_codes returns every slot the lock reports.

    Filtering empty slots is the caller's responsibility, so this function
    must not drop them.
    """
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

    # _async_get_all_codes returns ALL codes — including empty slots.
    # Callers (e.g. _slots_with_existing_codes) filter as needed.
    assert LOCK_1_ENTITY_ID in result
    assert result[LOCK_1_ENTITY_ID] == {1: "1234", 3: "9999", 4: SlotCode.EMPTY}
    assert LOCK_1_ENTITY_ID in instances


async def test_async_get_all_codes_entity_not_in_registry(hass: HomeAssistant):
    """Test _async_get_all_codes skips locks not in the entity registry."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    result, instances = await _async_get_all_codes(
        hass, dev_reg, ent_reg, ["lock.does_not_exist"]
    )

    assert result == {}
    assert instances == {}


async def test_async_get_all_codes_unsupported_platform(hass: HomeAssistant):
    """Test _async_get_all_codes skips locks on unsupported platforms."""
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "lock", "unsupported_platform", "test_lock_1", suggested_object_id="test_1"
    )
    dev_reg = dr.async_get(hass)

    result, instances = await _async_get_all_codes(
        hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
    )

    assert result == {}
    assert instances == {}


async def test_async_get_all_codes_missing_lock_config_entry(hass: HomeAssistant):
    """Test _async_get_all_codes skips locks whose config entry is missing."""
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "lock", "zwave_js", "test_lock_1", suggested_object_id="test_1"
    )
    dev_reg = dr.async_get(hass)

    with (
        patch(
            "custom_components.lock_code_manager.config_flow.INTEGRATIONS_CLASS_MAP",
            {"zwave_js": MagicMock()},
        ),
        patch.object(hass.config_entries, "async_get_entry", return_value=None),
    ):
        result, instances = await _async_get_all_codes(
            hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID]
        )

    assert result == {}
    assert instances == {}


async def test_clear_existing_slot_handles_failures(hass: HomeAssistant):
    """Test that clearing failures and missing instances are handled gracefully."""
    mock_clear_ok = AsyncMock(return_value=True)
    mock_clear_fail = AsyncMock(side_effect=RuntimeError("clear failed"))
    mock_lock_ok = MagicMock()
    mock_lock_ok.async_internal_clear_usercode = mock_clear_ok
    mock_lock_fail = MagicMock()
    mock_lock_fail.async_internal_clear_usercode = mock_clear_fail

    # Locks: one without an instance (skipped), one OK, one that fails,
    # one whose slot 1 is reported as EMPTY (must not be cleared), and one
    # that doesn't have slot 1 at all (must not be cleared)
    existing = {
        "lock.no_instance": {1: "1111"},
        "lock.ok": {1: "2222"},
        "lock.fails": {1: "3333"},
        "lock.empty_slot": {1: SlotCode.EMPTY},
        "lock.different_slot": {2: "4444"},
    }
    instances = {
        "lock.ok": mock_lock_ok,
        "lock.fails": mock_lock_fail,
        "lock.empty_slot": MagicMock(),
        "lock.different_slot": MagicMock(),
    }

    with patch(GET_ALL_CODES_PATCH, return_value=(existing, instances)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "5678"}}},
    )

    # Confirm step shown because slot 1 has existing codes
    assert result["step_id"] == "existing_codes_confirm"
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_clear"}
    )

    assert result["type"] == "create_entry"
    # OK lock cleared, failing lock attempted (exception swallowed),
    # no_instance skipped, empty_slot skipped (no code to clear),
    # different_slot not touched (slot 1 not present)
    mock_clear_ok.assert_called_once_with(1, source="direct")
    mock_clear_fail.assert_called_once_with(1, source="direct")
    instances["lock.empty_slot"].async_internal_clear_usercode.assert_not_called()
    instances["lock.different_slot"].async_internal_clear_usercode.assert_not_called()


async def test_existing_codes_clear_without_next_step_aborts(hass: HomeAssistant):
    """Defensive: clear step aborts if _next_step was never assigned."""
    handler = LockCodeManagerFlowHandler()
    handler.hass = hass
    # _init_existing_codes_state ran in __init__; _next_step is None

    result = await handler.async_step_existing_codes_clear()

    assert result["type"] == "abort"
    assert result["reason"] == "unknown"


# --- Options flow tests ---


async def _start_options_flow(
    hass: HomeAssistant,
    *,
    locks: list[str] | None = None,
    slots: dict[int, dict] | None = None,
) -> tuple[str, MockConfigEntry]:
    """Create an options flow and return (flow_id, entry).

    Mirrors the existing config-flow test helpers: keeps the entry creation
    and options-flow init in one place so the individual tests stay focused
    on the behavior being checked.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="test",
        data={
            CONF_LOCKS: locks or [LOCK_1_ENTITY_ID],
            CONF_SLOTS: slots or {1: {CONF_ENABLED: True, CONF_PIN: "1234"}},
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    return result["flow_id"], entry


async def test_options_flow_no_added_pairs_persists_immediately(hass: HomeAssistant):
    """No new (lock, slot) pairs -> skip scan and confirm step entirely."""
    flow_id, _ = await _start_options_flow(hass)

    # Submit the same locks/slots that already exist on the entry — no diff
    with patch(GET_ALL_CODES_PATCH) as mock_get_codes:
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}},
            },
        )

    assert result["type"] == "create_entry"
    # Critical: no lock query should happen when there's nothing new to check
    mock_get_codes.assert_not_called()


async def test_options_flow_added_pair_no_existing_code_persists(hass: HomeAssistant):
    """New (lock, slot) added but lock has no code there -> persist directly."""
    flow_id, _ = await _start_options_flow(hass)

    # Adding slot 2; lock has nothing in slot 2 (only slot 1)
    with patch(
        GET_ALL_CODES_PATCH,
        return_value=({LOCK_1_ENTITY_ID: {1: "1234"}}, {LOCK_1_ENTITY_ID: MagicMock()}),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "create_entry"


async def test_options_flow_added_pair_with_existing_code_confirm_clear(
    hass: HomeAssistant,
):
    """New (lock, slot) added and lock has code there -> confirm -> clear -> persist."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock = MagicMock()
    mock_lock.async_internal_clear_usercode = mock_clear

    flow_id, _ = await _start_options_flow(hass)

    # Adding slot 2; lock already has "9999" in slot 2 (and our managed "1234"
    # in slot 1 — slot 1 is NOT in added_pairs so it must not be cleared)
    with patch(
        GET_ALL_CODES_PATCH,
        return_value=(
            {LOCK_1_ENTITY_ID: {1: "1234", 2: "9999"}},
            {LOCK_1_ENTITY_ID: mock_lock},
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    assert result["description_placeholders"]["slots"] == "2"

    # Confirm -> entry is updated AND only the newly-added slot (2) is cleared.
    # The pre-existing managed slot 1 must NOT be cleared even though it has a
    # non-empty code in _all_codes — we scoped to added pairs only.
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "existing_codes_clear"}
    )
    assert result["type"] == "create_entry"
    mock_clear.assert_called_once_with(2, source="direct")


async def test_options_flow_added_lock_with_existing_code_confirm_clear(
    hass: HomeAssistant,
):
    """A NEW lock (with codes in already-managed slot) triggers the confirm step."""
    mock_clear = AsyncMock(return_value=True)
    mock_lock_2 = MagicMock()
    mock_lock_2.async_internal_clear_usercode = mock_clear

    flow_id, _ = await _start_options_flow(hass)

    # Add LOCK_2 to the entry. Slot 1 was already managed for LOCK_1, but
    # the (LOCK_2, 1) pair is new -- and LOCK_2 happens to already have a
    # code in slot 1. The mixin should detect this and prompt.
    with patch(
        GET_ALL_CODES_PATCH,
        return_value=(
            {LOCK_2_ENTITY_ID: {1: "5555"}},
            {LOCK_2_ENTITY_ID: mock_lock_2},
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
                CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}},
            },
        )

    assert result["step_id"] == "existing_codes_confirm"
    assert result["description_placeholders"]["slots"] == "1"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "existing_codes_clear"}
    )
    assert result["type"] == "create_entry"
    mock_clear.assert_called_once_with(1, source="direct")


async def test_options_flow_existing_codes_cancel_aborts(hass: HomeAssistant):
    """Cancel from the confirm step aborts and does not change anything."""
    mock_clear = AsyncMock()
    mock_lock = MagicMock()
    mock_lock.async_internal_clear_usercode = mock_clear

    flow_id, _ = await _start_options_flow(hass)

    with patch(
        GET_ALL_CODES_PATCH,
        return_value=(
            {LOCK_1_ENTITY_ID: {2: "9999"}},
            {LOCK_1_ENTITY_ID: mock_lock},
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["step_id"] == "existing_codes_confirm"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "existing_codes_cancel"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "existing_codes_cancelled"
    mock_clear.assert_not_called()


async def test_options_flow_added_pair_empty_code_persists(hass: HomeAssistant):
    """Lock reports the new slot as EMPTY -> no confirm needed, persist."""
    flow_id, _ = await _start_options_flow(hass)

    with patch(
        GET_ALL_CODES_PATCH,
        return_value=(
            {LOCK_1_ENTITY_ID: {2: SlotCode.EMPTY}},
            {LOCK_1_ENTITY_ID: MagicMock()},
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "create_entry"


async def test_options_flow_invalid_yaml_shows_error(hass: HomeAssistant):
    """Validation error in the YAML keeps the form open with the error."""
    flow_id, _ = await _start_options_flow(hass)

    with patch(GET_ALL_CODES_PATCH) as mock_get_codes:
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                # Missing required PIN with enabled=True is invalid per schema
                CONF_SLOTS: {"not_an_int": {}},
            },
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_config"}
    mock_get_codes.assert_not_called()
