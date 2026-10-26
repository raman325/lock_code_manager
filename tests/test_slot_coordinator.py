"""Tests for the per-slot entity coordinator."""

import logging
import re
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
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir, translation
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.binary_sensor import (
    LockCodeManagerActiveEntity,
)
from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.ha.entry import (
    LockCodeManagerConfigEntryRuntimeData,
    available_credential_types,
)
from custom_components.lock_code_manager.ha.slot_coordinator import (
    CredentialRequiredError,
    SlotEntityCoordinator,
)
from custom_components.lock_code_manager.model.config import async_write_entry_config
from custom_components.lock_code_manager.model.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.model.queries import get_entry_config

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_PIN_ENTITY,
    async_write_slot_pin,
    unnumbered_user_subentry,
    user_subentries,
    write_entry_config,
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
        await coordinator.async_request_credential_update(CredentialType.PIN, "12")

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
        await coordinator.async_request_credential_update(
            CredentialType.PIN, "123456789"
        )


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

    await coordinator.async_request_credential_update(CredentialType.PIN, "567890")
    await hass.async_block_till_done()

    assert get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == (
        "567890"
    )


async def test_request_pin_update_skips_lock_advertising_no_pin_type(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A lock with known capabilities but no PIN type constrains nothing.

    Distinct from the uncached case: here the capabilities ARE known, they
    just say nothing about PIN length. Treating that silence as a rejection
    would let one non-PIN lock in the entry block every write.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = LockCapabilities(
            supports_user_management=True,
            max_users=30,
            credential_types={},
        )
    coordinator = runtime_data.slot_coordinators[1]

    # A single character would fail any real bound, so acceptance proves the skip.
    await coordinator.async_request_credential_update(CredentialType.PIN, "1")
    await hass.async_block_till_done()

    assert get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == "1"


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

    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()

    assert get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == ""


async def test_request_pin_update_fails_open_without_capabilities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Locks with unknown capabilities do not block a write."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(CredentialType.PIN, "12")
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
        await coordinator.async_request_credential_update(CredentialType.PIN, "12")

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
        await coordinator.async_request_credential_update(CredentialType.PIN, ok)
        await hass.async_block_till_done()
        assert (
            get_entry_config(lock_code_manager_config_entry).slot(1).get(CONF_PIN) == ok
        )

    for bad in ("123", "123456789"):  # one under the min and one over the max
        with pytest.raises(ServiceValidationError):
            await coordinator.async_request_credential_update(CredentialType.PIN, bad)


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
        await coordinator.async_request_credential_update(CredentialType.PIN, "12")

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

    await coordinator.async_request_credential_update(CredentialType.PIN, "")
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
    await coordinator.async_request_credential_update(CredentialType.PIN, "   ")
    await hass.async_block_till_done()

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_PIN) == ""
    assert config.get(CONF_ENABLED) is False


async def test_request_pin_update_strips_surrounding_whitespace(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A padded PIN is stored stripped, and its length is judged stripped too.

    Every consumer compares against the stored value stripped -- the
    validate_code action most visibly -- so storing the padding makes a
    PIN nobody can enter.
    """
    runtime_data = lock_code_manager_config_entry.runtime_data
    for lock in runtime_data.locks.values():
        lock._capabilities_cache = _pin_caps(4, 8)
    coordinator = runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(
        CredentialType.PIN, "  12345678  "
    )
    await hass.async_block_till_done()

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_PIN) == "12345678"
    assert config.get(CONF_ENABLED) is True


async def test_request_active_toggle_blocks_when_no_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Toggling active=True without a PIN raises and writes a repair issue."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    # Clear PIN first (auto-disables the slot)
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()
    assert coordinator.is_enabled is False
    assert coordinator.pin_value is None

    with pytest.raises(CredentialRequiredError):
        await coordinator.async_request_active_toggle(True)

    issue_registry = async_get_issue_registry(hass)
    issue = issue_registry.async_get_issue(
        DOMAIN, f"credential_required_{lock_code_manager_config_entry.entry_id}_1"
    )
    assert issue is not None
    assert coordinator.is_enabled is False


async def test_request_active_toggle_clears_credential_required_issue(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Successful enable removes the credential_required repair issue."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()
    with pytest.raises(CredentialRequiredError):
        await coordinator.async_request_active_toggle(True)
    issue_registry = async_get_issue_registry(hass)
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"credential_required_{lock_code_manager_config_entry.entry_id}_1"
        )
        is not None
    )

    # Re-add a PIN and successfully enable; the repair issue must be cleared
    await coordinator.async_request_credential_update(CredentialType.PIN, "4242")
    await hass.async_block_till_done()
    await coordinator.async_request_active_toggle(True)
    await hass.async_block_till_done()

    assert coordinator.is_enabled is True
    assert (
        issue_registry.async_get_issue(
            DOMAIN, f"credential_required_{lock_code_manager_config_entry.entry_id}_1"
        )
        is None
    )


async def test_switch_turn_on_raises_homeassistanterror_for_credential_required(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """The switch entity translates CredentialRequiredError into HomeAssistantError."""
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

    with pytest.raises(HomeAssistantError, match="Set a PIN or password"):
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
                CONF_CONDITION: "input_boolean.gate",
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
    """Empty-PIN auto-disable coalesces PIN and ENABLED into one subentry write.

    Two consecutive writes would let the second read a stale
    ``runtime_data.config`` and silently drop the first; one write keys
    both fields off the same snapshot.
    """
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    captured: list[dict[str, Any]] = []
    original = hass.config_entries.async_update_subentry

    def capture(entry, subentry, **kwargs):
        if entry is lock_code_manager_config_entry and "data" in kwargs:
            captured.append(dict(kwargs["data"]))
        return original(entry, subentry, **kwargs)

    with patch.object(
        hass.config_entries, "async_update_subentry", side_effect=capture
    ):
        await coordinator.async_request_credential_update(CredentialType.PIN, "")
        await hass.async_block_till_done()

    assert len(captured) == 1
    assert captured[0][CONF_PIN] == ""
    assert captured[0][CONF_ENABLED] is False


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
                CONF_CONDITION: "input_boolean.gate_a",
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
                CONF_CONDITION: "input_boolean.gate_b",
            },
        },
    }
    write_entry_config(hass, entry, new_options)
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
    write_entry_config(hass, lock_code_manager_config_entry, options_without_2)
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
    write_entry_config(hass, lock_code_manager_config_entry, options_with_2)
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
                CONF_CONDITION: condition_entity,
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
        await coordinator.async_request_credential_update(CredentialType.PIN, "5555")
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
    """A failure deleting the credential_required repair issue does not unwind the enable."""
    runtime_data = lock_code_manager_config_entry.runtime_data
    coordinator = runtime_data.slot_coordinators[1]
    # Disable so the toggle has work to do.
    await coordinator.async_request_active_toggle(False)
    await hass.async_block_till_done()
    assert coordinator.is_enabled is False

    boom = RuntimeError("issue registry boom")
    with patch(
        "custom_components.lock_code_manager.ha.slot_coordinator.async_delete_issue",
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
                CONF_CONDITION: "input_boolean.gate_a",
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
    collection. (The in-sync per-lock sensor subscribes to its lock's sync
    changes instead; its managers are started by the coordinator itself,
    covered by the slot add/remove lifecycle tests.)
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

    assert "LockCodeManagerNameText" in state_subscriber_owners
    assert "LockCodeManagerCredentialText" in state_subscriber_owners
    assert "LockCodeManagerSwitch" in state_subscriber_owners
    assert active_view_owners == {"LockCodeManagerActiveEntity"}


@pytest.mark.parametrize(
    "entity_class", ["LockCodeManagerCredentialText", "LockCodeManagerNameText"]
)
async def test_text_set_value_raises_when_slot_coordinator_missing(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    entity_class: str,
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
        if hasattr(sub, "__self__") and type(sub.__self__).__name__ == entity_class
    )

    text_entity._slot_coordinator = None
    with pytest.raises(
        HomeAssistantError, match=f"No slot coordinator for slot {text_entity.slot_num}"
    ):
        await text_entity.async_set_value("9999")


async def test_inactive_because_of_returns_defensive_copy(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """inactive_because_of returns a fresh list, not the internal state list."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[2]
    # Slot 2 has a calendar condition entity that starts inactive.
    assert coordinator.is_active is False

    result = coordinator.inactive_because_of
    assert result == coordinator._inactive_because_of
    assert result is not coordinator._inactive_because_of

    result.append("mutated")
    assert "mutated" not in coordinator._inactive_because_of


async def test_async_request_name_update_writes_name_field(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """async_request_name_update writes the CONF_NAME field for the slot."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_name_update("Bob")
    await hass.async_block_till_done()

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_NAME) == "Bob"


async def test_async_start_is_idempotent(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A second async_start call is a no-op; it does not re-subscribe or recompute."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    assert coordinator._started is True

    with patch.object(coordinator, "_recompute_active") as recompute:
        coordinator.async_start()

    recompute.assert_not_called()


async def test_notify_config_changed_noop_once_stopped(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """notify_config_changed is a no-op once the coordinator has been stopped."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    coordinator.async_stop()
    assert coordinator._started is False

    with patch.object(coordinator, "_recompute_active") as recompute:
        coordinator.notify_config_changed()

    recompute.assert_not_called()


async def test_notify_state_subscribers_isolates_individual_failures(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """One raising state subscriber does not prevent the others from being notified."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    boom = RuntimeError("simulated subscriber failure")
    healthy_called = {"value": False}

    def failing() -> None:
        raise boom

    def healthy() -> None:
        healthy_called["value"] = True

    unsub_failing = coordinator.register_state_subscriber(failing)
    unsub_healthy = coordinator.register_state_subscriber(healthy)
    try:
        with caplog.at_level(logging.ERROR):
            coordinator._notify_state_subscribers()
    finally:
        unsub_failing()
        unsub_healthy()

    assert healthy_called["value"] is True
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )


async def test_recompute_active_missing_condition_entity_is_inactive(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A configured condition entity with no state (not created) reads as inactive.

    ``hass.states.get`` returns None for a nonexistent entity, which must be
    treated the same as "unknown" (neither definitely on nor off) rather
    than crashing or defaulting to active.
    """
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "alice",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_CONDITION: "input_boolean.does_not_exist",
            },
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Test Missing Cond")
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.slot_coordinators[1]
    assert coordinator.is_active is False
    assert CONF_CONDITION in coordinator.inactive_because_of

    await hass.config_entries.async_unload(entry.entry_id)


async def test_credential_required_issue_creation_failure_is_swallowed(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A failure creating the credential_required repair issue does not swallow the raise."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()
    assert coordinator.pin_value is None

    boom = RuntimeError("issue registry boom")
    with patch(
        "custom_components.lock_code_manager.ha.slot_coordinator.async_create_issue",
        side_effect=boom,
    ):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(CredentialRequiredError):
                await coordinator.async_request_active_toggle(True)

    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "ERROR"
    )


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


async def test_an_entry_of_pin_locks_offers_pins_only(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """No lock takes a password: the entry offers none."""
    assert available_credential_types(lock_code_manager_config_entry) == {
        CredentialType.PIN
    }


async def test_an_entry_holding_no_lock_yet_offers_pins() -> None:
    """Before any lock is set up, a user can still be given a PIN."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = LockCodeManagerConfigEntryRuntimeData()
    assert available_credential_types(entry) == {CredentialType.PIN}


async def test_a_refused_password_value_is_never_written(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """The character rule refuses before the config is touched, without the value."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError) as refused:
        await coordinator.async_request_credential_update(
            CredentialType.PASSWORD, "café-marker-7f3a"
        )

    assert "marker-7f3a" not in str(refused.value)
    assert "password" not in get_entry_config(lock_code_manager_config_entry).slot(1)


async def test_a_stored_empty_password_does_not_gate_the_slot(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A credential field is never an active gate, cleared or not."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(CredentialType.PASSWORD, "")

    assert coordinator.is_active is True


async def _store_slot1_password(hass: HomeAssistant, entry) -> None:
    """Store a password for slot 1 past every write path, as a departed lock leaves one."""
    async_write_entry_config(
        hass, entry, get_entry_config(entry).with_slot_field_set(1, "password", "pw")
    )
    await hass.async_block_till_done()


async def test_clearing_the_pin_beside_a_password_no_lock_takes_turns_the_user_off(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    A stored password no lock in the entry takes does not count (decision 4).

    It is kept (decision 10), but the user is left with nothing a lock is given.
    """
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await _store_slot1_password(hass, lock_code_manager_config_entry)
    assert coordinator.is_enabled is True

    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_ENABLED) is False
    assert config["password"] == "pw"
    assert coordinator.is_enabled is False


async def test_a_password_no_lock_takes_cannot_enable_a_user(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Enabling a user who holds only a password no lock takes is refused."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await _store_slot1_password(hass, lock_code_manager_config_entry)
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()
    assert coordinator.is_enabled is False

    with pytest.raises(CredentialRequiredError):
        await coordinator.async_request_active_toggle(True)

    config = get_entry_config(lock_code_manager_config_entry).slot(1)
    assert config.get(CONF_ENABLED) is False
    assert config["password"] == "pw"
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"credential_required_{lock_code_manager_config_entry.entry_id}_1"
        )
        is not None
    )


async def test_a_disabled_user_reads_as_disabled_alone(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Holding nothing an entry's lock takes adds no reason to a disabled user."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]
    await _store_slot1_password(hass, lock_code_manager_config_entry)
    await coordinator.async_request_credential_update(CredentialType.PIN, "")
    await hass.async_block_till_done()

    assert coordinator.is_enabled is False
    assert coordinator.inactive_because_of == [CONF_ENABLED]


async def test_an_enable_answers_a_repair_raised_while_enabled(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    Enabling a user who was already on still answers ``credential_required``.

    Refused for want of a credential the entry offers, the user gets a PIN and
    enables again: no off-to-on transition is seen, but the repair is answered.
    """
    entry = lock_code_manager_config_entry
    coordinator = entry.runtime_data.slot_coordinators[1]
    await _store_slot1_password(hass, entry)
    async_write_entry_config(
        hass, entry, get_entry_config(entry).with_slot_field_removed(1, CONF_PIN)
    )
    await hass.async_block_till_done()
    assert coordinator.is_enabled is True
    assert coordinator.is_active is False
    issue_id = f"credential_required_{entry.entry_id}_1"

    with pytest.raises(CredentialRequiredError):
        await coordinator.async_request_active_toggle(True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await coordinator.async_request_credential_update(CredentialType.PIN, "1357")
    await coordinator.async_request_active_toggle(True)
    await hass.async_block_till_done()

    assert coordinator.is_active is True
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_a_leftover_pin_required_issue_is_deleted_at_setup(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """The issue's old id is cleared once the entry is set up again."""
    entry_id = lock_code_manager_config_entry.entry_id
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"pin_required_{entry_id}_1",
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="pin_required",
    )

    await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done()

    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"pin_required_{entry_id}_1") is None
    )


async def test_a_leftover_pin_required_issue_keeps_its_text(
    hass: HomeAssistant,
) -> None:
    """
    An older release's ``pin_required`` issue still renders.

    It is persistent, and only a setup of its entry deletes it: on an entry
    that never loads again it stays, shown through its own translation key.
    """
    translations = await translation.async_get_translations(
        hass, "en", "issues", {DOMAIN}
    )
    # The placeholders the older release raised the issue with.
    raised_with = {"slot_num", "config_entry_title"}
    for part in ("title", "fix_flow.step.init.title", "fix_flow.step.init.description"):
        template = translations[f"component.{DOMAIN}.issues.pin_required.{part}"]
        assert set(re.findall(r"\{(\w+)\}", template)) <= raised_with


async def test_another_users_pin_is_refused(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Two users sharing a PIN: whoever types it opens the door as either."""
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    with pytest.raises(
        ServiceValidationError, match="already has this PIN$"
    ) as refused:
        await coordinator.async_request_credential_update(CredentialType.PIN, "5678")

    assert "5678" not in str(refused.value)
    assert get_entry_config(lock_code_manager_config_entry).slot(1)[CONF_PIN] == "1234"


async def test_an_unchanged_pin_shared_with_another_user_is_left_alone(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A duplicate saved before this rule is not refused until it changes."""
    await async_write_slot_pin(hass, lock_code_manager_config_entry, 2, "1234")
    assert get_entry_config(lock_code_manager_config_entry).slot(2)[CONF_PIN] == "1234"
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[1]

    await coordinator.async_request_credential_update(CredentialType.PIN, "1234")


async def test_a_pin_held_by_a_user_with_no_slot_number_is_refused(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Every user in the entry counts, numbered or not (spec decision 12)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: [LOCK_1_ENTITY_ID]},
        subentries_data=[
            *user_subentries({1: {CONF_NAME: "Ann", CONF_ENABLED: True}}),
            unnumbered_user_subentry(
                "Unplaced", **{CONF_PIN: "1234", CONF_ENABLED: True}
            ),
        ],
        unique_id="unplaced-duplicate",
        version=5,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data.slot_coordinators[1]

    with pytest.raises(ServiceValidationError, match="Unplaced"):
        await coordinator.async_request_credential_update(CredentialType.PIN, "1234")

    assert not get_entry_config(entry).slot(1).get(CONF_PIN)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_turned_off_users_pin_is_refused(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    A turned-off user's PIN is still theirs, so it is still a duplicate.

    Nothing checks for duplicates again when the user is turned back on.
    """
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: SLOT_1_ENABLED_ENTITY},
        blocking=True,
    )
    config = get_entry_config(lock_code_manager_config_entry)
    assert config.slot(1)[CONF_ENABLED] is False
    assert config.slot(1)[CONF_PIN] == "1234"
    coordinator = lock_code_manager_config_entry.runtime_data.slot_coordinators[2]

    with pytest.raises(ServiceValidationError):
        await coordinator.async_request_credential_update(CredentialType.PIN, "1234")

    assert get_entry_config(lock_code_manager_config_entry).slot(2)[CONF_PIN] == "5678"
