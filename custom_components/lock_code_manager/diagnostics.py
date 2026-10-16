"""Diagnostics support for Lock Code Manager."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DATA, DOMAIN, SENSITIVE_ENTITY_KEYS, LockCodeManagerData
from .ha.runtime import lock_codes, lock_registry
from .model.config import build_slot_device_identifier, slot_unique_id_key
from .model.credentials import SlotCode
from .model.pin_masking import mask_pin
from .model.queries import get_entry_config
from .providers._base import BaseLock


def _get_instance_id(hass: HomeAssistant) -> str:
    """Return the HA instance UUID for PIN masking."""
    return hass.data.get(DATA, LockCodeManagerData()).get("instance_id", "")


def _mask_code(
    code: str | SlotCode | None, slot_num: int | str, instance_id: str
) -> str | None:
    """Mask a PIN code for diagnostics output."""
    if code is None:
        return None
    if isinstance(code, SlotCode):
        return code.value
    if not code:
        return "empty"
    return mask_pin(code, slot_num, instance_id)


def _is_sensitive(entry: er.RegistryEntry) -> bool:
    """Return True if the entity may expose a credential in its state."""
    return (
        entry.platform == DOMAIN
        and slot_unique_id_key(entry.unique_id or "") in SENSITIVE_ENTITY_KEYS
    )


def _entity_states_for_device(
    hass: HomeAssistant,
    ent_reg: er.EntityRegistry,
    device_id: str,
) -> list[dict[str, Any]]:
    """
    Return entity states for all entities on a device.

    Sensitive entities (PIN text, code sensor) have their state and
    attributes redacted to avoid leaking PINs in diagnostics output.
    """
    entities: list[dict[str, Any]] = []
    for entry in er.async_entries_for_device(ent_reg, device_id):
        state = hass.states.get(entry.entity_id)
        friendly_name = (
            state.attributes.get("friendly_name") if state else entry.original_name
        )
        if _is_sensitive(entry):
            entities.append(
                {
                    "entity_id": entry.entity_id,
                    "friendly_name": friendly_name,
                    "platform": entry.platform,
                    "state": "**REDACTED**",
                    "attributes": {},
                }
            )
        else:
            entities.append(
                {
                    "entity_id": entry.entity_id,
                    "friendly_name": friendly_name,
                    "platform": entry.platform,
                    "state": state.state if state else "unavailable",
                    "attributes": dict(state.attributes) if state else {},
                }
            )
    return entities


def _lock_diagnostic(
    hass: HomeAssistant,
    lock: BaseLock,
    instance_id: str,
    ent_reg: er.EntityRegistry,
) -> dict[str, Any]:
    """Build diagnostic data for a single lock."""
    lock_entity_id = lock.lock.entity_id
    worker = lock_registry(hass).worker(lock_entity_id)
    result: dict[str, Any] = {
        "entity_id": lock_entity_id,
        "domain": lock.domain,
        "supports_push": lock.supports_push,
        "supports_code_slot_events": lock.supports_code_slot_events,
        "worker": None
        if worker is None
        else {
            "reachable": worker.reachable,
            "offline": worker.offline,
            "data": {
                str(slot): _mask_code(code, slot, instance_id)
                for slot, code in lock_codes(hass, lock_entity_id).items()
            },
            "slots": {
                str(slot_num): {
                    "status": worker.status(slot_num)[0].value,
                    "hold": slot.hold.value if slot.hold else None,
                    "tries": slot.tries,
                    "release": slot.release,
                }
                for slot_num, slot in worker.slots.items()
            },
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
    masked_pin = mask_pin(raw_pin, slot_num, instance_id) if raw_pin else None

    result: dict[str, Any] = {
        "slot_num": slot_num,
        "pin": masked_pin,
        "enabled": slot_config.get("enabled"),
        "name": slot_config.get("name"),
    }

    result["locks"] = {
        lock_id: (
            {"code": _mask_code(code, slot_num, instance_id)}
            if (code := lock_codes(hass, lock_id).get(slot_num)) is not None
            else {}
        )
        for lock_id in locks
        if entry_config.has_lock(lock_id)
    }

    # Find the slot device and its entities
    slot_identifier = (
        DOMAIN,
        build_slot_device_identifier(config_entry.entry_id, slot_num),
    )
    device = dev_reg.async_get_device_by_identifier(
        slot_identifier, config_entry.entry_id
    )
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
    runtime_data = getattr(config_entry, "runtime_data", None)
    all_locks: dict[str, BaseLock] = (
        dict(runtime_data.locks) if runtime_data is not None else {}
    )

    return {
        "config_entry": {
            "entry_id": config_entry.entry_id,
            "title": config_entry.title,
            "state": config_entry.state.value,
        },
        "locks": {
            lock_id: _lock_diagnostic(hass, lock, instance_id, ent_reg)
            for lock_id, lock in all_locks.items()
            if entry_config.has_lock(lock_id)
        },
        "slots": {
            str(slot_num): _slot_diagnostic(
                hass, config_entry, slot_num, all_locks, instance_id, ent_reg, dev_reg
            )
            for slot_num in entry_config.slot_numbers
        },
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry, device: dr.DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a specific device."""
    instance_id = _get_instance_id(hass)
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    entry_config = get_entry_config(config_entry)
    runtime_data = getattr(config_entry, "runtime_data", None)
    all_locks: dict[str, BaseLock] = (
        dict(runtime_data.locks) if runtime_data is not None else {}
    )

    # Check if this is a slot device: (DOMAIN, entry_id|slot_num)
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN and "|" in str(identifier[1]):
            parts = str(identifier[1]).split("|", 1)
            if (
                len(parts) == 2
                and parts[0] == config_entry.entry_id
                and parts[1].isdigit()
            ):
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
