"""Sensor for lock_code_manager."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_CODE, ATTR_PIN_SYNCED_TO_LOCKS, CONF_PIN, COORDINATORS, DOMAIN
from .coordinator import LockUsercodeUpdateCoordinator
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
        BaseLockCodeManagerEntity.__init__(
            self, config_entry, [lock], slot_num, ATTR_CODE
        )
        CoordinatorEntity.__init__(self, coordinator)
        self.lock = lock
        if lock.device_entry:
            self._attr_device_info = DeviceInfo(
                connections=lock.device_entry.connections,
                identifiers=lock.device_entry.identifiers,
            )

        self._attr_unique_id = (
            f"{self.base_unique_id}|{slot_num}|{self.key}|{self.lock.lock.entity_id}"
        )

    @property
    def native_value(self) -> str | None:
        """Return native value."""
        return self.coordinator.data.get(
            self.slot_num, self.coordinator.data.get(int(self.slot_num))
        )

    @property
    def available(self) -> bool:
        """Return whether sensor is available or not."""
        return (
            self.slot_num in self.coordinator.data
            or int(self.slot_num) in self.coordinator.data
        )

    @callback
    def _handle_remove_lock(self, entity_id: str) -> None:
        """Handle lock entity is being removed."""
        if self.lock.lock.entity_id == entity_id:
            self._internal_async_remove()

    @callback
    def _check_desired_pin(self) -> None:
        """Check binary sensor and send update signal if needed."""
        pin_enabled_entity_id = self.ent_reg.async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            f"{self.base_unique_id}|{self.slot_num}|{ATTR_PIN_SYNCED_TO_LOCKS}",
        )
        if not pin_enabled_entity_id:
            return
        pin_enabled_state = self.hass.states.get(pin_enabled_entity_id)
        if not pin_enabled_state:
            return

        pin_entity_id = self.ent_reg.async_get_entity_id(
            TEXT_DOMAIN,
            DOMAIN,
            f"{self.base_unique_id}|{self.slot_num}|{CONF_PIN}",
        )
        if not pin_entity_id:
            return
        pin_state = self.hass.states.get(pin_entity_id)
        if not pin_state:
            return

        if (pin_enabled_state.state == STATE_ON and pin_state.state != self.state) or (
            pin_enabled_state.state == STATE_OFF and self.state != ""
        ):
            _LOGGER.info(
                "%s (%s): Lock %s slot %s PIN value does not match expected state, updating",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.lock,
                self.slot_num,
            )
            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_update_usercode_{self.slot_num}",
            )

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await CoordinatorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        self.async_on_remove(
            self.coordinator.async_add_listener(
                self._check_desired_pin, self.coordinator_context
            )
        )

        if self.native_value is None:
            self.hass.async_create_task(
                self.async_update(), f"Force update {self.entity_id}"
            )
