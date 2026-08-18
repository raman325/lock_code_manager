"""Config flow tests."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.config_flow import (
    LockCodeManagerFlowHandler,
    _async_query_locks,
    _LockQuery,
    _LockQuerySkipped,
)
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)

from .common import BASE_CONFIG, LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID, MockLCMLock

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


def _answers(existing: dict[str, dict[int, SlotCredential]]):
    """Answer the lock query with these codes, for whichever locks are asked.

    Replaces a fixture that returned one fixed mapping regardless of the
    question. The flow now distinguishes "read, and empty" from "could not
    read", so the stub has to answer per lock rather than in aggregate.
    """

    async def _query(hass, dev_reg, ent_reg, lock_entity_ids):
        return [
            _LockQuery(
                lock_entity_id=lock,
                managed=True,
                credential_index_follows_slot=True,
                codes=existing.get(lock, {}),
            )
            for lock in lock_entity_ids
        ]

    return _query


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

    with patch(GET_ALL_CODES_PATCH, side_effect=_answers({})):
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


async def test_config_flow_yaml(hass: HomeAssistant):
    """Test YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            CONF_SLOTS: {
                1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"},
                2: {CONF_NAME: "User 2", CONF_ENABLED: True, CONF_PIN: "5678"},
            }
        },
    )

    assert result["type"] == "create_entry"
    assert result["title"] == "test"
    assert result["data"] == {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"},
            2: {CONF_NAME: "User 2", CONF_ENABLED: True, CONF_PIN: "5678"},
        },
    }


async def test_config_flow_yaml_error(hass: HomeAssistant):
    """Test error handling in YAML based config flow."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: ""}}},
    )

    assert result["type"] == "form"
    assert result["step_id"] == "yaml"
    assert result["errors"] == {"base": "invalid_config"}


async def test_options_flow(hass: HomeAssistant):
    """Test options flow."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    # The options flow now rejects the deprecated number_of_uses key, so build
    # the user-submitted config from scratch without it. Real entries have it
    # auto-stripped during migration before any options-flow interaction.
    new_config = {
        CONF_LOCKS: list(BASE_CONFIG[CONF_LOCKS]),
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {
                CONF_NAME: "test2",
                CONF_PIN: "5678",
                CONF_ENABLED: True,
                CONF_ENTITY_ID: "calendar.test_1",
            },
            3: {CONF_NAME: "User 3", CONF_ENABLED: True, CONF_PIN: ""},
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

    new_config[CONF_SLOTS][3] = {
        CONF_NAME: "User 1",
        CONF_ENABLED: True,
        CONF_PIN: "1234",
    }
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
    """Test slots already configured error."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {2: {CONF_NAME: "User 2", CONF_ENABLED: False, CONF_PIN: "0123"}}},
    )
    assert result["errors"] == {"base": "slots_already_configured"}


async def test_config_flow_two_entries_same_locks(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
):
    """Test two entries that use same locks but different slots set up successfully."""
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {3: {CONF_NAME: "User 3", CONF_ENABLED: False, CONF_PIN: "0123"}}},
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
            CONF_ENTITY_ID: "switch.my_schedule",
        },
    )

    # Should show error for excluded platform
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"
    assert result["errors"] == {CONF_ENTITY_ID: "excluded_platform"}
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
    existing = {
        LOCK_1_ENTITY_ID: {
            1: SlotCredential.known("1234"),
            2: SlotCredential.known("5678"),
        }
    }

    with patch(GET_ALL_CODES_PATCH, side_effect=_answers(existing)):
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
    assert result["type"] == "abort"
    assert result["reason"] == "occupancy_unknown"
    assert LOCK_1_ENTITY_ID in result["description_placeholders"]["locks"]


async def test_yaml_existing_codes_confirm_continue(hass: HomeAssistant):
    """YAML path: existing codes detected -> confirm -> continue -> create entry."""
    existing = {LOCK_1_ENTITY_ID: {1: SlotCredential.known("9999")}}

    with patch(GET_ALL_CODES_PATCH, side_effect=_answers(existing)):
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
        {CONF_SLOTS: {1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    assert "slot 1" in result["description_placeholders"]["details"]

    # Confirm -> create entry (sync manager handles reconciliation)
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_continue"}
    )

    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"


async def test_yaml_existing_codes_confirm_cancel(hass: HomeAssistant):
    """YAML path: existing codes detected -> confirm -> cancel -> abort."""
    existing = {LOCK_1_ENTITY_ID: {1: SlotCredential.known("9999")}}

    with patch(GET_ALL_CODES_PATCH, side_effect=_answers(existing)):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"

    # Cancel -> abort, no clear
    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_cancel"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "existing_codes_cancelled"


async def test_ui_no_existing_codes_skips_confirm(
    hass: HomeAssistant, mock_lock_config_entry
):
    """UI path: no existing codes -> skip confirm step entirely."""
    with patch(GET_ALL_CODES_PATCH, side_effect=_answers({})):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["step_id"] == "choose_path"

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "ui"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NUM_USERS: 1}
    )

    # Goes directly to code_slot, no confirm step
    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_yaml_no_existing_codes_skips_confirm(hass: HomeAssistant):
    """YAML path: no existing codes -> create_entry directly without confirm."""
    with patch(GET_ALL_CODES_PATCH, side_effect=_answers({})):
        flow_id = await _init_flow_to_user_step(hass)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "yaml"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_SLOTS: {1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}}},
    )

    # Goes directly to create_entry, no confirm step
    assert result["type"] == "create_entry"
    assert result["data"][CONF_SLOTS][1][CONF_PIN] == "1234"


async def test_yaml_existing_codes_confirm_lists_slots_sorted(hass: HomeAssistant):
    """The confirmation names every slot holding a code, in ascending order."""
    existing = {
        LOCK_1_ENTITY_ID: {
            3: SlotCredential.known("1234"),
            1: SlotCredential.known("5678"),
        }
    }

    with patch(GET_ALL_CODES_PATCH, side_effect=_answers(existing)):
        flow_id = await _init_flow_to_user_step(hass)
        await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "yaml"}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1111"},
                    3: {CONF_NAME: "User 3", CONF_ENABLED: True, CONF_PIN: "3333"},
                }
            },
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    details = result["description_placeholders"]["details"]
    assert details.index("slot 1") < details.index("slot 3")


# --- _async_get_all_codes tests ---


async def test_query_locks_exception(hass: HomeAssistant):
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
        [query] = await _async_query_locks(hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID])

    # Exception should be caught; result should be empty
    # The read FAILED. Reporting it as empty would let allocation issue a
    # number this lock may already hold a credential at.
    assert query.codes is None
    assert query.managed


async def test_query_locks_provider_failure_logs_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """
    Provider raising LockCodeManagerProviderError logs WARNING (not DEBUG).

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
        [query] = await _async_query_locks(hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID])

    # The read FAILED. Reporting it as empty would let allocation issue a
    # number this lock may already hold a credential at.
    assert query.codes is None
    assert query.managed
    # Surfaced at WARNING (not DEBUG): failure should be visible in logs
    assert any(
        record.levelname == "WARNING" and LOCK_1_ENTITY_ID in record.message
        for record in caplog.records
    )


async def test_query_locks_bare_base_error_logs_warning(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
):
    """
    Defensive: a provider raising the bare base LockCodeManagerError still warns.

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
        [query] = await _async_query_locks(hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID])

    # The read FAILED. Reporting it as empty would let allocation issue a
    # number this lock may already hold a credential at.
    assert query.codes is None
    assert query.managed
    # Should be a clean WARNING (no traceback), since this is a known
    # failure mode — not the generic Exception arm
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        LOCK_1_ENTITY_ID in r.message and "bare base" in r.message for r in warnings
    )
    assert all(r.exc_info is None for r in warnings)


async def test_query_locks_returns_all_codes(hass: HomeAssistant):
    """
    Test _async_query_locks returns every slot the lock reports.

    Filtering empty slots is the caller's responsibility, so this function
    must not drop them.
    """
    mock_instance = MagicMock()
    mock_instance.async_internal_get_usercodes = AsyncMock(
        return_value={
            1: SlotCredential.known("1234"),
            3: SlotCredential.known("9999"),
            4: SlotCredential.empty(),
        }
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
            CONF_SLOTS: {
                1: {
                    CONF_NAME: "S1",
                    CONF_ENABLED: True,
                    CONF_PIN: "1234",
                }
            },
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
        [query] = await _async_query_locks(hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID])

    # Every slot the lock reports, including empty ones: filtering is the
    # caller's job, and an empty slot is a slot allocation may use.
    assert query.lock_entity_id == LOCK_1_ENTITY_ID
    assert query.codes == {
        1: SlotCredential.known("1234"),
        3: SlotCredential.known("9999"),
        4: SlotCredential.empty(),
    }


async def test_query_locks_entity_not_in_registry(hass: HomeAssistant):
    """Test _async_get_all_codes skips locks not in the entity registry."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    [query] = await _async_query_locks(hass, dev_reg, ent_reg, ["lock.does_not_exist"])

    # Lock Code Manager still writes here -- the provider just could not be
    # built right now -- so the lock has to constrain allocation, and an
    # unread lock constrains it by making occupancy unknown.
    assert query.managed
    assert query.credential_index_follows_slot
    assert query.codes is None


async def test_query_locks_unsupported_platform(hass: HomeAssistant):
    """Test _async_get_all_codes skips locks on unsupported platforms."""
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "lock", "unsupported_platform", "test_lock_1", suggested_object_id="test_1"
    )
    dev_reg = dr.async_get(hass)

    [query] = await _async_query_locks(hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID])

    # Not a lock Lock Code Manager writes credentials to, so its contents
    # cannot collide with anything allocation issues.
    assert not query.managed
    assert query.codes is None


async def test_query_locks_missing_lock_config_entry(hass: HomeAssistant):
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
        [query] = await _async_query_locks(hass, dev_reg, ent_reg, [LOCK_1_ENTITY_ID])

    # Lock Code Manager still writes here -- the provider just could not be
    # built right now -- so the lock has to constrain allocation, and an
    # unread lock constrains it by making occupancy unknown.
    assert query.managed
    assert query.credential_index_follows_slot
    assert query.codes is None


async def test_existing_codes_detected_across_multiple_locks(hass: HomeAssistant):
    """One lock holding a code at the requested slot is enough to prompt.

    The other lock answers, and answers "empty" -- which is a real answer, not
    a missing one, and must not be reported as an existing code.
    """
    existing = {
        LOCK_1_ENTITY_ID: {1: SlotCredential.empty()},
        LOCK_2_ENTITY_ID: {1: SlotCredential.known("2222")},
    }
    with patch(GET_ALL_CODES_PATCH, side_effect=_answers(existing)):
        flow_id = await _init_flow_to_user_step(hass)
        await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_NAME: "test", CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]},
        )
        await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "yaml"}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "5678"}
                }
            },
        )

    assert result["step_id"] == "existing_codes_confirm"
    details = result["description_placeholders"]["details"]
    assert LOCK_2_ENTITY_ID in details
    assert LOCK_1_ENTITY_ID not in details

    result = await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "existing_codes_continue"}
    )

    # Nothing is cleared -- the sync manager reconciles from here.
    assert result["type"] == "create_entry"


async def test_existing_codes_continue_without_next_step_aborts(hass: HomeAssistant):
    """Defensive: continue step aborts if _next_step was never assigned."""
    handler = LockCodeManagerFlowHandler()
    handler.hass = hass
    # _init_existing_codes_state ran in __init__; _next_step is None

    result = await handler.async_step_existing_codes_continue()

    assert result["type"] == "abort"
    assert result["reason"] == "unknown"


# --- Options flow tests ---


async def _start_options_flow(
    hass: HomeAssistant,
    *,
    locks: list[str] | None = None,
    slots: dict[int, dict] | None = None,
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
            CONF_SLOTS: slots
            or {1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}},
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
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}
                },
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
        side_effect=_answers({LOCK_1_ENTITY_ID: {1: SlotCredential.known("1234")}}),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_NAME: "User 2", CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "create_entry"


async def test_options_flow_added_pair_with_existing_code_confirm(
    hass: HomeAssistant,
):
    """New (lock, slot) added and lock has code there -> confirm -> persist."""
    flow_id, _ = await _start_options_flow(hass)

    # Adding slot 2; lock already has "9999" in slot 2 (and our managed "1234"
    # in slot 1 — slot 1 is NOT in added_pairs so it must not be cleared)
    with patch(
        GET_ALL_CODES_PATCH,
        side_effect=_answers(
            {
                LOCK_1_ENTITY_ID: {
                    1: SlotCredential.known("1234"),
                    2: SlotCredential.known("9999"),
                }
            }
        ),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_NAME: "User 2", CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["type"] == "menu"
    assert result["step_id"] == "existing_codes_confirm"
    assert "slot 2" in result["description_placeholders"]["details"]

    # Confirm -> entry is updated. No slots are cleared by the config flow.
    # The pre-existing managed slot 1 is not affected even though it has a
    # non-empty code in _all_codes — we scoped to added pairs only.
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "existing_codes_continue"}
    )
    assert result["type"] == "create_entry"


async def test_options_flow_added_lock_with_existing_code_confirm(
    hass: HomeAssistant,
):
    """A NEW lock (with codes in already-managed slot) triggers the confirm step."""
    flow_id, _ = await _start_options_flow(hass)

    # Add LOCK_2 to the entry. Slot 1 was already managed for LOCK_1, but
    # the (LOCK_2, 1) pair is new -- and LOCK_2 happens to already have a
    # code in slot 1. The mixin should detect this and prompt.
    with patch(
        GET_ALL_CODES_PATCH,
        side_effect=_answers({LOCK_2_ENTITY_ID: {1: SlotCredential.known("5555")}}),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"}
                },
            },
        )

    assert result["step_id"] == "existing_codes_confirm"
    assert "slot 1" in result["description_placeholders"]["details"]

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "existing_codes_continue"}
    )
    assert result["type"] == "create_entry"


async def test_options_flow_existing_codes_cancel_aborts(hass: HomeAssistant):
    """Cancel from the confirm step aborts and does not change anything."""
    flow_id, _ = await _start_options_flow(hass)

    with patch(
        GET_ALL_CODES_PATCH,
        side_effect=_answers({LOCK_1_ENTITY_ID: {2: SlotCredential.known("9999")}}),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_NAME: "User 2", CONF_ENABLED: True, CONF_PIN: "5678"},
                },
            },
        )

    assert result["step_id"] == "existing_codes_confirm"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "existing_codes_cancel"}
    )

    assert result["type"] == "abort"
    assert result["reason"] == "existing_codes_cancelled"


async def test_options_flow_added_pair_empty_code_persists(hass: HomeAssistant):
    """Lock reports the new slot as EMPTY -> no confirm needed, persist."""
    flow_id, _ = await _start_options_flow(hass)

    with patch(
        GET_ALL_CODES_PATCH,
        side_effect=_answers({LOCK_1_ENTITY_ID: {2: SlotCredential.empty()}}),
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: "User 1", CONF_ENABLED: True, CONF_PIN: "1234"},
                    2: {CONF_NAME: "User 2", CONF_ENABLED: True, CONF_PIN: "5678"},
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


# Slot capacity validation (issue #1398)


def _capacity_probe(**capabilities_mock_kwargs):
    """
    Make the config flow able to probe the test lock's capacity.

    The autouse ``auto_setup_mock_lock`` fixture only registers MockLCMLock in
    the ``domain.locks`` provider map; the config flow builds its throwaway
    provider instance from its own map, so the test platform has to be
    registered there too or every capacity check silently skips.
    """
    return (
        patch.dict(
            "custom_components.lock_code_manager.config_flow.INTEGRATIONS_CLASS_MAP",
            {"test": MockLCMLock},
        ),
        patch.object(
            MockLCMLock,
            "async_get_capabilities",
            new_callable=AsyncMock,
            **capabilities_mock_kwargs,
        ),
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
    """A slot number the lock cannot hold is caught before the entry is created."""
    flow_id = await _start_yaml_config_flow(hass)

    probe_registered, probe_capabilities = _capacity_probe(
        return_value=_capabilities_with_slots(30)
    )
    with probe_registered, probe_capabilities:
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    50: {CONF_NAME: "User 50", CONF_ENABLED: True, CONF_PIN: "2222"}
                }
            },
        )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "slot_out_of_range"}
    assert result["description_placeholders"]["out_of_range_slots"] == "50"
    assert result["description_placeholders"]["num_slots"] == "30"


async def test_config_flow_yaml_accepts_slot_within_capacity(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A slot inside the advertised range still creates the entry."""
    flow_id = await _start_yaml_config_flow(hass)

    probe_registered, probe_capabilities = _capacity_probe(
        return_value=_capabilities_with_slots(30)
    )
    with probe_registered, probe_capabilities:
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    30: {CONF_NAME: "User 30", CONF_ENABLED: True, CONF_PIN: "2222"}
                }
            },
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
        "custom_components.lock_code_manager.config_flow._async_build_lock_instance",
        side_effect=_LockQuerySkipped(LOCK_1_ENTITY_ID, managed=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["type"] == "abort"
    assert result["reason"] == "occupancy_unknown"
    assert LOCK_1_ENTITY_ID in result["description_placeholders"]["locks"]


async def test_setup_ignores_a_lock_on_an_unsupported_platform(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A lock this integration will not write to cannot constrain numbering."""
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch(
        "custom_components.lock_code_manager.config_flow._async_build_lock_instance",
        side_effect=_LockQuerySkipped(LOCK_1_ENTITY_ID, managed=False),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_setup_reads_only_as_far_as_it_has_to(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The window starts at the number of users and widens by what is in the way.

    Locks that answer one index per round trip make the width of this read
    its cost, so a lock is never walked to its advertised capacity to place a
    handful of users.
    """
    windows: list[int] = []

    async def _read(self, slots=None):
        scope = list(self.managed_slots if slots is None else slots)
        windows.append(max(scope, default=0))
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
    # Asked about 2, found 2 in the way, asked about 4, found 3 in the way,
    # asked about 5. Never about the lock's whole capacity.
    assert windows == [2, 4, 5]


async def test_setup_does_not_widen_when_nothing_is_in_the_way(
    hass: HomeAssistant, mock_lock_config_entry
):
    """An empty lock costs exactly one read of exactly the users asked for."""
    windows: list[int] = []

    async def _read(self, slots=None):
        scope = list(self.managed_slots if slots is None else slots)
        windows.append(max(scope, default=0))
        return dict.fromkeys(scope, SlotCredential.empty())

    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    with patch.object(MockLCMLock, "async_get_usercodes", _read):
        await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 3})

    assert windows == [3]


async def test_config_flow_ui_rejects_more_users_than_the_lock_holds(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A count the locks cannot hold is refused where it can still be changed.

    Which users get configured does not change which numbers allocation
    issues, so the answer is known as soon as the count is.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    probe_registered, probe_capabilities = _capacity_probe(
        return_value=_capabilities_with_slots(2)
    )
    with probe_registered, probe_capabilities, _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 3}
        )

    # A form, not an abort: the user can lower the count and carry on.
    assert result["type"] == "form"
    assert result["step_id"] == "ui"
    assert result["errors"] == {"base": "too_many_users"}
    assert result["description_placeholders"]["num_users"] == "3"
    assert result["description_placeholders"]["room"] == "2"
    assert result["description_placeholders"]["taken"] == "0"

    with probe_registered, probe_capabilities, _holding():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 2}
        )

    assert result["type"] == "form"
    assert result["step_id"] == "code_slot"


async def test_config_flow_ui_room_accounts_for_codes_already_on_the_lock(
    hass: HomeAssistant, mock_lock_config_entry
):
    """The room offered is capacity minus what the lock already holds.

    Reporting raw capacity would tell someone with a nearly-full lock they
    can add far more users than they can.
    """
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    probe_registered, probe_capabilities = _capacity_probe(
        return_value=_capabilities_with_slots(3)
    )
    with probe_registered, probe_capabilities, _holding(1):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_NUM_USERS: 3}
        )

    assert result["errors"] == {"base": "too_many_users"}
    # Three slots, one already holding a code: room for two, not three.
    assert result["description_placeholders"]["num_slots"] == "3"
    assert result["description_placeholders"]["taken"] == "1"
    assert result["description_placeholders"]["room"] == "2"


async def test_config_flow_capacity_check_skipped_when_lock_unreachable(
    hass: HomeAssistant, mock_lock_config_entry
):
    """
    An unreachable lock must not block configuration.

    Capabilities need the lock awake, so a sleeping battery lock would
    otherwise make the flow unusable. The write-time check still covers it.
    """
    flow_id = await _start_yaml_config_flow(hass)

    probe_registered, probe_capabilities = _capacity_probe(
        side_effect=LockDisconnected("lock asleep")
    )
    with probe_registered, probe_capabilities:
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    50: {CONF_NAME: "User 50", CONF_ENABLED: True, CONF_PIN: "2222"}
                }
            },
        )

    assert result["type"] == "create_entry"


async def test_config_flow_capacity_check_skipped_when_capacity_unknown(
    hass: HomeAssistant, mock_lock_config_entry
):
    """``num_slots`` of 0 is "unknown", not "no slots", so it cannot reject."""
    flow_id = await _start_yaml_config_flow(hass)

    probe_registered, probe_capabilities = _capacity_probe(
        return_value=_capabilities_with_slots(0)
    )
    with probe_registered, probe_capabilities:
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    50: {CONF_NAME: "User 50", CONF_ENABLED: True, CONF_PIN: "2222"}
                }
            },
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
        patch.dict(
            "custom_components.lock_code_manager.config_flow.INTEGRATIONS_CLASS_MAP",
            {"test": MockLCMLock},
        ),
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
            {
                CONF_SLOTS: {
                    50: {CONF_NAME: "User 50", CONF_ENABLED: True, CONF_PIN: "2222"}
                }
            },
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

    probe_registered, probe_capabilities = _capacity_probe(
        side_effect=RuntimeError("provider blew up")
    )
    with probe_registered, probe_capabilities:
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                CONF_SLOTS: {
                    50: {CONF_NAME: "User 50", CONF_ENABLED: True, CONF_PIN: "2222"}
                }
            },
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
    ("slots", "expected_error"),
    [
        (
            {
                1: {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1234"},
                2: {CONF_NAME: " raman ", CONF_ENABLED: True, CONF_PIN: "5678"},
            },
            "name_not_unique",
        ),
    ],
)
async def test_config_flow_yaml_enforces_name_rules(
    hass: HomeAssistant, mock_lock_config_entry, slots, expected_error
):
    """The YAML path enforces the same name rules as the single-slot path.

    Without this the migration's repair could be undone by the very next
    submission, and the options flow -- the only way to edit slots after
    setup -- goes through the same validator.
    """
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_SLOTS: slots}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": expected_error}


async def test_config_flow_yaml_missing_name_says_so(
    hass: HomeAssistant, mock_lock_config_entry
):
    """A slots block predating the name requirement names the actual problem.

    Falling through to the generic invalid_config would send the user to the
    logs for the one failure we can predict.
    """
    flow_id = await _start_yaml_config_flow(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_SLOTS: {1: {CONF_ENABLED: True, CONF_PIN: "1234"}}}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "name_required"}
