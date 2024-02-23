"""Helpers for lock_code_manager."""

from __future__ import annotations

import copy
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.components.persistent_notification import async_create
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector as sel,
)

from .const import CONF_CALENDAR, CONF_LOCKS, CONF_NUMBER_OF_USES, DOMAIN
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
    if not lock_entry:
        # If there are locks in the config other than the one that's invalid,
        # automatically remove it from the config and let the user know since they
        # can always go through the options flow to update it. If the invalid lock is
        # the only lock in the config, we must start a reauth flow so the user can
        # pick new lock(s).
        if len(config_entry.data[CONF_LOCKS]) > 1:
            locks = copy.deepcopy(config_entry.options[CONF_LOCKS])
            locks.pop(lock_entity_id)
            hass.config_entries.async_update_entry(
                config_entry, options={**config_entry.options, CONF_LOCKS: locks}
            )
            async_create(
                hass,
                (
                    f"Lock with entity ID {lock_entity_id} not found. This lock has "
                    f"been removed from the {config_entry.title} Lock Code Manager "
                    "configuration. To make any additional changes to the locks "
                    "included in this configuration, you must reconfigure the config "
                    "entry."
                ),
                "Lock not found",
            )
            hass.async_create_task(
                hass.config_entries.async_reload(config_entry.entry_id),
                f"Reload config entry {config_entry.entry_id}",
            )
        else:
            config_entry.async_start_reauth(
                hass, context={"reason": "lock_not_found", "lock": lock_entity_id}
            )
        raise HomeAssistantError(f"Lock with entity ID {lock_entity_id} not found.")
    lock_config_entry = hass.config_entries.async_get_entry(lock_entry.config_entry_id)
    assert lock_config_entry
    lock = INTEGRATIONS_CLASS_MAP[lock_entry.platform](
        hass, dev_reg, ent_reg, config_entry, lock_config_entry, lock_entry
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
            for data in hass.data[DOMAIN].values()
            for lock in data[CONF_LOCKS].values()
            if entity_id == lock.lock.entity_id
        )
    except StopIteration:
        raise ConfigEntryNotFoundError(
            f"Lock with entity ID {entity_id} not found."
        ) from None
