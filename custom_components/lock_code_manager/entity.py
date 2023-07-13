"""Base entity class for Lock Code Manager."""
from __future__ import annotations

import copy
from typing import Any, final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityCategory

from .const import ATTR_CODE_SLOT, CONF_SLOTS, DOMAIN
from .providers import BaseLock


class BaseLockCodeManagerEntity(Entity):
    """Base Lock Code Manager Entity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, config_entry: ConfigEntry, locks: list[BaseLock], slot_num: int, key: str
    ) -> None:
        """Initialize base entity."""
        self.config_entry = config_entry
        self.locks = locks
        self.slot_num = slot_num
        self.key = key
        self.ent_reg: er.EntityRegistry | None = None

        self._attr_name = f"Code slot {slot_num} {key.replace('_', ' ').lower()}"
        base_unique_id = "_".join(sorted([lock.lock.entity_id for lock in locks]))
        self._attr_unique_id = f"{base_unique_id}_{slot_num}_{key}"
        self._attr_extra_state_attributes = {ATTR_CODE_SLOT: int(slot_num)}

    @callback
    @final
    def _update_config_entry(self, value: Any) -> None:
        """Update config entry data."""
        data = copy.deepcopy(dict(self.config_entry.data))
        data[CONF_SLOTS][self.slot_num][self.key] = value
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=data, options={}
        )
        self.async_write_ha_state()

    async def internal_async_remove(self) -> None:
        """
        Handle entity removal.

        Should not be overwritten by platforms.
        """
        entity_id = self.entity_id
        await self._async_remove()
        await self.async_remove(force_remove=True)
        self.ent_reg.async_remove(entity_id)

    async def _async_remove(self) -> None:
        """
        Handle entity removal.

        Can be overwritten by platforms.
        """
        pass

    @callback
    def dispatcher_connect(self) -> None:
        """
        Connect entity to dispatcher signals.
        
        Can be overwritten by platforms if necessary
        """
        entry = self.config_entry
        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_{self.slot_num}_{self.key}",
                self.internal_async_remove,
            )
        )
        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_{self.slot_num}",
                self.internal_async_remove,
            )
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await Entity.async_added_to_hass(self)
        if not self.ent_reg:
            self.ent_reg = er.async_get(self.hass)
        self.dispatcher_connect()
