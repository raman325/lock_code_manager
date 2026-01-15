"""Helpers for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import CONF_LOCKS, DOMAIN
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
