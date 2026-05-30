"""Tests for the per-slot entity coordinator."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.binary_sensor import (
    LockCodeManagerActiveEntity,
)
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.data import get_entry_config
from custom_components.lock_code_manager.slot_manager import (
    PinRequiredError,
    SlotEntityCoordinator,
)

from .common import (
    LOCK_1_ENTITY_ID,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_PIN_ENTITY,
)

_LOGGER = logging.getLogger(__name__)


async def test_coordinator_registered_for_each_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """One coordinator per configured slot is registered on runtime data."""
    coordinators = lock_code_manager_config_entry.runtime_data.slot_coordinators
    assert set(coordinators) == {1, 2}
    for slot_num, coordinator in coordinators.items():
        assert isinstance(coordinator, SlotEntityCoordinator)
        assert coordinator.slot_num == slot_num


async def test_coordinator_drives_active_view(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The active binary sensor renders the coordinator's derived state."""
    state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert state is not None
    assert state.state == STATE_ON
    # Slot 2 has a calendar condition; the calendar starts OFF, so slot 2 is inactive
    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state is not None
    assert state.state == STATE_OFF


async def test_request_pin_update_auto_disables_on_empty(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Clearing the PIN on an enabled slot disables the slot in one write."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]
    assert coordinator.is_enabled is True
    assert coordinator.pin_value == "1234"

    await coordinator.async_request_pin_update("")
    await hass.async_block_till_done()

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_PIN) == ""
    assert config.get(CONF_ENABLED) is False
    assert coordinator.is_enabled is False

    state = hass.states.get(SLOT_1_PIN_ENTITY)
    assert state is not None
    assert state.state == ""
    state = hass.states.get(SLOT_1_ENABLED_ENTITY)
    assert state is not None
    assert state.state == STATE_OFF


async def test_request_pin_update_normalizes_whitespace(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A whitespace-only PIN normalizes to empty and auto-disables the slot."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_pin_update("   ")
    await hass.async_block_till_done()

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_PIN) == ""
    assert config.get(CONF_ENABLED) is False


async def test_request_active_toggle_blocks_when_no_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Toggling active=True without a PIN raises and writes a repair issue."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    # Clear PIN first (auto-disables the slot)
    await coordinator.async_request_pin_update("")
    await hass.async_block_till_done()
    assert coordinator.is_enabled is False
    assert coordinator.pin_value is None

    with pytest.raises(PinRequiredError):
        await coordinator.async_request_active_toggle(True)

    issue_registry = async_get_issue_registry(hass)
    issue = issue_registry.async_get_issue(
        DOMAIN, f"pin_required_{lock_code_manager_config_entry.entry_id}_1"
    )
    assert issue is not None
    assert coordinator.is_enabled is False


async def test_request_active_toggle_clears_pin_required_issue(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Successful enable removes the pin_required repair issue."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_pin_update("")
    await hass.async_block_till_done()
    with pytest.raises(PinRequiredError):
        await coordinator.async_request_active_toggle(True)
    issue_registry = async_get_issue_registry(hass)
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"pin_required_{lock_code_manager_config_entry.entry_id}_1"
        )
        is not None
    )

    # Re-add a PIN and successfully enable; the repair issue must be cleared
    await coordinator.async_request_pin_update("4242")
    await hass.async_block_till_done()
    await coordinator.async_request_active_toggle(True)
    await hass.async_block_till_done()

    assert coordinator.is_enabled is True
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"pin_required_{lock_code_manager_config_entry.entry_id}_1"
        )
        is None
    )


async def test_switch_turn_on_raises_homeassistanterror_for_pin_required(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The switch entity translates PinRequiredError into HomeAssistantError."""
    # Disable slot 2 so we can test the enable path; clear its PIN to trigger
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    with pytest.raises(HomeAssistantError, match="Set a PIN code"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
            blocking=True,
        )


async def test_condition_entity_subscription_owned_by_coordinator(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """The coordinator owns the condition-entity subscription, not the entity."""
    hass.states.async_set("input_boolean.gate", STATE_ON)
    await hass.async_block_till_done()

    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "alice",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_ENTITY_ID: "input_boolean.gate",
            },
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Test Cond")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.slot_coordinators[1]
    assert coordinator.condition_entity_id == "input_boolean.gate"
    assert coordinator.is_active is True

    hass.states.async_set("input_boolean.gate", STATE_OFF)
    await hass.async_block_till_done()
    assert coordinator.is_active is False

    await hass.config_entries.async_unload(entry.entry_id)


async def test_coordinator_removed_on_slot_removal(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Removing a slot via options tears down its coordinator."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    assert 2 in runtime_data.slot_coordinators

    new_options = {
        CONF_LOCKS: list(runtime_data.locks.keys()),
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }
    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_options
    )
    await hass.async_block_till_done()

    assert 2 not in runtime_data.slot_coordinators
    assert 1 in runtime_data.slot_coordinators


async def test_unload_reads_slots_via_entry_config_view(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """async_unload_entry routes slot enumeration through EntryConfig.

    Reading via ``get_entry_config`` rather than ``config_entry.data``
    keeps the unload slot loop functional regardless of which side of
    the data/options migration last wrote the entry — covers the case
    where an update listener has just migrated data to options but the
    write-back has not run yet.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    removed_slots: list[int] = []
    original_invoke = runtime_data.callbacks.invoke_entity_removers_for_slot

    async def record_invoke(slot_num: int) -> None:
        removed_slots.append(slot_num)
        await original_invoke(slot_num)

    runtime_data.callbacks.invoke_entity_removers_for_slot = record_invoke  # type: ignore[method-assign]
    await hass.config_entries.async_unload(lock_code_manager_config_entry.entry_id)
    await hass.async_block_till_done()
    assert set(removed_slots) == {1, 2}


async def test_request_pin_update_auto_disables_in_single_write(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Empty-PIN auto-disable coalesces PIN and ENABLED into one async_update_entry call.

    Two consecutive writes would let the second read a stale
    ``runtime_data.config`` and silently drop the first; one write keys
    both fields off the same snapshot.
    """
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    captured: list[dict[str, Any]] = []
    original = hass.config_entries.async_update_entry

    def capture(entry, **kwargs):
        if entry is lock_code_manager_config_entry and "data" in kwargs:
            captured.append(kwargs["data"])
        return original(entry, **kwargs)

    with patch.object(hass.config_entries, "async_update_entry", side_effect=capture):
        await coordinator.async_request_pin_update("")
        await hass.async_block_till_done()

    assert len(captured) == 1
    slot_1 = captured[0][CONF_SLOTS][1]
    assert slot_1[CONF_PIN] == ""
    assert slot_1[CONF_ENABLED] is False


async def test_condition_entity_swap_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Changing CONF_ENTITY_ID via options unsubscribes from the old condition entity."""
    hass.states.async_set("input_boolean.gate_a", STATE_ON)
    hass.states.async_set("input_boolean.gate_b", STATE_OFF)
    await hass.async_block_till_done()

    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "alice",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_ENTITY_ID: "input_boolean.gate_a",
            },
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Test Swap")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.slot_coordinators[1]
    assert coordinator.is_active is True

    new_options = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "alice",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_ENTITY_ID: "input_boolean.gate_b",
            },
        },
    }
    hass.config_entries.async_update_entry(entry, options=new_options)
    await hass.async_block_till_done()

    # gate_b is OFF, so the slot is now inactive.
    assert coordinator.is_active is False

    # Toggling the OLD condition entity must NOT affect the slot anymore.
    hass.states.async_set("input_boolean.gate_a", STATE_OFF)
    await hass.async_block_till_done()
    assert coordinator.is_active is False

    # Toggling the NEW condition entity to ON makes the slot active again.
    hass.states.async_set("input_boolean.gate_b", STATE_ON)
    await hass.async_block_till_done()
    assert coordinator.is_active is True

    await hass.config_entries.async_unload(entry.entry_id)


async def test_slot_remove_then_readd_creates_fresh_coordinator(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Re-adding a removed slot creates a NEW coordinator with fresh state.

    A regression that retained a stale coordinator across remove + re-add
    would let prior state (condition subscription, registered writers,
    repair-issue history) leak into the new slot lifecycle.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    original_slot_2_coordinator = runtime_data.slot_coordinators[2]

    # Remove slot 2.
    options_without_2 = {
        CONF_LOCKS: list(runtime_data.locks.keys()),
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }
    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=options_without_2
    )
    await hass.async_block_till_done()
    assert 2 not in runtime_data.slot_coordinators

    # Re-add slot 2 with different config.
    options_with_2 = {
        CONF_LOCKS: list(runtime_data.locks.keys()),
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "fresh2", CONF_PIN: "9999", CONF_ENABLED: False},
        },
    }
    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=options_with_2
    )
    await hass.async_block_till_done()

    assert 2 in runtime_data.slot_coordinators
    new_slot_2_coordinator = runtime_data.slot_coordinators[2]
    assert new_slot_2_coordinator is not original_slot_2_coordinator
    assert new_slot_2_coordinator.pin_value == "9999"
    assert new_slot_2_coordinator.is_enabled is False
    # The discarded coordinator must have been stopped.
    assert original_slot_2_coordinator._started is False


async def test_request_sync_check_public_wrapper_delegates(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The public ``request_sync_check`` forwards to the private handler.

    The wrapper exists so callers that aren't listener-shaped don't have
    to pretend to be one (``_request_sync_check`` accepts ``*_args`` to
    serve as a state-change listener). A regression that bypasses the
    private handler would lose the centralized state-transition rules.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    assert runtime_data.sync_managers
    manager = next(iter(runtime_data.sync_managers))

    with patch.object(manager, "_request_sync_check") as private:
        manager.request_sync_check()

    private.assert_called_once_with()


async def test_active_binary_sensor_does_not_self_track_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """``LockCodeManagerActiveEntity`` must not subscribe to state changes itself.

    All condition-entity tracking lives on ``SlotEntityCoordinator``;
    a regression that re-added ``async_track_state_change_event`` inside
    the entity would risk double subscriptions and re-introduce the
    cross-entity-state-coupling D removed.
    """
    # The entity's only subscription handle should be the coordinator
    # view unsub. The pre-D version tracked the condition entity and a
    # config-entry update listener via additional unsub attributes.
    assert not any(
        attr.startswith("_condition_") or attr.startswith("_config_entry_")
        for attr in vars(LockCodeManagerActiveEntity)
        if attr.endswith("_unsub")
    )
    instance_attrs = set()
    for entity in hass.states.async_all("binary_sensor"):
        if not entity.entity_id.endswith("_active"):
            continue
        runtime_data = lock_code_manager_config_entry.runtime_data
        for slot_coord in runtime_data.slot_coordinators.values():
            for writer in slot_coord._active_view_writers:
                # The writer is the entity's bound method; reach its instance.
                if hasattr(writer, "__self__"):
                    instance_attrs.update(vars(writer.__self__).keys())

    # Same assertion at instance level: no condition / config-entry unsubs.
    assert not any(
        attr.startswith("_condition_") or attr.startswith("_config_entry_")
        for attr in instance_attrs
        if attr.endswith("_unsub")
    )
