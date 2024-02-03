"""Base entity class for Lock Code Manager."""

from __future__ import annotations

import copy
import functools
import logging
from typing import Any, KeysView, final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.event import async_track_state_change

from . import ATTR_ENTITIES_ADDED_TRACKER, ATTR_ENTITIES_REMOVED_TRACKER
from .const import ATTR_CODE_SLOT, CONF_SLOTS, DOMAIN
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class BaseLockCodeManagerEntity(Entity):
    """Base Lock Code Manager Entity."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, config_entry: ConfigEntry, locks: list[BaseLock], slot_num: int, key: str
    ) -> None:
        """Initialize base entity."""
        self.config_entry = config_entry
        self.entry_id = self.base_unique_id = config_entry.entry_id
        self.locks = locks
        self.slot_num = slot_num
        self.key = key
        self.ent_reg: er.EntityRegistry | None = None

        self._uid_cache: dict[str, str] = {}
        self._entity_id_map: dict[str, str] = {}
        self._unsub_initial_state: CALLBACK_TYPE | None = None

        key_parts = key.lower().split("_")
        try:
            key_parts[key_parts.index("pin")] = "PIN"
        except ValueError:
            pass

        self._attr_name = f"Code slot {slot_num} {' '.join(key_parts)}"
        self._attr_unique_id = f"{self.base_unique_id}|{slot_num}|{key}"
        self._attr_extra_state_attributes = {ATTR_CODE_SLOT: int(slot_num)}

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
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=data, options={}
        )
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
        self.ent_reg.async_remove(self.entity_id)

        # Figure out whether we were waiting for ourself to be removed before
        # reporting it.
        tracker_dict: dict[int, dict[str, str]] = self.hass.data[DOMAIN][
            self.config_entry.entry_id
        ][ATTR_ENTITIES_REMOVED_TRACKER]
        slot_dict: dict[str, str] = tracker_dict[self.slot_num]
        if self.key not in slot_dict:
            return
        slot_dict[self.key] = False
        if not any(slot_dict.values()):
            tracker_dict.pop(slot_dict)
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
        lock = next(
            lock for lock in self.locks if lock.lock.entity_id == lock_entity_id
        )
        self.locks.remove(lock)

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
        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_{self.slot_num}_{self.key}",
                self._internal_async_remove,
            )
        )
        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_{self.slot_num}",
                self._internal_async_remove,
            )
        )
        entry.async_on_unload(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{entry.entry_id}_remove_lock",
                self._handle_remove_lock,
            )
        )
        entry.async_on_unload(
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

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if self._unsub_initial_state:
            self._unsub_initial_state()
            self._unsub_initial_state = None

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await Entity.async_added_to_hass(self)
        if not self.ent_reg:
            self.ent_reg = er.async_get(self.hass)

        _LOGGER.debug(
            "%s (%s): Adding entity %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
        )

        # Figure out whether we were waiting for ourself to be added before
        # reporting it.
        tracker_dict: dict[int, dict[str, str]] = self.hass.data[DOMAIN][
            self.config_entry.entry_id
        ][ATTR_ENTITIES_ADDED_TRACKER]
        slot_dict: dict[str, str] = tracker_dict[self.slot_num]
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
            if not self.hass.states.get(self.entity_id):
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
