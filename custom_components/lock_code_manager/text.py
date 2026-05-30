"""Text for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BaseLockCodeManagerEntity
from .models import LockCodeManagerConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_standard_text_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add standard text entities for slot."""
        async_add_entities(
            [
                LockCodeManagerText(hass, ent_reg, config_entry, slot_num, *props)
                for props in ((CONF_NAME, TextMode.TEXT), (CONF_PIN, TextMode.PASSWORD))
            ],
            True,
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_standard_adder(
            add_standard_text_entities
        )
    )

    return True


class LockCodeManagerText(BaseLockCodeManagerEntity, TextEntity):
    """Text entity for lock code manager."""

    _attr_native_min = 0
    _attr_native_max = 9999

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
        key: str,
        text_mode: TextMode,
    ) -> None:
        """Initialize Text entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, key
        )
        self._attr_mode = text_mode

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        return self._state

    async def async_set_value(self, value: str) -> None:
        """Set value of text."""
        coordinator = self.config_entry.runtime_data.slot_coordinators.get(
            self.slot_num
        )
        if coordinator is None:
            _LOGGER.warning(
                "%s (%s): No slot coordinator for slot %s; cannot apply %s update",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.slot_num,
                self.key,
            )
            return

        if self.key == CONF_PIN:
            await coordinator.async_request_pin_update(value)
        else:
            await coordinator.async_request_name_update(value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await TextEntity.async_added_to_hass(self)

        coordinator = self.config_entry.runtime_data.slot_coordinators.get(
            self.slot_num
        )
        if coordinator is not None:
            self.async_on_remove(
                coordinator.register_state_subscriber(self.async_write_ha_state)
            )
