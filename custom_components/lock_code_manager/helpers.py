"""Helpers for lock_code_manager."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector as sel,
)

from .const import CONF_CALENDAR, CONF_LOCKS, CONF_NUMBER_OF_USES, DOMAIN
from .exceptions import ConfigEntryNotFoundError
from .providers import INTEGRATIONS, BaseLock

_LOGGER = logging.getLogger(__name__)


CODE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_PIN): cv.string,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Required(CONF_CALENDAR, default=False): cv.boolean,
        vol.Optional(CONF_NUMBER_OF_USES, default=-1): sel.NumberSelector(
            sel.NumberSelectorConfig(
                min=-1, max=999999999, mode=sel.NumberSelectorMode.BOX
            )
        ),
    }
)


CODE_SLOTS_SCHEMA = vol.Schema({vol.Coerce(int): CODE_SLOT_SCHEMA})


@callback
def create_lock_instance(
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
    return INTEGRATIONS[lock_entry.platform](
        hass, dev_reg, ent_reg, config_entry, lock_config_entry, lock_entry
    )


def get_lock_from_entity_id(hass: HomeAssistant, entity_id: str) -> BaseLock:
    """Get lock from entity ID."""
    for data in hass.data[DOMAIN].values():
        lock: BaseLock = data[CONF_LOCKS]
        if entity_id == lock.lock.entity_id:
            return lock
    raise ConfigEntryNotFoundError(f"Lock with entity ID {entity_id} not found.")
