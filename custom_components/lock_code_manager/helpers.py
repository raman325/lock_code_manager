"""Helpers for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

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
    area_ids: list[str] = target_data.get(ATTR_AREA_ID, [])
    device_ids: list[str] = target_data.get(ATTR_DEVICE_ID, [])
    entity_ids: list[str] = target_data.get(ATTR_ENTITY_ID, [])
    locks: set[BaseLock] = set()
    lock_entity_ids: set[str] = set()
    ent_reg = er.async_get(hass)
    for area_id in area_ids:
        lock_entity_ids.update(
            ent.entity_id
            for ent in er.async_entries_for_area(ent_reg, area_id)
            if ent.domain == LOCK_DOMAIN
        )
    for device_id in device_ids:
        lock_entity_ids.update(
            ent.entity_id
            for ent in er.async_entries_for_device(ent_reg, device_id)
            if ent.domain == LOCK_DOMAIN
        )
    for entity_id in entity_ids:
        if not entity_id.startswith(LOCK_DOMAIN):
            _LOGGER.warning(
                "Entity ID %s is not a lock entity, skipping",
                entity_id,
            )
            continue
        lock_entity_ids.add(entity_id)
    for lock_entity_id in lock_entity_ids:
        try:
            locks.add(
                next(
                    lock
                    for lock in hass.data[DOMAIN][CONF_LOCKS].values()
                    if lock_entity_id == lock.lock.entity_id
                )
            )
        except StopIteration:
            _LOGGER.warning(
                (
                    "Lock with entity ID %s does not have a Lock Code Manager entry, "
                    "skipping"
                ),
                entity_id,
            )

    return locks
