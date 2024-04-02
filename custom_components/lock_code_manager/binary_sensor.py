"""Binary sensor for lock_code_manager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorEntity,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    CONF_PIN,
    MATCH_ALL,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    ATTR_IN_SYNC,
    CONF_CALENDAR,
    CONF_NUMBER_OF_USES,
    COORDINATORS,
    DOMAIN,
    EVENT_PIN_USED,
    PLATFORM_MAP,
)
from .coordinator import LockUsercodeUpdateCoordinator
from .entity import BaseLockCodeManagerCodeSlotPerLockEntity, BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_pin_active_entity(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add active binary sensor entities for slot."""
        async_add_entities(
            [LockCodeManagerActiveEntity(hass, ent_reg, config_entry, slot_num)],
            True,
        )

    @callback
    def add_code_slot_entities(
        lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ):
        """Add code slot sensor entities for slot."""
        coordinator: LockUsercodeUpdateCoordinator = hass.data[DOMAIN][
            config_entry.entry_id
        ][COORDINATORS][lock.lock.entity_id]
        async_add_entities(
            [
                LockCodeManagerCodeSlotInSyncEntity(
                    hass, ent_reg, config_entry, coordinator, lock, slot_num
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_{config_entry.entry_id}_add", add_pin_active_entity
        )
    )
    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add_lock_slot",
            add_code_slot_entities,
        )
    )
    return True


class LockCodeManagerActiveEntity(BaseLockCodeManagerEntity, BinarySensorEntity):
    """Active binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: ConfigEntry,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, ATTR_ACTIVE
        )
        self._entity_id_map: dict[str, str] = {}

    async def _update_state(self, _: datetime | None = None) -> None:
        """Update binary sensor state by getting dependent states."""
        _LOGGER.debug(
            "%s (%s): Updating %s",
            self.config_entry.entry_id,
            self.config_entry.title,
            self.entity_id,
        )
        # Switch binary sensor on if at least one state exists and all states are 'on'
        entity_id_map = self._entity_id_map.copy()
        # If there is a calendar entity, we need to check its state as well
        if self._calendar_entity_id:
            entity_id_map[CONF_CALENDAR] = self._calendar_entity_id

        states: dict[str, dict[str, str]] = {}
        for key, entity_id in entity_id_map.items():
            if key in (EVENT_PIN_USED, CONF_NAME, CONF_PIN, ATTR_IN_SYNC):
                continue
            if not (state := self.hass.states.get(entity_id)):
                return
            states[key] = {"entity_id": entity_id, "state": state.state}

        # For the binary sensor to be on, all states must be 'on', or for the number
        # of uses, greater than 0
        inactive_because_of = [
            key
            for key, state in states.items()
            if (key != CONF_NUMBER_OF_USES and state["state"] != STATE_ON)
            or (
                key == CONF_NUMBER_OF_USES
                and (
                    state["state"] in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                    or int(float(state["state"])) == 0
                )
            )
        ]
        self._attr_is_on = bool(states and not inactive_because_of)
        if inactive_because_of:
            self._attr_extra_state_attributes["inactive_because_of"] = (
                inactive_because_of
            )
        else:
            self._attr_extra_state_attributes.pop("inactive_because_of", None)
        self.async_write_ha_state()

    async def _config_entry_update_listener(
        self, hass: HomeAssistant, config_entry: ConfigEntry
    ) -> None:
        """Update listener."""
        if config_entry.options:
            return
        await self._update_state()

    async def _remove_keys_to_track(self, keys: list[str]) -> None:
        """Remove keys to track."""
        for key in keys:
            if key not in PLATFORM_MAP:
                continue
            self._entity_id_map.pop(key, None)
        await self._update_state()

    async def _add_keys_to_track(self, keys: list[str]) -> None:
        """Add keys to track."""
        for key in keys:
            if not (platform := PLATFORM_MAP.get(key)):
                continue
            self._entity_id_map[key] = self.ent_reg.async_get_entity_id(
                platform, DOMAIN, self._get_uid(key)
            )
        await self._update_state()

    async def _handle_calendar_state_changes(
        self, entity_id: str, _: State, __: State
    ) -> None:
        """Handle calendar state changes."""
        if entity_id == self._calendar_entity_id:
            await self._update_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_add_tracking_{self.slot_num}",
                self._add_keys_to_track,
            )
        )

        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_remove_tracking_{self.slot_num}",
                self._remove_keys_to_track,
            )
        )

        self.async_on_remove(
            async_track_state_change(
                self.hass, MATCH_ALL, self._handle_calendar_state_changes
            )
        )

        self.async_on_remove(
            self.config_entry.add_update_listener(self._config_entry_update_listener)
        )


class LockCodeManagerCodeSlotInSyncEntity(
    BaseLockCodeManagerCodeSlotPerLockEntity,
    CoordinatorEntity[LockUsercodeUpdateCoordinator],
    BinarySensorEntity,
):
    """PIN synced binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: ConfigEntry,
        coordinator: LockUsercodeUpdateCoordinator,
        lock: BaseLock,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerCodeSlotPerLockEntity.__init__(
            self, hass, ent_reg, config_entry, lock, slot_num, ATTR_IN_SYNC
        )
        CoordinatorEntity.__init__(self, coordinator)
        self._entity_id_map: dict[str, str] = {}
        self._active_unique_id = self._get_uid(ATTR_ACTIVE)
        self._name_text_unique_id = self._get_uid(CONF_NAME)
        self._pin_text_unique_id = self._get_uid(CONF_PIN)
        lock_entity_id = self.lock.lock.entity_id
        self._lock_slot_sensor_unique_id = (
            f"{self._get_uid(ATTR_CODE)}|{lock_entity_id}"
        )
        self._lock = asyncio.Lock()

    @property
    def should_poll(self) -> bool:
        """Return whether entity should poll."""
        return True

    @property
    def available(self) -> bool:
        """Return whether binary sensor is available or not."""
        return BaseLockCodeManagerCodeSlotPerLockEntity._is_available(self) and (
            int(self.slot_num) in self.coordinator.data
        )

    async def async_update(self) -> None:
        """Update entity."""
        if (
            self._lock
            and not self._lock.locked()
            and self.is_on is False
            and (state := self.hass.states.get(self.lock.lock.entity_id))
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            _LOGGER.error(
                "Updating %s code slot %s because it is out of sync",
                self.lock.lock.entity_id,
                self.slot_num,
            )
            await self._update_state()

    def _get_entity_state(self, key: str) -> str | None:
        """Get entity state."""
        if (state := self.hass.states.get(self._entity_id_map[key])) is None:
            return None
        return state.state

    async def _update_state(
        self,
        entity_id: str | None = None,
        from_state: State | None = None,
        to_state: State | None = None,
    ) -> None:
        """Update binary sensor state by getting dependent states."""
        if entity_id is not None and (
            not (ent_entry := self.ent_reg.async_get(entity_id))
            or ent_entry.platform != DOMAIN
            or (ent_entry.domain, ent_entry.unique_id)
            not in (
                (BINARY_SENSOR_DOMAIN, self._active_unique_id),
                (TEXT_DOMAIN, self._name_text_unique_id),
                (TEXT_DOMAIN, self._pin_text_unique_id),
                (SENSOR_DOMAIN, self._lock_slot_sensor_unique_id),
            )
            or (
                to_state is not None
                and to_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            )
        ):
            return

        async with self._lock:
            for key, domain, unique_id in (
                (CONF_PIN, TEXT_DOMAIN, self._pin_text_unique_id),
                (CONF_NAME, TEXT_DOMAIN, self._name_text_unique_id),
                (ATTR_ACTIVE, BINARY_SENSOR_DOMAIN, self._active_unique_id),
                (ATTR_CODE, SENSOR_DOMAIN, self._lock_slot_sensor_unique_id),
            ):
                if key not in self._entity_id_map:
                    if not (
                        ent_id := self.ent_reg.async_get_entity_id(
                            domain, DOMAIN, unique_id
                        )
                    ):
                        return
                    self._entity_id_map[key] = ent_id

                if self._get_entity_state(key) is None:
                    return

            if self._get_entity_state(ATTR_ACTIVE) == STATE_ON:
                if (
                    pin_state := self._get_entity_state(CONF_PIN)
                ) is not None and pin_state != self._get_entity_state(ATTR_CODE):
                    self._attr_is_on = False
                    self.async_write_ha_state()
                    await self.lock.async_set_usercode(
                        int(self.slot_num), pin_state, self._get_entity_state(CONF_NAME)
                    )
                    _LOGGER.info(
                        "%s (%s): Set usercode for %s slot %s",
                        self.config_entry.entry_id,
                        self.config_entry.title,
                        self.lock.lock.entity_id,
                        self.slot_num,
                    )
                elif self._attr_is_on:
                    return
                else:
                    self._attr_is_on = True
            elif self._get_entity_state(ATTR_ACTIVE) == STATE_OFF:
                if self._get_entity_state(ATTR_CODE) != "":
                    self._attr_is_on = False
                    self.async_write_ha_state()
                    await self.lock.async_clear_usercode(int(self.slot_num))
                    _LOGGER.info(
                        "%s (%s): Cleared usercode for lock %s slot %s",
                        self.config_entry.entry_id,
                        self.config_entry.title,
                        self.lock.lock.entity_id,
                        self.slot_num,
                    )
                elif self._attr_is_on:
                    return
                else:
                    self._attr_is_on = True

            if self._attr_is_on is False:
                await self.coordinator.async_refresh()
            else:
                self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerCodeSlotPerLockEntity.async_added_to_hass(self)
        # await CoordinatorEntity.async_added_to_hass(self)

        self.async_on_remove(
            async_track_state_change(self.hass, MATCH_ALL, self._update_state)
        )
        await self._update_state()
