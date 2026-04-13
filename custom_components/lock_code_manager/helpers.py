"""Helpers for lock_code_manager."""

from __future__ import annotations

import copy
import logging
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENTITY_ID,
)
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import CONF_LOCKS, CONF_SLOTS, DOMAIN, EXCLUDED_CONDITION_PLATFORMS
from .data import get_entry_data
from .providers import INTEGRATIONS_CLASS_MAP, BaseLock

_LOGGER = logging.getLogger(__name__)


@callback
def async_create_lock_instance(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    config_entry: ConfigEntry,
    lock_entity_id: str,
) -> BaseLock:
    """Generate lock from config entry."""
    lock_entry = ent_reg.async_get(lock_entity_id)
    assert lock_entry
    lock_config_entry = hass.config_entries.async_get_entry(lock_entry.config_entry_id)
    lock = INTEGRATIONS_CLASS_MAP[lock_entry.platform](
        hass, dev_reg, ent_reg, lock_config_entry, lock_entry
    )
    _LOGGER.debug(
        "%s (%s): Created lock instance %s",
        config_entry.entry_id,
        config_entry.title,
        lock,
    )
    return lock


def get_locks_from_targets(
    hass: HomeAssistant, target_data: dict[str, Any]
) -> set[BaseLock]:
    """Get lock(s) from target IDs."""
    # Targets can be a single string or list; normalize for consistent iteration.
    area_ids: list[str] = cv.ensure_list(target_data.get(ATTR_AREA_ID, []))
    device_ids: list[str] = cv.ensure_list(target_data.get(ATTR_DEVICE_ID, []))
    entity_ids: list[str] = cv.ensure_list(target_data.get(ATTR_ENTITY_ID, []))
    lock_entity_ids: set[str] = set()
    lcm_lock_entity_ids: set[str] = set(hass.data[DOMAIN][CONF_LOCKS].keys())
    ent_reg = er.async_get(hass)
    lock_entity_ids.update(
        ent.entity_id
        for area_id in area_ids
        for ent in er.async_entries_for_area(ent_reg, area_id)
        if ent.domain == LOCK_DOMAIN
    )
    lock_entity_ids.update(
        ent.entity_id
        for device_id in device_ids
        for ent in er.async_entries_for_device(ent_reg, device_id)
        if ent.domain == LOCK_DOMAIN
    )
    # Split invalid (non-lock domain) from unmanaged lock entities for clearer logs.
    invalid_entities: set[str] = set()
    unmanaged_entities: set[str] = set()
    for entity_id in entity_ids:
        domain = split_entity_id(entity_id)[0]
        if domain != LOCK_DOMAIN:
            invalid_entities.add(entity_id)
            continue
        if entity_id in lcm_lock_entity_ids:
            lock_entity_ids.add(entity_id)
        else:
            unmanaged_entities.add(entity_id)

    if invalid_entities:
        _LOGGER.warning(
            "%s lock(s) are invalid lock entities: %s",
            len(invalid_entities),
            ", ".join(invalid_entities),
        )
    if unmanaged_entities:
        _LOGGER.warning(
            "%s lock(s) are not managed by Lock Code Manager: %s",
            len(unmanaged_entities),
            ", ".join(unmanaged_entities),
        )

    return {
        lock
        for ent_id in (lock_entity_ids & lcm_lock_entity_ids)
        if (lock := hass.data[DOMAIN][CONF_LOCKS].get(ent_id))
    }


def get_managed_lock(hass: HomeAssistant, lock_entity_id: str) -> BaseLock:
    """Get a managed lock by entity ID, raising if not found."""
    lock = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {}).get(lock_entity_id)
    if not lock:
        raise ServiceValidationError(
            f"Lock {lock_entity_id} is not managed by Lock Code Manager"
        )
    return lock


async def async_set_usercode(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int, usercode: str
) -> None:
    """Set a usercode on a lock slot."""
    usercode = usercode.strip()
    if not usercode:
        raise ServiceValidationError(
            "Usercode must not be empty; use the clear operation instead"
        )
    lock = get_managed_lock(hass, lock_entity_id)
    await lock.async_internal_set_usercode(code_slot, usercode)


async def async_clear_usercode(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int
) -> None:
    """Clear a usercode from a lock slot."""
    lock = get_managed_lock(hass, lock_entity_id)
    await lock.async_internal_clear_usercode(code_slot)


def get_slot_config(config_entry: ConfigEntry, slot_num: int) -> dict[str, Any]:
    """Get slot config dict, raising if not found."""
    slots = get_entry_data(config_entry, CONF_SLOTS, {})
    slot_key = slot_num if slot_num in slots else str(slot_num)
    if slot_key not in slots:
        raise ServiceValidationError(f"Slot {slot_num} not found in config entry")
    return slots[slot_key]


def get_loaded_config_entry(hass: HomeAssistant, config_entry_id: str) -> ConfigEntry:
    """Get a loaded config entry by ID, raising if not found or not loaded."""
    config_entry = hass.config_entries.async_get_entry(config_entry_id)
    if not config_entry or config_entry.domain != DOMAIN:
        raise ServiceValidationError(
            f"No lock code manager config entry with ID `{config_entry_id}` found"
        )
    if config_entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"Config entry {config_entry.entry_id} not loaded")
    return config_entry


async def async_set_slot_condition(
    hass: HomeAssistant, config_entry_id: str, slot: int, entity_id: str
) -> None:
    """Set a condition entity for a slot."""
    config_entry = get_loaded_config_entry(hass, config_entry_id)
    get_slot_config(config_entry, slot)

    # Verify entity exists
    if not hass.states.get(entity_id):
        raise ServiceValidationError(f"Entity {entity_id} not found")

    # Check for excluded platforms
    ent_reg = er.async_get(hass)
    entity_entry = ent_reg.async_get(entity_id)
    if entity_entry and entity_entry.platform in EXCLUDED_CONDITION_PLATFORMS:
        raise ServiceValidationError(
            f"Entities from the '{entity_entry.platform}' integration are not "
            "supported as condition entities. See the wiki for details: "
            "https://github.com/raman325/lock_code_manager/wiki/"
            "Unsupported-Condition-Entity-Integrations"
        )

    # Update config entry data using effective config (handles data vs options)
    data = {
        CONF_LOCKS: copy.deepcopy(get_entry_data(config_entry, CONF_LOCKS, [])),
        CONF_SLOTS: copy.deepcopy(get_entry_data(config_entry, CONF_SLOTS, {})),
    }
    slot_key = slot if slot in data[CONF_SLOTS] else str(slot)
    data[CONF_SLOTS][slot_key][CONF_ENTITY_ID] = entity_id

    hass.config_entries.async_update_entry(config_entry, options=data)


async def async_clear_slot_condition(
    hass: HomeAssistant, config_entry_id: str, slot: int
) -> None:
    """Clear the condition entity from a slot."""
    config_entry = get_loaded_config_entry(hass, config_entry_id)
    get_slot_config(config_entry, slot)

    # Update config entry data using effective config (handles data vs options)
    data = {
        CONF_LOCKS: copy.deepcopy(get_entry_data(config_entry, CONF_LOCKS, [])),
        CONF_SLOTS: copy.deepcopy(get_entry_data(config_entry, CONF_SLOTS, {})),
    }
    slot_key = slot if slot in data[CONF_SLOTS] else str(slot)
    data[CONF_SLOTS][slot_key].pop(CONF_ENTITY_ID, None)

    hass.config_entries.async_update_entry(config_entry, options=data)
