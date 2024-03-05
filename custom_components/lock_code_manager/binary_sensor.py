"""Sensor for lock_code_manager."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Callable

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    CONF_PIN,
    MATCH_ALL,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change

from .const import (
    ATTR_CODE,
    ATTR_PIN_SYNCED_TO_LOCKS,
    CONF_CALENDAR,
    CONF_NUMBER_OF_USES,
    COORDINATORS,
    DOMAIN,
    EVENT_PIN_USED,
    PLATFORM_MAP,
)
from .coordinator import LockUsercodeUpdateCoordinator
from .entity import BaseLockCodeManagerEntity
from .exceptions import EntityNotFoundError
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_pin_enabled_entity(slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Add PIN enabled binary sensor entities for slot."""
        async_add_entities(
            [LockCodeManagerPINSyncedEntity(hass, ent_reg, config_entry, slot_num)],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_{config_entry.entry_id}_add", add_pin_enabled_entity
        )
    )
    return True


class LockCodeManagerPINSyncedEntity(BaseLockCodeManagerEntity, BinarySensorEntity):
    """PIN synced to locks binary sensor entity for lock code manager."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        ent_reg: er.EntityRegistry,
        config_entry: ConfigEntry,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, ent_reg, config_entry, slot_num, ATTR_PIN_SYNCED_TO_LOCKS
        )
        self._entity_id_map: dict[str, str] = {}
        self._issue_reg: ir.IssueRegistry | None = None
        self._call_later_unsub: Callable | None = None

    def _lock_slot_sensor_state(self, lock: BaseLock) -> str:
        """Return lock slot sensor entity ID."""
        if not (
            entity_id := self.ent_reg.async_get_entity_id(
                SENSOR_DOMAIN,
                DOMAIN,
                f"{self.base_unique_id}|{self.slot_num}|{ATTR_CODE}|{lock.lock.entity_id}",
            )
        ) or not (state := self.hass.states.get(entity_id)):
            raise EntityNotFoundError(lock, self.slot_num, ATTR_CODE)
        return state.state

    async def async_update_usercodes(
        self, states: dict[str, dict[str, str]] | None = None
    ) -> None:
        """Update usercodes on locks based on state change."""
        if not states:
            states = {}
        coordinators: list[LockUsercodeUpdateCoordinator] = []
        for lock in self.locks:
            lock_slot_sensor_state = self._lock_slot_sensor_state(lock)
            if self.is_on:
                name_entity_id = self._entity_id_map[CONF_NAME]
                name_state = self.hass.states.get(name_entity_id)

                pin_entity_id = self._entity_id_map[CONF_PIN]
                pin_state = self.hass.states.get(pin_entity_id)

                for entity_id, state in (
                    (name_entity_id, name_state),
                    (pin_entity_id, pin_state),
                ):
                    if not state:
                        raise ValueError(f"State not found for {entity_id}")

                if lock_slot_sensor_state == pin_state.state:
                    continue

                await lock.async_set_usercode(
                    int(self.slot_num), pin_state.state, name_state.state
                )

                _LOGGER.info(
                    "%s (%s): Set usercode for %s slot %s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    lock.lock.entity_id,
                    self.slot_num,
                )
            else:
                if lock_slot_sensor_state in (
                    "",
                    STATE_UNKNOWN,
                ):
                    continue

                if not (
                    disabling_entity_ids := (
                        state["entity_id"]
                        for key, state in states.items()
                        if (
                            key not in (CONF_NUMBER_OF_USES, CONF_PIN)
                            and state["state"] != STATE_ON
                        )
                        or (
                            key == CONF_NUMBER_OF_USES
                            and (
                                state["state"] in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                                or int(float(state["state"])) == 0
                            )
                        )
                        or (
                            key == CONF_PIN
                            and state["state"] != self._lock_slot_sensor_state(lock)
                        )
                    )
                ):
                    return

                await lock.async_clear_usercode(int(self.slot_num))

                _LOGGER.info(
                    (
                        "%s (%s): Cleared usercode for lock %s slot %s because the "
                        "following entities indicate the slot is disabled: %s"
                    ),
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    lock.lock.entity_id,
                    self.slot_num,
                    ", ".join(disabling_entity_ids),
                )

            coordinators.append(
                self.hass.data[DOMAIN][COORDINATORS][lock.lock.entity_id]
            )

        await asyncio.gather(
            *[coordinator.async_refresh() for coordinator in coordinators]
        )

    async def _update_state(self, _: datetime | None = None) -> None:
        """Update binary sensor state by getting dependent states."""
        if self._call_later_unsub:
            self._call_later_unsub()
            self._call_later_unsub = None

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
            if key in (EVENT_PIN_USED, CONF_NAME):
                continue
            issue_id = f"{self.config_entry.entry_id}_{self.slot_num}_no_{key}"
            if not (state := self.hass.states.get(entity_id)):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    translation_key="no_state",
                    translation_placeholders={
                        "entity_id": entity_id,
                        "entry_title": self.config_entry.title,
                        "key": key,
                    },
                    severity=ir.IssueSeverity.ERROR,
                )
                continue
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            states[key] = {"entity_id": entity_id, "state": state.state}

        # For the binary sensor to be on, all states must be 'on', or for the number
        # of uses, greater than 0
        self._attr_is_on = bool(
            states
            and all(
                (
                    key not in (CONF_NUMBER_OF_USES, CONF_PIN)
                    and state["state"] == STATE_ON
                )
                or (
                    key == CONF_NUMBER_OF_USES
                    and state["state"] not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                    and int(float(state["state"])) > 0
                )
                for key, state in states.items()
                if key != CONF_PIN
            )
        )

        # If the state of the binary sensor has changed, or the binary sensor is on and
        # the desired PIN state has changed, update the usercodes
        if (
            not (state := self.hass.states.get(self.entity_id))
            or state.state != self.state
            or (
                self.is_on
                and any(
                    self._lock_slot_sensor_state(lock) != states[CONF_PIN]["state"]
                    for lock in self.locks
                )
            )
        ):
            try:
                await self.async_update_usercodes(states)
            except EntityNotFoundError:
                self._call_later_unsub = async_call_later(
                    self.hass, timedelta(seconds=2), self._update_state
                )
                self.async_on_remove(self._call_later_unsub)
                return
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

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await asyncio.sleep(0)
        if self._call_later_unsub:
            self._call_later_unsub()
            self._call_later_unsub = None

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BinarySensorEntity.async_added_to_hass(self)
        await BaseLockCodeManagerEntity.async_added_to_hass(self)

        if not self._issue_reg:
            self._issue_reg = ir.async_get(self.hass)

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
            async_dispatcher_connect(
                self.hass,
                f"{DOMAIN}_{self.entry_id}_update_usercode_{self.slot_num}",
                self.async_update_usercodes,
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
