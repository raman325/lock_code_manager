"""Helpers for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector as sel,
)

from .const import CONF_CALENDAR, CONF_LOCKS, CONF_NUMBER_OF_USES, DOMAIN
from .providers import INTEGRATIONS_CLASS_MAP, BaseLock

_LOGGER = logging.getLogger(__name__)


UI_CODE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PIN): cv.string,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_CALENDAR): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=CALENDAR_DOMAIN)
        ),
        vol.Optional(CONF_NUMBER_OF_USES): sel.TextSelector(
            sel.TextSelectorConfig(type=sel.TextSelectorType.NUMBER)
        ),
    }
)

CODE_SLOT_SCHEMA = UI_CODE_SLOT_SCHEMA.extend(
    {vol.Optional(CONF_NUMBER_OF_USES): vol.Coerce(int)}
)


def enabled_requires_pin(data: dict[str, Any]) -> dict[str, Any]:
    """Validate that if enabled is True, pin is set."""
    if any(val.get(CONF_ENABLED) and not val.get(CONF_PIN) for val in data.values()):
        raise vol.Invalid("PIN must be set if enabled is True")
    return data


CODE_SLOTS_SCHEMA = vol.All(
    vol.Schema({vol.Coerce(int): CODE_SLOT_SCHEMA}), enabled_requires_pin
)


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
    assert lock_config_entry
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
