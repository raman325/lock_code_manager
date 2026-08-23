"""Config flow tests."""

import json
from pathlib import Path
import re
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.config_flow import _check_common_slots
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
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
from custom_components.lock_code_manager.domain.queries import get_entry_config
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


async def test_config_flow_yaml_error(hass: HomeAssistant):
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

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(30))
    with probe_capabilities:
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

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(30))
    with probe_capabilities:
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

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(10))
    # Nine of the ten slots are taken, well past the window the count asks for.
    with probe_capabilities, _holding(*range(1, 10)):
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

    probe_capabilities = _capacity_probe(side_effect=LockDisconnected("lock asleep"))
    with probe_capabilities:
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

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(0))
    with probe_capabilities:
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

    probe_capabilities = _capacity_probe(side_effect=RuntimeError("provider blew up"))
    with probe_capabilities:
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
    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(4))
    flow_id = await _start_config_flow(hass)
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})

    # Three users fit in four slots; slots 1 and 2 are taken, so they would
    # land on 3, 4 and 5 -- and 5 does not exist.
    with probe_capabilities, _holding(1, 2):
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

    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(2))
    with probe_capabilities, _holding():
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
        build_lock_instance(hass, dr.async_get(hass), ent_reg, entity_id)

    assert raised.value.managed is managed


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
        build_lock_instance(hass, dr.async_get(hass), ent_reg, orphan.entity_id)

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
    probe_capabilities = _capacity_probe(return_value=_capabilities_with_slots(1))
    with probe_capabilities:
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )

    assert result["errors"] == {"base": "slot_out_of_range"}
    assert result["description_placeholders"]["num_slots"] == "1"
    assert result["description_placeholders"]["out_of_range_slots"] == "2"
