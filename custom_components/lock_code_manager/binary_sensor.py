"""Binary sensor entities for lock_code_manager."""

import logging
from typing import override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ATTR_ACTIVE, ATTR_IN_SYNC, ATTR_SYNC_STATUS, credential_in_sync_key
from .entity import (
    BaseLockCodeManagerCodeSlotPerLockEntity,
    BaseLockCodeManagerEntity,
    LockWorkerView,
)
from .ha.entry import LockCodeManagerConfigEntry
from .model.credentials import CredentialType
from .model.queries import subentry_id_for_slot
from .providers import BaseLock
from .sync.slot import Status

_LOGGER = logging.getLogger(__name__)

SYNCING = "syncing"


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
        # The aggregate over every credential the slot holds on the lock,
        # then one sensor per credential type the lock takes.
        async_add_entities(
            [
                LockCodeManagerCodeSlotInSyncEntity(
                    hass, ent_reg, config_entry, lock, slot_num, *props
                )
                for props in (
                    (ATTR_IN_SYNC, None),
                    *(
                        (credential_in_sync_key(credential_type.value), credential_type)
                        for credential_type in sorted(lock.managed_credential_types)
                    ),
                )
            ],
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

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

    @override
    def _register_slot_coordinator_subscription(self) -> None:
        """Subscribe to the derived active-state view rather than the generic state poke."""
        # Type narrowing; the base only calls this hook when set.
        assert self._slot_coordinator is not None
        self.async_on_remove(
            self._slot_coordinator.register_active_view(self._apply_coordinator_state)
        )


class LockCodeManagerCodeSlotInSyncEntity(
    LockWorkerView,
    BaseLockCodeManagerCodeSlotPerLockEntity,
    BinarySensorEntity,
):
    """
    Whether the user's code on this lock is what it should be.

    On when the slot is in sync. ``sync_status`` says more: out of sync,
    syncing (a sync of it is queued or running), or suspended (held, with a
    repair). The aggregate ``in_sync`` sensor covers every managed
    credential; a per-credential sensor covers one. The lock's worker owns
    the sync; this entity only views it, so disabling it never stops one.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        lock: BaseLock,
        slot_num: int,
        key: str,
        credential_type: CredentialType | None,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, key
        )
        self._credential_type = credential_type
        # A per-type sensor reads the same as the aggregate on a lock with
        # one type, and splits it on one with more: only the aggregate ships
        # enabled, and whoever wants the split enables the rest.
        self._attr_entity_registry_enabled_default = key == ATTR_IN_SYNC

    @property
    @override
    def is_on(self) -> bool | None:
        """
        Return whether the slot is in sync.

        Off while the slot is being checked (unknown): it is not known to be
        in sync, and a write may be due, the same as pending.
        """
        if (status := self._core_status()) is None:
            return None
        return status is Status.IN_SYNC

    @property
    @override
    def extra_state_attributes(self) -> dict[str, str]:
        """Return extra state attributes."""
        status = self._core_status()
        if status is None:
            return {}
        if status is not Status.SUSPENDED and self._core_syncing():
            return {ATTR_SYNC_STATUS: SYNCING}
        if status is Status.UNKNOWN:
            return {}
        return {ATTR_SYNC_STATUS: status.value}

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        self._watch_lock()
