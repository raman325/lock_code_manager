"""Diagnostics support for Lock Code Manager."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import CONF_LOCKS, DOMAIN
from .data import get_entry_config
from .models import SlotCode
from .providers._base import BaseLock
from .util import mask_pin


def _get_instance_id(hass: HomeAssistant) -> str:
    """Return the HA instance UUID for PIN masking."""
    return hass.data.get("core.uuid", "unknown")


def _mask_code(
    code: str | SlotCode | None, lock_entity_id: str, instance_id: str
) -> str | None:
    """Mask a PIN code for diagnostics output."""
    if code is None:
        return None
    if isinstance(code, SlotCode):
        return code.value
    if not code:
        return "empty"
    return mask_pin(code, lock_entity_id, instance_id)


def _entity_states_for_device(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    device_id: str,
) -> list[dict[str, Any]]:
    """Return entity states for all entities on a device."""
    return [
        {
            "entity_id": entry.entity_id,
            "platform": entry.platform,
            "state": state.state
            if (state := hass.states.get(entry.entity_id))
            else "unavailable",
            "attributes": dict(state.attributes) if state else {},
        }
        for entry in er.async_entries_for_device(ent_reg, device_id)
    ]


def _lock_diagnostic(
    hass: HomeAssistant,
    lock: BaseLock,
    instance_id: str,
    ent_reg: er.EntityRegistry,
) -> dict[str, Any]:
    """Build diagnostic data for a single lock."""
    coordinator = lock.coordinator
    coordinator_data: dict[str, str | None] = {}
    if coordinator and coordinator.data:
        coordinator_data = {
            str(slot): _mask_code(code, lock.lock.entity_id, instance_id)
            for slot, code in coordinator.data.items()
        }

    result: dict[str, Any] = {
        "entity_id": lock.lock.entity_id,
        "domain": lock.domain,
        "supports_push": lock.supports_push,
        "supports_code_slot_events": lock.supports_code_slot_events,
        "coordinator": {
            "last_update_success": (
                coordinator.last_update_success if coordinator else None
            ),
            "slot_sync_mgrs_suspended": (
                coordinator.slot_sync_mgrs_suspended if coordinator else None
            ),
            "data": coordinator_data,
        },
    }

    if lock.device_entry:
        result["entities"] = _entity_states_for_device(
            hass, ent_reg, lock.device_entry.id
        )

    return result


def _slot_diagnostic(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    locks: dict[str, BaseLock],
    instance_id: str,
    ent_reg: er.EntityRegistry,
    dev_reg: dr.DeviceRegistry,
) -> dict[str, Any]:
    """Build diagnostic data for a single slot device."""
    entry_config = get_entry_config(config_entry)
    slot_config = entry_config.slot(slot_num)

    raw_pin = slot_config.get("pin")
    masked_pin = mask_pin(raw_pin, "configured", instance_id) if raw_pin else None

    result: dict[str, Any] = {
        "slot_num": slot_num,
        "pin": masked_pin,
        "enabled": slot_config.get("enabled"),
        "name": slot_config.get("name"),
    }

    # Per-lock coordinator code for this slot
    per_lock: dict[str, dict[str, Any]] = {}
    for lock_id, lock in locks.items():
        if not entry_config.has_lock(lock_id):
            continue
        lock_slot: dict[str, Any] = {}
        if lock.coordinator and lock.coordinator.data:
            lock_slot["coordinator_code"] = _mask_code(
                lock.coordinator.data.get(slot_num), lock_id, instance_id
            )
        per_lock[lock_id] = lock_slot
    result["locks"] = per_lock

    # Find the slot device and its entities
    slot_identifier = (DOMAIN, f"{config_entry.entry_id}|{slot_num}")
    device = dev_reg.async_get_device(identifiers={slot_identifier})
    if device:
        result["entities"] = _entity_states_for_device(hass, ent_reg, device.id)

    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the config entry (superset of all data)."""
    instance_id = _get_instance_id(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entry_config = get_entry_config(config_entry)
    all_locks: dict[str, BaseLock] = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})

    # Locks managed by this config entry
    locks_diag = {}
    for lock_id, lock in all_locks.items():
        if entry_config.has_lock(lock_id):
            locks_diag[lock_id] = _lock_diagnostic(hass, lock, instance_id, ent_reg)

    # Slots managed by this config entry
    slots_diag = {}
    for slot_num in entry_config.slots:
        slots_diag[str(slot_num)] = _slot_diagnostic(
            hass, config_entry, slot_num, all_locks, instance_id, ent_reg, dev_reg
        )

    return {
        "config_entry": {
            "entry_id": config_entry.entry_id,
            "title": config_entry.title,
            "state": config_entry.state.value,
        },
        "locks": locks_diag,
        "slots": slots_diag,
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a specific device."""
    instance_id = _get_instance_id(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entry_config = get_entry_config(config_entry)
    all_locks: dict[str, BaseLock] = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})

    # Check if this is a slot device: (DOMAIN, entry_id|slot_num)
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN and "|" in str(identifier[1]):
            parts = str(identifier[1]).split("|", 1)
            if len(parts) == 2 and parts[1].isdigit():
                slot_num = int(parts[1])
                return _slot_diagnostic(
                    hass,
                    config_entry,
                    slot_num,
                    all_locks,
                    instance_id,
                    ent_reg,
                    dev_reg,
                )

    # Check if this is a lock device (from an external integration)
    for lock_id, lock in all_locks.items():
        if (
            entry_config.has_lock(lock_id)
            and lock.device_entry
            and lock.device_entry.id == device.id
        ):
            return _lock_diagnostic(hass, lock, instance_id, ent_reg)

    # Config entry device or unknown — return the full config entry diagnostic
    return await async_get_config_entry_diagnostics(hass, config_entry)
