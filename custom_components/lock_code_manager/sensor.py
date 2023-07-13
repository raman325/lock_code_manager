"""Sensor for lock_code_manager."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import LockUsercodeUpdateCoordinator
from .const import COORDINATORS, DOMAIN
from .entity import BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Setup config entry."""

    @callback
    def add_code_slot_entities(lock: BaseLock, slot_num: int) -> None:
        """Add code slot sensor entities for slot."""
        coordinator: LockUsercodeUpdateCoordinator = hass.data[DOMAIN][
            config_entry.entry_id
        ][COORDINATORS][lock.lock.entity_id]
        async_add_entities(
            [LockCodeManagerCodeSlot(config_entry, lock, coordinator, slot_num)], True
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add_lock_slot_sensor",
            add_code_slot_entities,
        )
    )
    return True


class LockCodeManagerCodeSlot(
    BaseLockCodeManagerEntity,
    SensorEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
):
    """Code slot sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lock-smart"

    def __init__(
        self,
        config_entry: ConfigEntry,
        lock: BaseLock,
        coordinator: LockUsercodeUpdateCoordinator,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(self, config_entry, [lock], slot_num, "code")
        CoordinatorEntity.__init__(self, coordinator)
        self.lock = lock
        self._attr_device_info = lock.device_info

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        return self.coordinator.data.get(
            self.slot_num, self.coordinator.data[int(self.slot_num)]
        )

    @property
    def available(self) -> bool:
        """Return whether sensor is available or not."""
        return (
            self.slot_num in self.coordinator.data
            or int(self.slot_num) in self.coordinator.data
        )

    @callback
    def dispatcher_connect(self) -> None:
        """Connect entity to dispatcher signals."""
        entry = self.config_entry
        lock_entity_id = self.lock.lock.entity_id
        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_lock_slot_sensors_{lock_entity_id}",
                self.internal_async_remove,
            )
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await CoordinatorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
