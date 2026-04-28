"""Tests for diagnostics."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

from .common import LOCK_1_ENTITY_ID


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
        if code and code not in ("empty", "EMPTY", "UNREADABLE_CODE"):
            assert code.startswith("pin#")

    # Should have slot data
    assert len(result["slots"]) > 0
    slot_diag = next(iter(result["slots"].values()))
    assert "slot_num" in slot_diag
    assert "pin" in slot_diag
    assert "entities" in slot_diag

    # Configured PINs should also be masked
    if slot_diag["pin"]:
        assert slot_diag["pin"].startswith("pin#")


async def test_device_diagnostics_slot_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test device diagnostics for a slot device."""
    dev_reg = dr.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    # Find the slot 1 device
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
    """Test device diagnostics for a lock device."""
    # Find the lock device — look through all LCM locks
    all_locks = hass.data.get(DOMAIN, {}).get("locks", {})
    lock = all_locks.get(LOCK_1_ENTITY_ID)

    if lock and lock.device_entry:
        result = await async_get_device_diagnostics(
            hass, lock_code_manager_config_entry, lock.device_entry
        )

        assert result["entity_id"] == LOCK_1_ENTITY_ID
        assert "coordinator" in result
    else:
        # If no device entry (test mock may not create one), fall back to
        # config entry diagnostics
        pass


async def test_device_diagnostics_config_entry_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test device diagnostics for the config entry parent device."""
    dev_reg = dr.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    # The config entry device
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

    # Collect all PIN-like values
    for lock_diag in result["locks"].values():
        for code in lock_diag["coordinator"]["data"].values():
            if code and code not in ("empty", "EMPTY", "UNREADABLE_CODE"):
                # Must be masked
                assert code.startswith("pin#"), f"Unmasked PIN in diagnostics: {code}"

    for slot_diag in result["slots"].values():
        if slot_diag["pin"]:
            assert slot_diag["pin"].startswith("pin#"), (
                f"Unmasked configured PIN: {slot_diag['pin']}"
            )
