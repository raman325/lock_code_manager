"""Binary sensor entities for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_ACTIVE, ATTR_IN_SYNC, ATTR_SYNC_STATUS
from .domain.coordinator import LockUsercodeUpdateCoordinator
from .domain.credentials import CredentialAddress, pin_address
from .domain.models import LockCodeManagerConfigEntry
from .domain.queries import subentry_id_for_slot
from .domain.sync import SlotSyncManager, fold_in_sync, fold_sync_status
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity, BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_pin_active_entity(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add active binary sensor entities for slot."""
        async_add_entities(
            [
                LockCodeManagerActiveEntity(
                    hass, ent_reg, config_entry, slot_num, ATTR_ACTIVE
                )
            ],
            True,
            config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
        )

    @callback
    def add_code_slot_entities(
        lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ):
        """Add code slot sensor entities for slot."""
        coordinator = lock.coordinator
        if coordinator is None:
            return
        async_add_entities(
            [
                LockCodeManagerCodeSlotInSyncEntity(
                    hass, ent_reg, config_entry, coordinator, lock, slot_num
                ),
                LockCodeManagerCredentialInSyncEntity(
                    hass,
                    ent_reg,
                    config_entry,
                    coordinator,
                    lock,
                    slot_num,
                    pin_address(slot_num),
                ),
            ],
            True,
            config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
        )

    callbacks = config_entry.runtime_data.callbacks
    config_entry.async_on_unload(
        callbacks.register_standard_adder(add_pin_active_entity)
    )
    config_entry.async_on_unload(
        callbacks.register_lock_slot_adder(add_code_slot_entities)
    )
    return True


class LockCodeManagerActiveEntity(BaseLockCodeManagerEntity, BinarySensorEntity):
    """
    Active binary sensor entity for lock code manager.

    Read-only view over ``SlotEntityCoordinator``. The coordinator owns
    the condition-entity subscription and the active-state computation;
    this entity only renders the current value.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @callback
    def _apply_coordinator_state(
        self, is_on: bool | None, inactive_because_of: list[str]
    ) -> None:
        """Apply coordinator-derived active state and write Home Assistant state."""
        self._attr_is_on = bool(is_on)
        if inactive_because_of:
            self._attr_extra_state_attributes["inactive_because_of"] = list(
                inactive_because_of
            )
        else:
            self._attr_extra_state_attributes.pop("inactive_because_of", None)
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

    def _register_slot_coordinator_subscription(self) -> None:
        """Subscribe to the derived active-state view rather than the generic state poke."""
        # Type narrowing; the base only calls this hook when set.
        assert self._slot_coordinator is not None
        self.async_on_remove(
            self._slot_coordinator.register_active_view(self._apply_coordinator_state)
        )


class LockCodeManagerCodeSlotInSyncEntity(
    BaseLockCodeManagerCodeSlotPerLockEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
    BinarySensorEntity,
):
    """
    In-sync binary sensor for the user's whole record on this lock.

    A view over every sync manager for this user on this lock: on when all
    of them are, and reporting the worst of their statuses. The managers
    live in the entry's runtime data; this entity neither starts nor stops
    them, so disabling it never stops a sync.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        coordinator: LockUsercodeUpdateCoordinator,
        lock: BaseLock,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, ATTR_IN_SYNC
        )
        CoordinatorEntity.__init__(self, coordinator)
        self._attr_sync_status: str | None = None
        self._managers: list[SlotSyncManager] = []

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and all(
            self.coordinator.has_credential(manager.address)
            for manager in self._managers
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return extra state attributes."""
        if self._attr_sync_status is None:
            return {}
        return {ATTR_SYNC_STATUS: self._attr_sync_status}

    @callback
    def _fold(self, *_args: Any) -> None:
        """Recompute this sensor from every manager and write the result."""
        self._attr_is_on = fold_in_sync(manager.in_sync for manager in self._managers)
        self._attr_sync_status = fold_sync_status(
            manager.sync_status for manager in self._managers
        )
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)
        coordinator = self._slot_coordinator
        self._managers = (
            coordinator.sync_managers_for(self.lock.lock.entity_id)
            if coordinator is not None
            else []
        )
        for manager in self._managers:
            self.async_on_remove(manager.async_add_listener(self._fold))
        self._fold()


class LockCodeManagerCredentialInSyncEntity(
    BaseLockCodeManagerCodeSlotPerLockEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
    BinarySensorEntity,
):
    """
    In-sync binary sensor for one credential type of the user on this lock.

    A view over one sync manager. With only PINs managed it reads the same
    as the ``in_sync`` sensor, so it is disabled by default: the split is
    there for whoever wants it, and a second credential type arrives as
    another sensor of this class rather than a new shape.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        coordinator: LockUsercodeUpdateCoordinator,
        lock: BaseLock,
        slot_num: int,
        address: CredentialAddress,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self,
            hass,
            ent_reg,
            config_entry,
            lock,
            slot_num,
            f"{address.credential_type.value}_{ATTR_IN_SYNC}",
        )
        CoordinatorEntity.__init__(self, coordinator)
        self._address = address
        self._attr_sync_status: str | None = None

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and (
            self.coordinator.has_credential(self._address)
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return extra state attributes."""
        if self._attr_sync_status is None:
            return {}
        return {ATTR_SYNC_STATUS: self._attr_sync_status}

    @callback
    def _mirror(self, in_sync: bool | None, sync_status: str | None) -> None:
        """Take the manager's state as this sensor's own."""
        self._attr_is_on = in_sync
        self._attr_sync_status = sync_status
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)
        coordinator = self._slot_coordinator
        manager = (
            coordinator.sync_manager(self.lock.lock.entity_id, self._address)
            if coordinator is not None
            else None
        )
        if manager is None:
            return
        self.async_on_remove(manager.async_add_listener(self._mirror))
        self._mirror(manager.in_sync, manager.sync_status)
