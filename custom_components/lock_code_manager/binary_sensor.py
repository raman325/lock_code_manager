"""Sensor for lock_code_manager."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, CONF_PIN, STATE_ON
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import MATCH_ALL, async_track_state_change

from .const import (
    ATTR_CODE,
    ATTR_PIN_SYNCED_TO_LOCKS,
    CONF_CALENDAR,
    CONF_NUMBER_OF_USES,
    DOMAIN,
    EVENT_PIN_USED,
    PLATFORM_MAP,
)
from .entity import BaseLockCodeManagerEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Set up config entry."""

    @callback
    def add_pin_enabled_entity(slot_num: int) -> None:
        """Add PIN enabled binary sensor entities for slot."""
        async_add_entities(
            [LockCodeManagerPINSyncedEntity(hass, config_entry, slot_num)],
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
        config_entry: ConfigEntry,
        slot_num: int,
    ) -> None:
        """Initialize entity."""
        BaseLockCodeManagerEntity.__init__(
            self, hass, config_entry, slot_num, ATTR_PIN_SYNCED_TO_LOCKS
        )
        self._entity_id_map: dict[str, str] = {}
        self._update_usercodes_task: asyncio.Task | None = None
        self._issue_reg: ir.IssueRegistry | None = None

    async def async_update_usercodes(self) -> None:
        """Update usercodes on locks based on state change."""
        for lock in self.locks:
            lock_slot_sensor_entity_id = self.ent_reg.async_get_entity_id(
                SENSOR_DOMAIN,
                DOMAIN,
                f"{self.base_unique_id}|{self.slot_num}|{ATTR_CODE}|{lock.lock.entity_id}",
            )
            assert lock_slot_sensor_entity_id

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

                if (
                    state := self.hass.states.get(lock_slot_sensor_entity_id)
                ) and state.state == pin_state.state:
                    continue

                _LOGGER.info(
                    "%s (%s): Setting usercode for %s slot %s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    lock.lock.entity_id,
                    self.slot_num,
                )

                self.config_entry.async_create_task(
                    self.hass,
                    lock.async_set_usercode(
                        int(self.slot_num), pin_state.state, name_state.state
                    ),
                    f"async_set_usercode_{lock.lock.entity_id}_{self.slot_num}",
                )
            else:
                if (
                    state := self.hass.states.get(lock.lock.entity_id)
                ) and state.state == "":
                    continue

                _LOGGER.info(
                    "%s (%s): Clearing usercode for lock %s slot %%s",
                    self.config_entry.entry_id,
                    self.config_entry.title,
                    lock.lock.entity_id,
                    self.slot_num,
                )

                self.config_entry.async_create_task(
                    self.hass,
                    lock.async_clear_usercode(int(self.slot_num)),
                    f"async_clear_usercode_{lock.lock.entity_id}_{self.slot_num}",
                )

    @callback
    def _create_update_usercode_task(self) -> None:
        """Create task to update usercode."""
        if self._update_usercodes_task:
            self._update_usercodes_task.cancel()
        self._update_usercodes_task = self.config_entry.async_create_task(
            self.hass,
            self.async_update_usercodes(),
            f"async_update_usercodes_{self.config_entry.entry_id}_{self.slot_num}",
        )

    @callback
    def _update_state(self) -> None:
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
        if calendar_entity_id := self.config_entry.data.get(CONF_CALENDAR):
            entity_id_map[CONF_CALENDAR] = calendar_entity_id

        states = {}
        for key, entity_id in entity_id_map.items():
            if key in (EVENT_PIN_USED, CONF_NAME, CONF_PIN):
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
            states[key] = state.state

        # For the binary sensor to be on, all states must be 'on', or for the number
        # of uses, greater than 0
        self._attr_is_on = bool(
            states
            and all(
                (key != CONF_NUMBER_OF_USES and state == STATE_ON)
                or (key == CONF_NUMBER_OF_USES and int(state) > 0)
                for key, state in states.items()
            )
        )

        if (
            not (state := self.hass.states.get(self.entity_id))
            or state.state != self.state
        ):
            self._create_update_usercode_task()
            self.async_write_ha_state()

    @callback
    def _remove_keys_to_track(self, keys: list[str]) -> None:
        """Remove keys to track."""
        for key in keys:
            if key not in PLATFORM_MAP:
                continue
            self._entity_id_map.pop(key, None)
        self._update_state()

    @callback
    def _add_keys_to_track(self, keys: list[str]) -> None:
        """Add keys to track."""
        for key in keys:
            if not (platform := PLATFORM_MAP.get(key)):
                continue
            self._entity_id_map[key] = self.ent_reg.async_get_entity_id(
                platform, DOMAIN, self._get_uid(key)
            )
        self._update_state()

    @callback
    def _handle_state_changes(self, entity_id: str, _: State, __: State) -> None:
        """Handle state change."""
        entity_id_map = self._entity_id_map.copy()
        if calendar_entity_id := self.config_entry.data.get(CONF_CALENDAR):
            entity_id_map[CONF_CALENDAR] = calendar_entity_id
        if any(
            entity_id == key_entity_id
            for key, key_entity_id in entity_id_map.items()
            if key not in (EVENT_PIN_USED, CONF_NAME, CONF_PIN)
        ):
            self._update_state()

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
                self._create_update_usercode_task,
            )
        )

        self.async_on_remove(
            async_track_state_change(self.hass, MATCH_ALL, self._handle_state_changes)
        )
