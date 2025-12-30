"""Sensor for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_CODE
from .coordinator import LockUsercodeUpdateCoordinator
from .data import LockCodeManagerConfigEntry
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_code_slot_entities(
        lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ) -> None:
        """Add code slot sensor entities for slot."""
        coordinator = lock.coordinator
        if coordinator is None:
            _LOGGER.warning(
                "%s (%s): Coordinator missing for lock %s when adding slot %s entities",
                config_entry.entry_id,
                config_entry.title,
                lock.lock.entity_id,
                slot_num,
            )
            return
        async_add_entities(
            [
                LockCodeManagerCodeSlotSensorEntity(
                    hass, ent_reg, config_entry, lock, coordinator, slot_num
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_per_lock_adder(
            add_code_slot_entities
        )
    )
    return True


class LockCodeManagerCodeSlotSensorEntity(
    BaseLockCodeManagerCodeSlotPerLockEntity,
    SensorEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
):
    """Code slot sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        lock: BaseLock,
        coordinator: LockUsercodeUpdateCoordinator,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, ATTR_CODE
        )
        CoordinatorEntity.__init__(self, coordinator)

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        return self.coordinator.data.get(
            self.slot_num, self.coordinator.data.get(int(self.slot_num))
        )

    @property
    def available(self) -> bool:
        """Return whether sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and (
            int(self.slot_num) in self.coordinator.data
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)

        if self.native_value is None:
            self.hass.async_create_task(
                self.async_update(), f"Force update {self.entity_id}"
            )
