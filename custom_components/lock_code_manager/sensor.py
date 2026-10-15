"""Sensor for lock_code_manager."""

from typing import override

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_CODE
from .core.slot import Holds, Seen, Status
from .domain.models import LockCodeManagerConfigEntry
from .domain.queries import subentry_id_for_slot
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity, LockWorkerView
from .providers import BaseLock


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
        async_add_entities(
            [
                LockCodeManagerCodeSlotSensorEntity(
                    hass, ent_reg, config_entry, lock, slot_num
                )
            ],
            True,
            config_subentry_id=subentry_id_for_slot(config_entry, slot_num),
        )

    config_entry.async_on_unload(
        config_entry.runtime_data.callbacks.register_lock_slot_adder(
            add_code_slot_entities
        )
    )
    return True


class LockCodeManagerCodeSlotSensorEntity(
    LockWorkerView,
    BaseLockCodeManagerCodeSlotPerLockEntity,
    SensorEntity,
):
    """
    What the lock holds in the slot: a PIN, or nothing.

    A lock that hides its codes shows the configured PIN once the write is
    trusted (the slot is in sync), and unknown until then.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        lock: BaseLock,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, ATTR_CODE
        )

    @property
    @override
    def native_value(self) -> str | None:
        """Return what the lock holds, as far as it is known."""
        if (slot := self._core_slot()) is None:
            return None
        if isinstance(slot.seen, Holds):
            return slot.seen.pin
        if slot.seen is Seen.EMPTY:
            return ""
        if self._core_status() is Status.IN_SYNC:
            return slot.want
        return None

    @property
    @override
    def available(self) -> bool:
        """Return whether the lock has said anything about the slot yet."""
        return (
            BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self)
            and (slot := self._core_slot()) is not None
            and slot.seen is not None
        )

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        self._watch_lock()
