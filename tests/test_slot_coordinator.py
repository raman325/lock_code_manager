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
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.binary_sensor import (
    LockCodeManagerActiveEntity,
)
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.slot_coordinator import (
    PinRequiredError,
    SlotEntityCoordinator,
)

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_PIN_ENTITY,
)

_LOGGER = logging.getLogger(__name__)


def _pin_caps(min_length: int, max_length: int) -> LockCapabilities:
    """Build LockCapabilities advertising a PIN type with the given bounds."""
    return LockCapabilities(
        supports_user_management=True,
        max_users=30,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=30,
                min_length=min_length,
                max_length=max_length,
                supports_learn=False,
            )
        },
    )


async def test_request_pin_update_rejects_out_of_range_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A PIN shorter than the lock minimum is rejected and never written."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)
    coordinator = runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError):
        await coordinator.async_request_pin_update("12")

    # The rejected PIN must not reach config.
    assert get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == (
        "1234"
    )


async def test_request_pin_update_rejects_too_long_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A PIN longer than the lock maximum is rejected."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)
    coordinator = runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError):
        await coordinator.async_request_pin_update("123456789")


async def test_request_pin_update_allows_in_range_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A PIN within the advertised range is written normally."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)
    coordinator = runtime_data.slot_coordinators[1]

    await coordinator.async_request_pin_update("567890")
    await hass.async_block_till_done()

    assert get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == (
        "567890"
    )


async def test_request_pin_update_empty_pin_exempt_from_length(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Clearing the PIN is allowed even when the lock requires a minimum length."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)
    coordinator = runtime_data.slot_coordinators[1]

    await coordinator.async_request_pin_update("")
    await hass.async_block_till_done()

    assert get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == ""


async def test_request_pin_update_fails_open_without_capabilities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Locks with unknown capabilities do not block a write."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_pin_update("12")
    await hass.async_block_till_done()

    assert (
        get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == "12"
    )


async def test_validation_names_each_offending_lock(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The rejection message names every lock the PIN violates."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    runtime_data.locks[LOCK_1_ENTITY_ID]._capabilities_cache = _pin_caps(6, 8)
    runtime_data.locks[LOCK_2_ENTITY_ID]._capabilities_cache = _pin_caps(6, 8)
    coordinator = runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError) as exc:
        await coordinator.async_request_pin_update("12")

    message = str(exc.value)
    assert runtime_data.locks[LOCK_1_ENTITY_ID].display_name in message
    assert runtime_data.locks[LOCK_2_ENTITY_ID].display_name in message


async def test_request_pin_update_accepts_boundary_lengths(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A PIN exactly at the min or max is accepted; one past either end is rejected."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)
    coordinator = runtime_data.slot_coordinators[1]

    for ok in ("1234", "12345678"):  # exactly the min (4) and the max (8)
        await coordinator.async_request_pin_update(ok)
        await hass.async_block_till_done()
        assert (
            get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == ok
        )

    for bad in ("123", "123456789"):  # one under the min and one over the max
        with pytest.raises(ServiceValidationError):
            await coordinator.async_request_pin_update(bad)


async def test_validation_message_unbounded_max_says_at_least(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A lock advertising a minimum but no maximum yields an 'at least N' message."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(6, 0)  # max 0 == unbounded
    coordinator = runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError) as exc:
        await coordinator.async_request_pin_update("12")

    assert "at least 6 characters" in str(exc.value)


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


async def test_active_binary_sensor_does_not_self_track_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """``LockCodeManagerActiveEntity`` must not subscribe to the condition entity itself.

    The slot coordinator owns the condition-entity subscription. A
    regression that re-added ``async_track_state_change_event`` inside
    the entity (under any attribute name) would produce a SECOND
    subscription for the same condition entity, observable as two
    listeners on that state-changed event.
    """
    condition_entity = "input_boolean.gate_a"
    hass.states.async_set(condition_entity, STATE_ON)
    await hass.async_block_till_done()

    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "alice",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_ENTITY_ID: condition_entity,
            },
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Active No Track"
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.slot_coordinators[1]
    assert len(coordinator._active_view_writers) == 1

    active_entity = next(
        writer.__self__
        for writer in coordinator._active_view_writers
        if hasattr(writer, "__self__")
        and isinstance(writer.__self__, LockCodeManagerActiveEntity)
    )

    # No attribute on the active entity should hold a tracker we added
    # ourselves (HA internals like ``_unsub_device_updates`` are allowed).
    forbidden_prefixes = ("_condition_", "_config_entry_", "_track_", "_state_change_")
    leaked = [
        attr
        for attr in vars(active_entity)
        if attr.startswith(forbidden_prefixes) and attr.endswith("_unsub")
    ]
    assert leaked == [], (
        f"Active entity holds entity-side tracker unsubs: {leaked} -- the "
        "coordinator should own all condition-state subscriptions."
    )

    # And the coordinator's condition subscription is the SOLE wiring: a
    # state change on the condition entity flips the active state once,
    # not twice (no double-fire indicating duplicate listeners).
    flips: list[bool | None] = []

    @callback
    def record_writer(is_on: bool | None, _inactive: list[str]) -> None:
        flips.append(is_on)

    coordinator.register_active_view(record_writer)
    flips.clear()
    hass.states.async_set(condition_entity, STATE_OFF)
    await hass.async_block_till_done()
    assert flips == [False], f"expected single flip, saw {flips}"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_write_config_fields_refreshes_runtime_config_before_notify(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A synchronous notify after async_update_entry sees the fresh config.

    ``async_update_entry`` schedules the listener as a task, so
    ``runtime_data.config`` is still stale at the synchronous
    ``_notify_state_subscribers`` call inside ``_write_config_fields``.
    Subscribers must observe the post-write value, not the pre-write one.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]

    captured_pin: list[str | None] = []

    def capture_subscriber() -> None:
        captured_pin.append(coordinator.pin_value)

    unsub = coordinator.register_state_subscriber(capture_subscriber)
    try:
        await coordinator.async_request_pin_update("5555")
        await hass.async_block_till_done()
    finally:
        unsub()

    # The subscriber fires twice (synchronous notify after the eager
    # refresh, plus the listener-task notify). Both must observe the
    # post-write value; the FIRST one is the load-bearing assertion --
    # without the eager refresh, that synchronous fire would observe
    # the stale "1234" because the listener task has not yet run.
    assert captured_pin, "subscriber must fire at least once"
    assert captured_pin[0] == "5555"
    assert all(pin == "5555" for pin in captured_pin)


async def test_unload_continues_when_slot_coordinator_stop_raises(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """``async_unload_entry`` clears every slot coordinator even when one stop raises."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinators = list(runtime_data.slot_coordinators.values())
    assert len(coordinators) >= 2

    failing = coordinators[0]
    boom = RuntimeError("simulated coordinator stop failure")
    original_stop = failing.async_stop
    raised = {"value": False}

    def failing_stop() -> None:
        original_stop()
        if not raised["value"]:
            raised["value"] = True
            raise boom

    with patch.object(failing, "async_stop", failing_stop):
        with caplog.at_level(logging.WARNING):
            await hass.config_entries.async_unload(
                lock_code_manager_config_entry.entry_id
            )
            await hass.async_block_till_done()

    assert raised["value"]
    assert runtime_data.slot_coordinators == {}
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_register_active_view_initial_writer_failure_propagates_and_discards(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A raising writer at registration is dropped and the exception propagates.

    Otherwise the platform layer would see a half-added entity that the
    coordinator kept fanning out to.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]
    boom = RuntimeError("simulated writer failure")
    initial_count = len(coordinator._active_view_writers)

    def failing_writer(_is_on: bool | None, _inactive: list[str]) -> None:
        raise boom

    with pytest.raises(RuntimeError, match="simulated writer failure"):
        coordinator.register_active_view(failing_writer)

    assert failing_writer not in coordinator._active_view_writers
    assert len(coordinator._active_view_writers) == initial_count


async def test_active_toggle_enable_survives_repair_issue_delete_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A failure deleting the pin_required repair issue does not unwind the enable."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]
    # Disable so the toggle has work to do.
    await coordinator.async_request_active_toggle(False)
    await hass.async_block_till_done()
    assert coordinator.is_enabled is False

    boom = RuntimeError("issue registry boom")
    with patch(
        "custom_components.lock_code_manager.domain.slot_coordinator.async_delete_issue",
        side_effect=boom,
    ):
        with caplog.at_level(logging.ERROR):
            await coordinator.async_request_active_toggle(True)
            await hass.async_block_till_done()

    # Write succeeded; the swallowed delete failure was logged.
    assert coordinator.is_enabled is True
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_handle_condition_state_change_after_stop_is_noop(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A queued condition-entity callback firing after async_stop must not recompute."""
    hass.states.async_set("input_boolean.gate_a", STATE_ON)
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
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Test Stopped Cb")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.slot_coordinators[1]
    assert coordinator.is_active is True

    coordinator.async_stop()
    assert coordinator._started is False

    # Simulate a late-firing condition-entity callback. The guard must
    # short-circuit before _recompute_active runs (and would touch
    # _active_view_writers etc.).
    with patch.object(coordinator, "_recompute_active") as recompute:
        coordinator._handle_condition_state_change(None)  # type: ignore[arg-type]

    recompute.assert_not_called()

    await hass.config_entries.async_unload(entry.entry_id)


async def test_poke_sync_managers_isolates_individual_failures(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """One raising sync manager does not abort the poke of the others."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]

    # Inject two stand-in managers so the poke loop has predictable
    # targets regardless of how many real sync managers ended up wired
    # to this slot via the fixture entity registrations.
    boom = RuntimeError("simulated sync manager poke failure")
    healthy_called = {"value": False}

    class _StandIn:
        def __init__(self, raise_on_call: bool) -> None:
            self._raise = raise_on_call

        def request_sync_check(self) -> None:
            if self._raise:
                raise boom
            healthy_called["value"] = True

    failing = _StandIn(raise_on_call=True)
    healthy = _StandIn(raise_on_call=False)
    coordinator._sync_managers.add(failing)  # type: ignore[arg-type]
    coordinator._sync_managers.add(healthy)  # type: ignore[arg-type]

    try:
        with caplog.at_level(logging.ERROR):
            coordinator._poke_sync_managers()
    finally:
        coordinator._sync_managers.discard(failing)  # type: ignore[arg-type]
        coordinator._sync_managers.discard(healthy)  # type: ignore[arg-type]

    assert healthy_called["value"] is True
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_base_entity_caches_slot_coordinator_on_add(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """``async_added_to_hass`` populates ``_slot_coordinator`` from runtime_data.

    Pins the hoist contract: a regression that broke the resolution
    would leave entities silently coordinator-less even though one
    exists in the registry.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    for slot_num, coordinator in runtime_data.slot_coordinators.items():
        all_entities = [
            writer.__self__
            for writer in coordinator._active_view_writers
            if hasattr(writer, "__self__")
        ]
        all_entities.extend(
            sub.__self__
            for sub in coordinator._state_subscribers
            if hasattr(sub, "__self__")
        )
        assert all_entities, (
            f"Slot {slot_num} coordinator has no registered entity writers"
        )
        for entity in all_entities:
            cached = getattr(entity, "_slot_coordinator", "<missing>")
            assert cached is coordinator, (
                f"Entity {type(entity).__name__} for slot {slot_num} cached "
                f"coordinator {cached!r}, expected {coordinator!r}"
            )


async def test_hook_dispatch_routes_each_entity_kind_to_the_right_collection(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """text/switch -> _state_subscribers; active -> _active_view_writers.

    Pins the polymorphic ``_register_slot_coordinator_subscription`` hook
    for the slot-scoped entities. A regression that broke either
    subclass's override would route the entity into the wrong
    collection. (In-sync per-lock entities go through a separate
    lock-slot adder path that fires before the slot coordinator exists
    on initial setup -- a pre-existing D-design limitation; their
    ``register_sync_manager`` registration is exercised only via the
    options-flow ``_async_setup_new_locks`` path, covered by the slot
    add/remove lifecycle tests.)
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]

    state_subscriber_owners = {
        type(sub.__self__).__name__
        for sub in coordinator._state_subscribers
        if hasattr(sub, "__self__")
    }
    active_view_owners = {
        type(w.__self__).__name__
        for w in coordinator._active_view_writers
        if hasattr(w, "__self__")
    }

    assert "LockCodeManagerText" in state_subscriber_owners
    assert "LockCodeManagerSwitch" in state_subscriber_owners
    assert active_view_owners == {"LockCodeManagerActiveEntity"}


async def test_text_set_value_raises_when_slot_coordinator_missing(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """text ``async_set_value`` surfaces a HomeAssistantError, not a silent no-op.

    Pins the asymmetry fix where text used to log a WARNING and return
    while switch raised; both should raise so the user sees the failed
    service call.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]
    text_entity = next(
        sub.__self__
        for sub in coordinator._state_subscribers
        if hasattr(sub, "__self__")
        and type(sub.__self__).__name__ == "LockCodeManagerText"
    )

    text_entity._slot_coordinator = None
    with pytest.raises(
        HomeAssistantError, match=f"No slot coordinator for slot {text_entity.slot_num}"
    ):
        await text_entity.async_set_value("9999")


async def test_unload_clears_registry_when_coordinator_stop_raises_before_cleanup(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A coordinator whose ``async_stop`` raises BEFORE cleanup is still cleared.

    Complements ``test_unload_continues_when_slot_coordinator_stop_raises``
    which covers the call-original-then-raise pattern. This variant covers
    the raise-before-cleanup mode -- proving the registry is cleared
    regardless of how stop fails.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    failing = next(iter(runtime_data.slot_coordinators.values()))
    boom = RuntimeError("simulated coordinator stop failure (before cleanup)")

    def failing_stop() -> None:
        # Do NOT call the real stop -- raise immediately so internal
        # state is left intact, simulating a stop that fails at its
        # very first statement.
        raise boom

    with patch.object(failing, "async_stop", failing_stop):
        with caplog.at_level(logging.ERROR):
            await hass.config_entries.async_unload(
                lock_code_manager_config_entry.entry_id
            )
            await hass.async_block_till_done()

    assert runtime_data.slot_coordinators == {}
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )
