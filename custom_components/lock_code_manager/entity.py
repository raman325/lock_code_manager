"""Base entity class for Lock Code Manager."""

from __future__ import annotations

import copy
import functools
import logging
from typing import Any, KeysView, final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    MATCH_ALL,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    STATE_UNLOCKED,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.event import async_track_state_change

from .const import (
    ATTR_CODE_SLOT,
    ATTR_ENTITIES_ADDED_TRACKER,
    ATTR_ENTITIES_REMOVED_TRACKER,
    ATTR_TO,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from .data import get_slot_data
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class BaseLockCodeManagerEntity(Entity):
    """Base Lock Code Manager Entity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: ConfigEntry,
        slot_num: int,
        key: str,
    ) -> None:
        """Initialize base entity."""
        self._hass = hass
        self.config_entry = config_entry
        self.entry_id = self.base_unique_id = config_entry.entry_id
        self.locks: list[BaseLock] = list(
            hass.data[DOMAIN][config_entry.entry_id][CONF_LOCKS].values()
        )
        self.slot_num = slot_num
        self.key = key
        self.ent_reg = ent_reg

        self._uid_cache: dict[str, str] = {}
        self._entity_id_map: dict[str, str] = {}
        self._unsub_initial_state: CALLBACK_TYPE | None = None

        key_parts = key.lower().split("_")
        try:
            key_parts[key_parts.index("pin")] = "PIN"
        except ValueError:
            pass

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry_id}|{slot_num}")},
            name=f"{config_entry.title} code slot {slot_num}",
            manufacturer="Lock Code Manager",
            model="Code Slot",
            via_device=(DOMAIN, self.entry_id),
        )

        self._attr_name: str | None = " ".join(key_parts)
        self._attr_unique_id = f"{self.base_unique_id}|{slot_num}|{key}"
        self._attr_extra_state_attributes = {ATTR_CODE_SLOT: int(slot_num)}

    @final
    @property
    def _state(self) -> Any:
        """Return state of entity."""
        return get_slot_data(self.config_entry, self.slot_num).get(self.key)

    @final
    @property
    def _calendar_entity_id(self) -> str | None:
        """Return calendar entity ID for this slot."""
        return get_slot_data(self.config_entry, self.slot_num).get(CONF_CALENDAR)

    @final
    def _get_uid(self, key: str) -> str:
        """Get and cache unique id for a given key."""
        if key not in self._uid_cache:
            self._uid_cache[key] = f"{self.base_unique_id}|{self.slot_num}|{key}"
        return self._uid_cache[key]

    @callback
    @final
    def _update_config_entry(self, value: Any) -> None:
        """Update config entry data."""
        _LOGGER.debug(
            "%s (%s): Updating %s to %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.key,
            value,
        )
        data = copy.deepcopy(dict(self.config_entry.data))
        data[CONF_SLOTS][self.slot_num][self.key] = value
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        self.async_write_ha_state()

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

        # Figure out whether we were waiting for ourself to be removed before
        # reporting it.
        tracker_dict: dict[int, dict[str, bool]] = self.hass.data[DOMAIN][
            self.config_entry.entry_id
        ][ATTR_ENTITIES_REMOVED_TRACKER]
        slot_dict: dict[str, bool] = tracker_dict[self.slot_num]
        if self.key not in slot_dict:
            return
        slot_dict[self.key] = False
        if not any(slot_dict.values()):
            tracker_dict.pop(self.slot_num)
            async_dispatcher_send(
                self.hass,
                f"{DOMAIN}_{self.config_entry.entry_id}_remove_tracking_{self.slot_num}",
                slot_dict.keys(),
            )

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

    @callback
    def _handle_add_locks(self, locks: list[BaseLock]) -> None:
        """
        Handle lock entities are being added.

        Can be overwritten by platforms.
        """
        self.locks.extend(locks)

    @callback
    def dispatcher_connect(self) -> None:
        """
        Connect entity to dispatcher signals.

        Can be overwritten by platforms if necessary
        """
        entry = self.config_entry
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_{self.slot_num}_{self.key}",
                self._internal_async_remove,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_{self.slot_num}",
                self._internal_async_remove,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_lock",
                self._handle_remove_lock,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_add_locks",
                self._handle_add_locks,
            )
        )

    @callback
    def _listen_for_initial_state(
        self, keys: KeysView, _: str, __: State, ___: State
    ) -> None:
        """Handle state change."""
        async_dispatcher_send(
            self.hass,
            f"{DOMAIN}_{self.entry_id}_add_tracking_{self.slot_num}",
            keys,
        )
        if self._unsub_initial_state:
            self._unsub_initial_state()
            self._unsub_initial_state = None

    @callback
    def _event_filter(self, event: Event) -> bool:
        """Filter events."""
        return (
            any(
                event.data[ATTR_ENTITY_ID] == lock.lock.entity_id for lock in self.locks
            )
            and event.data[ATTR_CODE_SLOT] == int(self.slot_num)
            and event.data[ATTR_TO] == STATE_UNLOCKED
        )

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self._unsub_initial_state:
            self._unsub_initial_state()
            self._unsub_initial_state = None

    @callback
    def _is_available(self) -> bool:
        """Return whether entity should be available."""
        return any(
            state.state != STATE_UNAVAILABLE
            for lock in self.locks
            if (state := self.hass.states.get(lock.lock.entity_id))
        )

    @callback
    def _handle_available_state_update(
        self,
        entity_id: str | None = None,
        _: State | None = None,
        __: State | None = None,
    ) -> None:
        """Handle availability state update."""
        if (
            entity_id is not None
            and entity_id not in (lock.lock.entity_id for lock in self.locks)
        ):
            return
        if (new_available := self._is_available()) != self._attr_available:
            self._attr_available: bool = new_available
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await Entity.async_added_to_hass(self)

        self.dispatcher_connect()
        self.async_on_remove(
            async_track_state_change(
                self.hass,
                MATCH_ALL,
                self._handle_available_state_update,
                to_state=STATE_UNAVAILABLE,
            )
        )
        self.async_on_remove(
            async_track_state_change(
                self.hass,
                MATCH_ALL,
                self._handle_available_state_update,
                from_state=STATE_UNAVAILABLE,
            )
        )
        self._handle_available_state_update()

        _LOGGER.debug(
            "%s (%s): Adding entity %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
        )

        # Figure out whether we were waiting for ourself to be added before
        # reporting it.
        tracker_dict: dict[int, dict[str, bool]] = self.hass.data[DOMAIN][
            self.config_entry.entry_id
        ][ATTR_ENTITIES_ADDED_TRACKER]
        slot_dict: dict[str, bool] = tracker_dict[self.slot_num]
        if self.key not in slot_dict:
            return
        slot_dict[self.key] = False
        # Once my entity is the last entity that's being tracked and is loaded
        # send a signal so the binary sensor can do its thing.
        if not any(slot_dict.values()):
            tracker_dict.pop(self.slot_num)
            _LOGGER.debug(
                "%s (%s): Sending signal from %s to binary sensor that slot %s is ready",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.entity_id,
                self.slot_num,
            )
            if (
                not (state := self.hass.states.get(self.entity_id))
                or state.state == STATE_UNKNOWN
                or (state.state == STATE_UNAVAILABLE and self._is_available)
                or self._state is None
            ):
                self._unsub_initial_state = async_track_state_change(
                    self.hass,
                    [self.entity_id],
                    functools.partial(self._listen_for_initial_state, slot_dict),
                )
            else:
                async_dispatcher_send(
                    self.hass,
                    f"{DOMAIN}_{self.entry_id}_add_tracking_{self.slot_num}",
                    slot_dict.keys(),
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
        if lock.device_entry:
            self._attr_device_info = DeviceInfo(
                connections=lock.device_entry.connections,
                identifiers=lock.device_entry.identifiers,
            )
            self._attr_name = f"Code slot {slot_num}"

        self._attr_unique_id = (
            f"{self.base_unique_id}|{slot_num}|{self.key}|{lock.lock.entity_id}"
        )

    @callback
    def _handle_remove_lock(self, lock_entity_id: str) -> None:
        """Handle lock entity is being removed."""
        super()._handle_remove_lock(lock_entity_id)
        if self.lock.lock.entity_id != lock_entity_id:
            return
        self._hass.async_create_task(self._internal_async_remove())

    @callback
    def _is_available(self) -> bool:
        """Return whether entity is available."""
        return (
            state := self.hass.states.get(self.lock.lock.entity_id)
        ) and state.state != STATE_UNAVAILABLE

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
