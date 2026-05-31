"""LCM service-handler implementations."""

from __future__ import annotations

from homeassistant.const import CONF_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from ..const import EXCLUDED_CONDITION_PLATFORMS
from .locks import get_managed_lock
from .queries import get_entry_config, get_loaded_config_entry


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


async def async_set_slot_condition(
    hass: HomeAssistant, config_entry_id: str, slot: int, entity_id: str
) -> None:
    """Set a condition entity for a slot."""
    config_entry = get_loaded_config_entry(hass, config_entry_id)
    config = get_entry_config(config_entry)
    if not config.has_slot(slot):
        raise ServiceValidationError(f"Slot {slot} not found in config entry")

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

    new_config = config.with_slot_field_set(slot, CONF_ENTITY_ID, entity_id)
    hass.config_entries.async_update_entry(config_entry, options=new_config.to_dict())


async def async_clear_slot_condition(
    hass: HomeAssistant, config_entry_id: str, slot: int
) -> None:
    """Clear the condition entity from a slot."""
    config_entry = get_loaded_config_entry(hass, config_entry_id)
    config = get_entry_config(config_entry)
    if not config.has_slot(slot):
        raise ServiceValidationError(f"Slot {slot} not found in config entry")

    new_config = config.with_slot_field_removed(slot, CONF_ENTITY_ID)
    hass.config_entries.async_update_entry(config_entry, options=new_config.to_dict())
