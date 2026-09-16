"""Binary sensor entities for lock_code_manager."""

from __future__ import annotations

from collections.abc import Iterable
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_ACTIVE, ATTR_IN_SYNC, ATTR_SYNC_STATUS, credential_in_sync_key
from .domain.coordinator import LockUsercodeUpdateCoordinator
from .domain.credentials import CredentialAddress, managed_addresses
from .domain.models import LockCodeManagerConfigEntry
from .domain.queries import subentry_id_for_slot
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
        addresses = managed_addresses(slot_num)
        # The aggregate over every managed credential, then one sensor per
        # credential. With a single credential type the two read the same,
        # so the per-credential sensors ship disabled; enabling one splits
        # the view for whoever wants it. Not updated before add: the sensors
        # read the coordinator's fold, and a refresh requested by a sensor
        # about to be discarded as disabled would cost the lock a read.
        sensors: list[tuple[str, tuple[CredentialAddress, ...], bool]] = [
            (ATTR_IN_SYNC, addresses, True),
            *(
                (
                    credential_in_sync_key(address.credential_type.value),
                    (address,),
                    False,
                )
                for address in addresses
            ),
        ]
        async_add_entities(
            [
                LockCodeManagerCodeSlotInSyncEntity(
                    hass,
                    ent_reg,
                    config_entry,
                    coordinator,
                    lock,
                    slot_num,
                    key,
                    covered,
                    enabled_default=enabled_default,
                )
                for key, covered, enabled_default in sensors
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
    In-sync binary sensor over some credentials of the user on this lock.

    A read-only view over the slot coordinator's fold of those credentials:
    on when all are in sync, reporting the worst of their statuses. The
    aggregate ``in_sync`` sensor covers every managed credential; a
    per-credential sensor covers one. The coordinator owns the managers and
    notifies its subscribers when one changes; this entity neither starts
    nor stops them, so disabling it never stops a sync.
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
        key: str,
        addresses: Iterable[CredentialAddress],
        *,
        enabled_default: bool = True,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, key
        )
        CoordinatorEntity.__init__(self, coordinator)
        self._addresses = tuple(addresses)
        self._attr_entity_registry_enabled_default = enabled_default

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and all(
            self.coordinator.has_credential(address) for address in self._addresses
        )

    @property
    def _sync_state(self) -> tuple[bool | None, str | None]:
        """The coordinator's fold for this lock, or unknown without a coordinator."""
        if self._slot_coordinator is None:
            return None, None
        return self._slot_coordinator.sync_state_for(
            self.lock.lock.entity_id, self._addresses
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether every credential this sensor covers is in sync."""
        return self._sync_state[0]

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return extra state attributes."""
        status = self._sync_state[1]
        return {} if status is None else {ATTR_SYNC_STATUS: status}

    def _register_slot_coordinator_subscription(self) -> None:
        """Write state when a manager of this lock changes; config writes are not this sensor's."""
        assert self._slot_coordinator is not None
        self.async_on_remove(
            self._slot_coordinator.register_sync_subscriber(
                self.lock.lock.entity_id, self.async_write_ha_state
            )
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        await CoordinatorEntity.async_added_to_hass(self)
