"""Helpers for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector as sel,
)

from .const import CONF_CALENDAR, CONF_LOCKS, CONF_NUMBER_OF_USES, CONF_SLOTS, DOMAIN
from .exceptions import ConfigEntryNotFoundError
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


def get_lock_from_entity_id(hass: HomeAssistant, entity_id: str) -> BaseLock:
    """Get lock from entity ID."""
    try:
        return next(
            lock
            for lock in hass.data[DOMAIN][CONF_LOCKS].values()
            if entity_id == lock.lock.entity_id
        )
    except StopIteration:
        raise ConfigEntryNotFoundError(
            f"Lock with entity ID {entity_id} not found."
        ) from None


def get_entry_data(config_entry: ConfigEntry, key: str, default: Any = {}) -> Any:
    """Get data from config entry."""
    return config_entry.data.get(key, config_entry.options.get(key, default))


def get_slot_data(config_entry, slot_num: int) -> dict[str, Any]:
    """Get data for slot."""
    return get_entry_data(config_entry, CONF_SLOTS, {}).get(slot_num, {})
