"""Tests for diagnostics."""

from __future__ import annotations

import copy

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.lock_code_manager.const import CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential

from .common import BASE_CONFIG, LOCK_1_ENTITY_ID

# SlotCode sentinel values as they appear after _mask_code
_SENTINEL_VALUES = {"empty", "unreadable_code", None}


async def test_config_entry_diagnostics(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test config entry diagnostics returns locks, slots, and entities."""
    result = await async_get_config_entry_diagnostics(
        hass, lock_code_manager_config_entry
    )

    assert "config_entry" in result
    assert result["config_entry"]["state"] == "loaded"
    assert "locks" in result
    assert "slots" in result

    # Should have our managed lock
    assert LOCK_1_ENTITY_ID in result["locks"]
    lock_diag = result["locks"][LOCK_1_ENTITY_ID]
    assert lock_diag["entity_id"] == LOCK_1_ENTITY_ID
    assert "coordinator" in lock_diag
    assert "data" in lock_diag["coordinator"]

    # PINs should be masked (not raw values)
    for code in lock_diag["coordinator"]["data"].values():
        if code not in _SENTINEL_VALUES:
            assert code.startswith("pin#")

    # Should have slot data
    assert len(result["slots"]) > 0
    slot_diag = next(iter(result["slots"].values()))
    assert "slot_num" in slot_diag
    assert "pin" in slot_diag
    assert "entities" in slot_diag

    # Configured PINs should also be masked
    if slot_diag["pin"] not in _SENTINEL_VALUES:
        assert slot_diag["pin"].startswith("pin#")


async def test_device_diagnostics_slot_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test device diagnostics for a slot device."""
    dev_reg = dr.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    slot_device = dev_reg.async_get_device(identifiers={(DOMAIN, f"{entry_id}|1")})
    assert slot_device is not None

    result = await async_get_device_diagnostics(
        hass, lock_code_manager_config_entry, slot_device
    )

    assert result["slot_num"] == 1
    assert "pin" in result
    assert "entities" in result
    assert "locks" in result


async def test_device_diagnostics_lock_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Test device diagnostics for a lock device.

    The test mock may not create a device_entry for the lock. If that's the
    case, verify the fallback to config entry diagnostics instead.
    """
    lock = lock_code_manager_config_entry.runtime_data.locks.get(LOCK_1_ENTITY_ID)
    assert lock is not None

    if lock.device_entry:
        result = await async_get_device_diagnostics(
            hass, lock_code_manager_config_entry, lock.device_entry
        )
        assert result["entity_id"] == LOCK_1_ENTITY_ID
        assert "coordinator" in result
    else:
        # No device entry — device diagnostics falls through to config entry
        dev_reg = dr.async_get(hass)
        entry_id = lock_code_manager_config_entry.entry_id
        ce_device = dev_reg.async_get_device(identifiers={(DOMAIN, entry_id)})
        assert ce_device is not None
        result = await async_get_device_diagnostics(
            hass, lock_code_manager_config_entry, ce_device
        )
        assert "locks" in result
        assert LOCK_1_ENTITY_ID in result["locks"]


async def test_device_diagnostics_config_entry_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test device diagnostics for the config entry parent device."""
    dev_reg = dr.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    ce_device = dev_reg.async_get_device(identifiers={(DOMAIN, entry_id)})
    assert ce_device is not None

    result = await async_get_device_diagnostics(
        hass, lock_code_manager_config_entry, ce_device
    )

    # Falls through to config entry diagnostics (superset)
    assert "locks" in result
    assert "slots" in result


async def test_mask_code_values(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test that all PIN values in diagnostics are masked deterministically."""
    result = await async_get_config_entry_diagnostics(
        hass, lock_code_manager_config_entry
    )

    for lock_diag in result["locks"].values():
        for code in lock_diag["coordinator"]["data"].values():
            if code not in _SENTINEL_VALUES:
                assert code.startswith("pin#"), f"Unmasked PIN in diagnostics: {code}"

    for slot_diag in result["slots"].values():
        if slot_diag["pin"] not in _SENTINEL_VALUES:
            assert slot_diag["pin"].startswith("pin#"), (
                f"Unmasked configured PIN: {slot_diag['pin']}"
            )


async def test_sensitive_entities_redacted(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test that PIN and code entity states are redacted in diagnostics."""
    result = await async_get_config_entry_diagnostics(
        hass, lock_code_manager_config_entry
    )

    # Collect all entities across all diagnostic sections
    all_entities = []
    for slot_diag in result["slots"].values():
        all_entities.extend(slot_diag.get("entities", []))
    for lock_diag in result["locks"].values():
        all_entities.extend(lock_diag.get("entities", []))

    # _is_sensitive checks unique_id for |pin and |code markers, which
    # results in **REDACTED** state. Verify at least one entity is redacted.
    redacted = [e for e in all_entities if e["state"] == "**REDACTED**"]
    assert len(redacted) > 0, "Expected at least one redacted entity"

    for entity in redacted:
        assert entity["attributes"] == {}
        assert entity["platform"] == DOMAIN


async def test_mask_code_covers_missing_and_edge_case_credentials(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """``_mask_code`` covers three distinct branches of its label-to-string mapping.

    A slot absent from coordinator data, a cleared slot (SlotCode.EMPTY),
    and a present-but-empty-string PIN (the falsy-label fallback) all
    surface differently in diagnostics output.
    """
    entry = lock_code_manager_config_entry
    lock_1 = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_1.coordinator
    assert coordinator is not None

    # Add slot 3 to config but never write a code for it on lock 1 --
    # coordinator.data has no entry for slot 3 even though it is non-empty.
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][3] = {
        CONF_NAME: "test3",
        CONF_PIN: "9999",
        CONF_ENABLED: False,
    }
    hass.config_entries.async_update_entry(entry, options=new_config)
    await hass.async_block_till_done()
    assert pin_address(3) not in coordinator.data

    # Slot 1: cleared on the lock -> SlotCode.EMPTY sentinel.
    coordinator.push_update({1: SlotCredential.empty()})
    # Slot 2: present but with an empty-string PIN -- the falsy-label
    # fallback distinct from the SlotCode.EMPTY sentinel above.
    coordinator.push_update({2: SlotCredential.known("")})
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)

    lock_1_data = result["locks"][LOCK_1_ENTITY_ID]["coordinator"]["data"]
    assert lock_1_data["1"] == "empty"
    assert lock_1_data["2"] == "empty"

    # Slot 3's per-lock entry is built via _slot_diagnostic's .get() lookup,
    # which returns None for a slot the coordinator has never seen.
    slot_3_lock_1 = result["slots"]["3"]["locks"][LOCK_1_ENTITY_ID]
    assert slot_3_lock_1["coordinator_code"] is None
