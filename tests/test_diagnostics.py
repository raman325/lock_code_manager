"""Tests for diagnostics."""

import copy

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, SERVICE_TURN_OFF
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.lock_code_manager.const import CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.lock_code_manager.model.credentials import (
    SlotCode,
    SlotCredential,
)

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    async_enable_and_reload,
    async_switch_slot,
    mock_lock,
    pin_in_sync_entity_id,
    write_entry_config,
)

# SlotCode sentinel values as they appear after _mask_code
_SENTINEL_VALUES = {*(code.value for code in SlotCode), None}


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
    worker_diag = lock_diag["worker"]
    assert worker_diag["reachable"] is True
    assert worker_diag["offline"] is False
    assert "data" in worker_diag
    assert worker_diag["slots"]["1"] == {
        "status": "in_sync",
        "hold": None,
        "tries": 0,
        "release": False,
    }

    # PINs should be masked (not raw values), on every lock
    assert worker_diag["data"]["1"].startswith("pin#")
    for each_lock in result["locks"].values():
        for code in each_lock["worker"]["data"].values():
            if code not in _SENTINEL_VALUES:
                assert code.startswith("pin#"), f"Unmasked PIN in diagnostics: {code}"

    # Should have slot data
    assert len(result["slots"]) > 0
    slot_diag = next(iter(result["slots"].values()))
    assert "slot_num" in slot_diag
    assert "pin" in slot_diag
    assert "entities" in slot_diag

    # Configured PINs should also be masked, on every slot
    for each_slot in result["slots"].values():
        if each_slot["pin"] not in _SENTINEL_VALUES:
            assert each_slot["pin"].startswith("pin#"), (
                f"Unmasked configured PIN: {each_slot['pin']}"
            )


async def test_device_diagnostics_slot_device(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """Test device diagnostics for a slot device."""
    dev_reg = dr.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    slot_device = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"{entry_id}|1"), entry_id
    )
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
        assert "worker" in result
    else:
        # No device entry — device diagnostics falls through to config entry
        dev_reg = dr.async_get(hass)
        entry_id = lock_code_manager_config_entry.entry_id
        ce_device = dev_reg.async_get_device_by_identifier((DOMAIN, entry_id), entry_id)
        assert ce_device is not None
        result = await async_get_device_diagnostics(
            hass, lock_code_manager_config_entry, ce_device
        )
        assert "locks" in result
        assert LOCK_1_ENTITY_ID in result["locks"]


async def test_device_diagnostics_for_a_device_that_is_not_a_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A device with no slot in its identifier gets the whole entry instead."""
    dev_reg = dr.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    other = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, f"{entry_id}|not-a-slot")},
        name="Something else",
    )

    result = await async_get_device_diagnostics(
        hass, lock_code_manager_config_entry, other
    )

    # Falls through to config entry diagnostics (superset)
    assert "locks" in result
    assert "slots" in result


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

    # An entity keyed by a credential (the PIN text, the code sensor) reads
    # **REDACTED**. Verify at least one entity is redacted.
    redacted = [e for e in all_entities if e["state"] == "**REDACTED**"]
    assert len(redacted) > 0, "Expected at least one redacted entity"

    for entity in redacted:
        assert entity["attributes"] == {}
        assert entity["platform"] == DOMAIN


async def test_the_pin_in_sync_sensor_is_not_redacted(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A sensor whose key starts with ``pin`` holds no PIN; it is what the download is for."""
    entry = lock_code_manager_config_entry
    entity_id = pin_in_sync_entity_id(hass, entry, 1)
    await async_enable_and_reload(hass, entry, entity_id)

    result = await async_get_config_entry_diagnostics(hass, entry)
    reported = next(
        entity
        for section in ("slots", "locks")
        for diag in result[section].values()
        for entity in diag.get("entities", [])
        if entity["entity_id"] == entity_id
    )
    assert reported["state"] != "**REDACTED**"


async def test_mask_code_covers_missing_and_edge_case_credentials(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """``_mask_code`` covers distinct branches of its label-to-string mapping.

    A slot being checked (SlotCode.CHECKING), a cleared slot (SlotCode.EMPTY),
    and a present-but-empty-string PIN (the falsy-label fallback) all
    surface differently in diagnostics output.
    """
    entry = lock_code_manager_config_entry
    lock_1 = mock_lock(entry)

    # Slot 1: cleared on the lock -> SlotCode.EMPTY sentinel.
    await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
    await hass.async_block_till_done()
    assert 1 not in lock_1.codes

    # Add slot 3 to config, but lock 1 never answers a read of it: on a lock
    # that has answered, slot 3 is being checked.
    lock_1.fail("get", 3)
    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][1][CONF_ENABLED] = False  # as switched off above
    new_config[CONF_SLOTS][3] = {
        CONF_NAME: "test3",
        CONF_PIN: "9999",
        CONF_ENABLED: False,
    }
    write_entry_config(hass, entry, new_config)
    await hass.async_block_till_done()

    # Slot 9, which nobody manages: present but with an empty-string PIN --
    # the falsy-label fallback distinct from the SlotCode.EMPTY sentinel above.
    lock_1._push_credential_update(9, SlotCredential.known(""))
    await hass.async_block_till_done()

    result = await async_get_config_entry_diagnostics(hass, entry)

    lock_1_data = result["locks"][LOCK_1_ENTITY_ID]["worker"]["data"]
    assert lock_1_data["1"] == "empty"
    assert lock_1_data["9"] == "empty"
    assert lock_1_data["3"] == "checking"

    assert result["slots"]["1"]["locks"][LOCK_1_ENTITY_ID] == {"code": "empty"}
    assert result["slots"]["3"]["locks"][LOCK_1_ENTITY_ID] == {"code": "checking"}
    lock_1.clear_faults()
