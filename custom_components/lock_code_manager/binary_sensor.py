"""Binary sensor entities for lock_code_manager."""

from __future__ import annotations

from collections.abc import Callable
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_ACTIVE, ATTR_IN_SYNC, ATTR_SYNC_STATUS
from .coordinator import LockUsercodeUpdateCoordinator
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity, BaseLockCodeManagerEntity
from .models import LockCodeManagerConfigEntry
from .providers import BaseLock
from .sync import SlotSyncManager
from .util import get_slot_coordinator

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
        )

    @callback
    def add_code_slot_entities(
        lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ):
        """Add code slot sensor entities for slot."""
        coordinator = get_slot_coordinator(config_entry, lock, slot_num)
        if coordinator is None:
            return
        async_add_entities(
            [
                LockCodeManagerCodeSlotInSyncEntity(
                    hass, ent_reg, config_entry, coordinator, lock, slot_num
                )
            ],
            True,
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
    _coordinator_view_unsub: Callable[[], None] | None = None

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

        coordinator = self.config_entry.runtime_data.slot_coordinators.get(
            self.slot_num
        )
        if coordinator is None:
            _LOGGER.warning(
                "%s (%s): No slot coordinator for slot %s when registering "
                "active binary sensor",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.slot_num,
            )
            return

        self._coordinator_view_unsub = coordinator.register_active_view(
            self._apply_coordinator_state
        )
        self.async_on_remove(self._coordinator_view_unsub)


class LockCodeManagerCodeSlotInSyncEntity(
    BaseLockCodeManagerCodeSlotPerLockEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
    BinarySensorEntity,
):
    """PIN synced binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _slot_coordinator_unsub: Callable[[], None] | None = None

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

        @callback
        def _sync_and_write_state(in_sync: bool | None) -> None:
            """Sync _attr_is_on from manager and write HA state."""
            self._attr_is_on = in_sync
            self._attr_sync_status = self._sync_manager.sync_status
            self.async_write_ha_state()

        self._sync_manager = SlotSyncManager(
            hass,
            ent_reg,
            config_entry,
            coordinator,
            lock,
            slot_num,
            state_writer=_sync_and_write_state,
        )

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and (
            int(self.slot_num) in self.coordinator.data
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return extra state attributes."""
        if self._attr_sync_status is None:
            return {}
        return {ATTR_SYNC_STATUS: self._attr_sync_status}

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)

        self.config_entry.runtime_data.sync_managers.add(self._sync_manager)
        slot_coordinator = self.config_entry.runtime_data.slot_coordinators.get(
            self.slot_num
        )
        if slot_coordinator is not None:
            self._slot_coordinator_unsub = slot_coordinator.register_sync_manager(
                self._sync_manager
            )
        await self._sync_manager.async_start()

    async def async_will_remove_from_hass(self) -> None:
        """Stop the sync manager and await its in-flight tick before removal."""
        # async_unload_entry stops sync managers up front so this is normally
        # a no-op (idempotent); the explicit stop here protects against entity
        # removal paths that do not flow through async_unload_entry, such as
        # a slot being removed via the options update listener.
        self.config_entry.runtime_data.sync_managers.discard(self._sync_manager)
        unsub = getattr(self, "_slot_coordinator_unsub", None)
        if unsub is not None:
            unsub()
            self._slot_coordinator_unsub = None
        await self._sync_manager.async_stop()
        await CoordinatorEntity.async_will_remove_from_hass(self)
