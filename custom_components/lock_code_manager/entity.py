"""Base entity class for Lock Code Manager."""

import logging
from typing import TYPE_CHECKING, Any, final, override

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.event import TrackStates, async_track_state_change_filtered

from .const import (
    ATTR_CODE_SLOT,
    ATTR_SLOT_FIELD,
    DOMAIN,
)
from .ha.entry import LockCodeManagerConfigEntry
from .ha.runtime import lock_registry, signal_lock_changed
from .ha.slot_coordinator import SlotEntityCoordinator
from .model.config import build_slot_device_identifier, build_slot_unique_id
from .model.credentials import CredentialAddress, CredentialType, pin_address
from .model.names import fallback_name
from .model.queries import get_entry_config
from .providers import BaseLock
from .sync.slot import Slot, Status

if TYPE_CHECKING:
    from homeassistant.helpers.event import _TrackStateChangeFiltered

    from .ha.adapter import PortWorker

_LOGGER = logging.getLogger(__name__)

# Statuses furthest from in sync first: an aggregate shows the worst of its
# credentials'.
_BY_DISTANCE = (Status.SUSPENDED, Status.OUT_OF_SYNC, Status.UNKNOWN)


def build_slot_device_info(config_entry: ConfigEntry, slot_num: int) -> DeviceInfo:
    """
    Describe the device that carries every entity for one user.

    The name is looked up here rather than passed in, so no caller can name a
    device something the configuration disagrees with. A slot with no user is
    named as the migration would name it: this is reachable while entities are
    being moved off a foreign device before their slot is swept away.

    Prefixed with the entry's title, because the entity IDs Home Assistant
    derives from this name have to be unique across the whole installation
    and a name is only unique within its entry. Two entries each holding a
    "Raman" would otherwise collide into an arbitrary "_2".
    """
    name = get_entry_config(config_entry).name_for(slot_num) or fallback_name(slot_num)
    return DeviceInfo(
        identifiers={
            (DOMAIN, build_slot_device_identifier(config_entry.entry_id, slot_num))
        },
        name=f"{config_entry.title} {name}",
        manufacturer="Lock Code Manager",
        model="User",
    )


class BaseLockCodeManagerEntity(Entity):
    """Base Lock Code Manager Entity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: LockCodeManagerConfigEntry,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize base entity."""
        self._hass = hass
        self.config_entry = config_entry
        self.entry_id = self.base_unique_id = config_entry.entry_id
        # The entry's locks, taken when the entity is added (see
        # ``_register_callbacks``), then kept by the lock added/removed handlers.
        self.locks: list[BaseLock] = []
        self.slot_num = slot_num
        self.key = key
        self.ent_reg = ent_reg

        self._attr_translation_key = key

        self._attr_device_info = build_slot_device_info(config_entry, slot_num)

        self._attr_unique_id = build_slot_unique_id(self.base_unique_id, slot_num, key)
        self._attr_extra_state_attributes: dict[str, int | str | list[str]] = {
            ATTR_CODE_SLOT: int(slot_num),
            ATTR_SLOT_FIELD: key,
        }

        # Resolved in ``async_added_to_hass``; the coordinator instance is
        # stable across the entity's lifetime because slot removal tears
        # down the entity, and re-adding the slot creates a new entity
        # bound to the new coordinator. See ``SlotEntityCoordinator``.
        self._slot_coordinator: SlotEntityCoordinator | None = None

        # Availability is driven by a state-change subscription scoped to just
        # the lock entities (re-scoped when locks are added/removed), rather
        # than every state in the instance. Created in ``async_added_to_hass``;
        # ``None`` until then.
        self._available_state_tracker: _TrackStateChangeFiltered | None = None

    @final
    @property
    def _state(self) -> Any:
        """Return state of entity."""
        return get_entry_config(self.config_entry).slot(self.slot_num).get(self.key)

    async def _internal_async_remove(self) -> None:
        """
        Handle entity removal.

        Should not be overwritten by platforms.
        """
        _LOGGER.debug(
            "%s (%s): Removing entity %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
        )
        await self._async_remove()
        await self.async_remove(force_remove=True)
        if self.ent_reg.async_get(self.entity_id):
            self.ent_reg.async_remove(self.entity_id)

    async def _async_remove(self) -> None:
        """
        Handle entity removal.

        Can be overwritten by platforms.
        """
        pass

    @callback
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """
        Handle lock entity is being removed.

        Can be overwritten by platforms.
        """
        self.locks = [
            lock for lock in self.locks if lock.lock.entity_id != lock_entity_id
        ]
        self._async_handle_locks_updated()

    @callback
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """
        Handle lock entities are being added.

        Can be overwritten by platforms.
        """
        self.locks.extend(locks)
        self._async_handle_locks_updated()

    @callback
    def _async_handle_locks_updated(self) -> None:
        """
        Re-scope the availability subscription after the lock set changes.

        ``self.locks`` is mutated at runtime by ``_handle_add_locks`` and
        ``_handle_remove_lock``. The availability subscription tracks only the
        lock entities (not every state change in the instance), so it is
        re-scoped to the new set here. Availability is recomputed immediately
        because the added/removed lock's own state change will not otherwise
        trigger an update.
        """
        if (tracker := self._available_state_tracker) is None:
            return
        tracker.async_update_listeners(
            TrackStates(False, {lock.lock.entity_id for lock in self.locks}, set())
        )
        self._handle_available_state_update()

    def _get_removal_uid(self) -> str:
        """
        Get unique ID for removal callback registration.

        Override in subclasses for different UID formats.
        """
        return f"{self.slot_num}|{self.key}"

    @callback
    def _register_callbacks(self) -> None:
        """
        Register entity with callback registry.

        Can be overwritten by platforms if necessary.
        """
        callbacks = self.config_entry.runtime_data.callbacks

        # Register for removal by slot/key pattern
        self.async_on_remove(
            callbacks.register_entity_remover(
                self._get_removal_uid(), self._internal_async_remove
            )
        )

        # Register for lock lifecycle events
        self.async_on_remove(
            callbacks.register_lock_removed_handler(self._handle_remove_lock)
        )
        # Taken here, with the handler that keeps it current: a lock held
        # earlier, even since this entity was made, announced itself before.
        self.locks = list(self.config_entry.runtime_data.locks.values())
        self.async_on_remove(
            callbacks.register_lock_added_handler(self._handle_add_locks)
        )

    @callback
    def _is_available(self) -> bool:
        """Return whether entity should be available."""
        if not self.locks:
            # Nothing to follow is not the same as nothing being up. An entry
            # with no locks answers for its credentials out of its own
            # configuration, which is present whenever the entry is loaded.
            return True
        return any(
            state.state != STATE_UNAVAILABLE
            for lock in self.locks
            if (state := self.hass.states.get(lock.lock.entity_id))
        )

    @callback
    def _handle_available_state_update(
        self, event: Event[EventStateChangedData] | None = None
    ) -> None:
        """Update binary sensor state by getting dependent states."""
        entity_id: str | None = None
        from_state: State | None = None
        to_state: State | None = None
        if event:
            entity_id = event.data["entity_id"]
            from_state = event.data["old_state"]
            to_state = event.data["new_state"]

        if entity_id is not None and entity_id not in (
            lock.lock.entity_id for lock in self.locks
        ):
            return

        if (from_state and from_state.state != STATE_UNAVAILABLE) and (
            to_state and to_state.state != STATE_UNAVAILABLE
        ):
            return

        if (new_available := self._is_available()) != self._attr_available:
            self._attr_available: bool = new_available
            self.async_write_ha_state()

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await Entity.async_added_to_hass(self)

        self._slot_coordinator = self.config_entry.runtime_data.slot_coordinators.get(
            self.slot_num
        )
        if self._slot_coordinator is not None:
            self._register_slot_coordinator_subscription()
        else:
            _LOGGER.warning(
                "%s (%s): No slot coordinator for slot %s when adding entity %s",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.slot_num,
                self.key,
            )

        self._register_callbacks()
        self._available_state_tracker = async_track_state_change_filtered(
            self.hass,
            TrackStates(False, {lock.lock.entity_id for lock in self.locks}, set()),
            self._handle_available_state_update,
        )
        self.async_on_remove(self._available_state_tracker.async_remove)
        self._handle_available_state_update()

        _LOGGER.debug(
            "%s (%s): Adding entity %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
        )

    def _require_slot_coordinator(self) -> SlotEntityCoordinator:
        """
        Return the cached slot coordinator, raising ``HomeAssistantError`` if absent.

        Used by intent handlers (text ``async_set_value``, switch
        ``async_turn_on``/``async_turn_off``) so a missing coordinator
        surfaces as a failed service call rather than a silent no-op.
        """
        if self._slot_coordinator is None:
            raise HomeAssistantError(f"No slot coordinator for slot {self.slot_num}")
        return self._slot_coordinator

    def _register_slot_coordinator_subscription(self) -> None:
        """
        Register the entity's subscription with the slot coordinator.

        Default: write Home Assistant state after the coordinator writes
        slot config fields (the shape text and switch want). Subclasses
        that need a different subscription shape (active view, sync
        manager) override. The base only calls this hook when
        ``_slot_coordinator`` is not None; subclasses can rely on that
        precondition.
        """
        # Type narrowing for mypy; guaranteed non-None by the caller.
        assert self._slot_coordinator is not None
        self.async_on_remove(
            self._slot_coordinator.register_state_subscriber(self.async_write_ha_state)
        )


class BaseLockCodeManagerCodeSlotPerLockEntity(BaseLockCodeManagerEntity):
    """Base LockCode Manager Code Slot Entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: ConfigEntry,
        lock: BaseLock,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, key
        )
        self.lock = lock
        # Deliberately keeps the slot device the base assigned rather than
        # attaching to the lock's own device. Since HA 2026.8 a device belongs
        # to exactly one config entry, so anything Lock Code Manager puts on
        # another integration's device either forks a duplicate of it or, if
        # attached without claiming it, leaves this entity somewhere its own
        # integration cannot show it.
        #
        # The lock therefore has to be named by the ENTITY. Sitting on the
        # slot device, one per lock, the slot number alone would name every
        # one of them the same thing.
        self._attr_translation_placeholders = {"lock_name": lock.display_name}

        self._attr_unique_id = build_slot_unique_id(
            self.base_unique_id, slot_num, self.key, lock.lock.entity_id
        )

    @override
    def _get_removal_uid(self) -> str:
        """Get unique ID for removal callback registration (per-lock variant)."""
        return f"{self.slot_num}|{self.key}|{self.lock.lock.entity_id}"

    @callback
    @override
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """Handle lock entity is being removed."""
        super()._handle_remove_lock(lock_entity_id)
        if self.lock.lock.entity_id != lock_entity_id:
            return
        self.config_entry.async_create_task(self.hass, self._internal_async_remove())

    @callback
    @override
    def _is_available(self) -> bool:
        """Return whether entity is available."""
        return (
            state := self.hass.states.get(self.lock.lock.entity_id)
        ) and state.state != STATE_UNAVAILABLE

    @override
    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)


class LockWorkerView:
    """
    A per-lock slot entity reading what the lock's worker knows of the slot.

    It writes its state whenever the worker reports a change on the lock.
    """

    hass: HomeAssistant
    lock: BaseLock
    slot_num: int

    config_entry: ConfigEntry

    # The credential type this entity views; None for every type the slot
    # holds on the lock.
    _credential_type: CredentialType | None = None

    @property
    def _pin(self) -> CredentialAddress:
        """Return the address of this slot's PIN on the lock."""
        return pin_address(int(self.slot_num))

    def _core_worker(self) -> PortWorker | None:
        """
        Return the lock's worker if it syncs this slot for this entry.

        A slot another entry already manages on the lock is that entry's:
        its state is not this entry's user's (their code is not there).
        """
        registry = lock_registry(self.hass)
        lock_entity_id = self.lock.lock.entity_id
        if registry.owner(lock_entity_id, int(self.slot_num)) not in (
            None,
            self.config_entry.entry_id,
        ):
            return None
        return registry.worker(lock_entity_id)

    def _addresses(self, worker: PortWorker) -> list[CredentialAddress]:
        """Return the addresses this entity views that the worker tracks."""
        slot_num = int(self.slot_num)
        return [
            address
            for address in worker.slots
            if address.user_ref == slot_num
            and self._credential_type in (None, address.credential_type)
        ]

    def _core_slot(self) -> Slot | None:
        """Return the worker's view of this slot's PIN, if the lock's worker has it."""
        if (worker := self._core_worker()) is None:
            return None
        return worker.slots.get(self._pin)

    def _core_status(self) -> Status | None:
        """
        Return the sync status of what this entity views, or None if untracked.

        Of several credentials, the one furthest from in sync: held, then
        out of sync, then being checked.
        """
        worker = self._core_worker()
        if worker is None or not (addresses := self._addresses(worker)):
            return None
        statuses = {worker.status(address)[0] for address in addresses}
        return next(
            (status for status in _BY_DISTANCE if status in statuses), Status.IN_SYNC
        )

    @property
    def available(self) -> bool:
        """
        Return whether the lock is up and has said something about the slot.

        Before the lock first answers, a slot never read has nothing to show.
        After that, a slot without a reading is being checked: its reading
        was set aside to read the slot again (``Slot.doubt``), so it is not
        gone, and the entity says unknown, not unavailable, until the read.
        """
        assert isinstance(self, BaseLockCodeManagerCodeSlotPerLockEntity)
        worker = self._core_worker()
        if not self._is_available() or worker is None:
            return False
        if not (addresses := self._addresses(worker)):
            return False
        return worker.answered or all(
            worker.slots[address].seen is not None for address in addresses
        )

    def _core_syncing(self) -> bool:
        """Return whether a sync of anything this entity views is queued or running."""
        worker = self._core_worker()
        return worker is not None and any(
            worker.syncing(address) for address in self._addresses(worker)
        )

    @callback
    def _watch_lock(self) -> None:
        """Write state on every change the lock's worker reports."""
        assert isinstance(self, Entity)
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_lock_changed(self.lock.lock.entity_id),
                self.async_write_ha_state,
            )
        )
