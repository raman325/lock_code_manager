"""Switch for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import CONF_ENABLED
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import BaseLockCodeManagerEntity
from .models import LockCodeManagerConfigEntry
from .slot_manager import PinRequiredError, SlotEntityCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_switch_entities(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add switch entities for slot."""
        async_add_entities(
            [
                LockCodeManagerSwitch(
                    hass, ent_reg, config_entry, slot_num, CONF_ENABLED
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_standard_adder(add_switch_entities)
    )
    return True


class LockCodeManagerSwitch(BaseLockCodeManagerEntity, SwitchEntity):
    """Switch entity for lock code manager."""

    @property
    def is_on(self) -> bool:
        """Return native value."""
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        """Turn on switch."""
        coordinator = self._require_coordinator()
        try:
            await coordinator.async_request_active_toggle(True)
        except PinRequiredError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Turn off switch."""
        coordinator = self._require_coordinator()
        await coordinator.async_request_active_toggle(False)
        self.async_write_ha_state()

    def _require_coordinator(self) -> SlotEntityCoordinator:
        """Return the slot coordinator, raising if it has not been created."""
        if self._slot_coordinator is None:
            raise HomeAssistantError(f"No slot coordinator for slot {self.slot_num}")
        return self._slot_coordinator
