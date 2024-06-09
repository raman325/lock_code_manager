"""Module for Z-Wave JS locks."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable, Iterable

from zwave_js_server.const.command_class.lock import ATTR_CODE_SLOT, ATTR_USERCODE
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import get_usercode_from_node, get_usercodes

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.zwave_js.const import (
    ATTR_EVENT,
    ATTR_EVENT_LABEL,
    ATTR_HOME_ID,
    ATTR_NODE_ID,
    ATTR_PARAMETERS,
    ATTR_TYPE,
    DATA_CLIENT,
    DOMAIN as ZWAVE_JS_DOMAIN,
    SERVICE_CLEAR_LOCK_USERCODE,
    SERVICE_SET_LOCK_USERCODE,
    ZWAVE_JS_NOTIFICATION_EVENT,
)
from homeassistant.components.zwave_js.helpers import async_get_node_from_entity_id
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_PIN,
    STATE_ON,
)
from homeassistant.core import Event, callback

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import get_entry_data
from ..exceptions import LockDisconnected
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)

# All known Access Control Notification CC events that indicate the lock is locked
# or unlocked
ACCESS_CONTROL_NOTIFICATION_TO_LOCKED = {
    True: (
        AccessControlNotificationEvent.AUTO_LOCK_LOCKED_OPERATION,
        AccessControlNotificationEvent.KEYPAD_LOCK_OPERATION,
        AccessControlNotificationEvent.LOCK_OPERATION_WITH_USER_CODE,
        AccessControlNotificationEvent.LOCKED_BY_RF_WITH_INVALID_USER_CODE,
        AccessControlNotificationEvent.MANUAL_LOCK_OPERATION,
        AccessControlNotificationEvent.RF_LOCK_OPERATION,
    ),
    False: (
        AccessControlNotificationEvent.KEYPAD_UNLOCK_OPERATION,
        AccessControlNotificationEvent.MANUAL_UNLOCK_OPERATION,
        AccessControlNotificationEvent.RF_UNLOCK_OPERATION,
        AccessControlNotificationEvent.UNLOCK_BY_RF_WITH_INVALID_USER_CODE,
        AccessControlNotificationEvent.UNLOCK_OPERATION_WITH_USER_CODE,
    ),
}


@dataclass(repr=False, eq=False)
class ZWaveJSLock(BaseLock):
    """Class to represent ZWave JS lock."""

    lock_config_entry: ConfigEntry = field(repr=False)
    _listeners: list[Callable[[], None]] = field(init=False, default_factory=list)

    @property
    def node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @callback
    def _zwave_js_event_filter(self, event_data: dict[str, Any]) -> bool:
        """Filter out events."""
        # Try to find the lock that we are getting an event for, skipping
        # ones that don't match
        assert self.node.client.driver
        return (
            event_data[ATTR_HOME_ID] == self.node.client.driver.controller.home_id
            and event_data[ATTR_NODE_ID] == self.node.node_id
            and event_data[ATTR_DEVICE_ID] == self.lock.device_id
        )

    @callback
    def _handle_zwave_js_event(self, evt: Event) -> None:
        """Handle Z-Wave JS event."""
        if evt.data[ATTR_TYPE] != NotificationType.ACCESS_CONTROL:
            _LOGGER.debug(
                "Lock %s received non Access Control event: %s",
                self.lock.entity_id,
                evt.as_dict(),
            )
            return

        params = evt.data.get(ATTR_PARAMETERS) or {}
        code_slot = params.get("userId", 0)
        self.async_fire_code_slot_event(
            code_slot=code_slot,
            to_locked=next(
                (
                    to_locked
                    for to_locked, codes in ACCESS_CONTROL_NOTIFICATION_TO_LOCKED.items()
                    if evt.data[ATTR_EVENT] in codes
                ),
                None,
            ),
            action_text=evt.data.get(ATTR_EVENT_LABEL),
            source_data=evt,
        )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZWAVE_JS_DOMAIN

    async def async_setup(self) -> None:
        """Set up lock."""
        self._listeners.append(
            self.hass.bus.async_listen(
                ZWAVE_JS_NOTIFICATION_EVENT,
                self._handle_zwave_js_event,
                self._zwave_js_event_filter,
            )
        )

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        for listener in self._listeners:
            listener()
        self._listeners.clear()

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        if (
            client := (
                self.lock_config_entry.runtime_data
                if hasattr(self.lock_config_entry, "runtime_data")
                and self.lock_config_entry.runtime_data
                else self.hass.data.get(ZWAVE_JS_DOMAIN, {})
            )
            .get(self.lock_config_entry.entry_id, {})
            .get(DATA_CLIENT)
        ) is None:
            return False
        return (
            self.lock_config_entry.state == ConfigEntryState.LOADED
            and client.connected
            and client.driver is not None
        )

    async def async_hard_refresh_codes(self) -> None:
        """
        Perform hard refresh of all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock.
        """
        for config_entry in self.hass.config_entries.async_entries(DOMAIN):
            if self.lock.entity_id not in get_entry_data(config_entry, CONF_LOCKS, []):
                continue
            for code_slot in get_entry_data(config_entry, CONF_SLOTS, {}):
                await get_usercode_from_node(self.node, int(code_slot))

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
            ATTR_USERCODE: usercode,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_SET_LOCK_USERCODE, service_data
        )

    async def async_clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_CLEAR_LOCK_USERCODE, service_data
        )

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        code_slots: Iterable[int] = (
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id not in get_entry_data(entry, CONF_LOCKS, [])
        )
        data: dict[int, int | str] = {}
        code_slot = 1

        if not await self.async_is_connection_up():
            raise LockDisconnected

        try:
            for slot in get_usercodes(self.node):
                code_slot = int(slot["code_slot"])
                usercode: str = slot["usercode"] or ""
                in_use: bool | None = slot["in_use"]
                # Retrieve code slots that haven't been populated yet
                if in_use is None and code_slot in code_slots:
                    usercode_resp = await get_usercode_from_node(self.node, code_slot)
                    usercode = slot["usercode"] = usercode_resp["usercode"] or ""
                    in_use = slot["in_use"] = usercode_resp["in_use"]

                if not in_use:
                    if code_slot in code_slots:
                        _LOGGER.debug(
                            "Lock %s code slot %s not enabled",
                            self.lock.entity_id,
                            code_slot,
                        )
                    data[code_slot] = ""
                # Special handling if usercode is all *'s
                elif usercode and len(str(usercode)) * "*" == str(usercode):
                    # Build data from entities
                    config_entry = next(
                        config_entry
                        for config_entry in self.hass.config_entries.async_entries(
                            DOMAIN
                        )
                        if self.lock.entity_id
                        in get_entry_data(config_entry, CONF_LOCKS, [])
                        and int(code_slot)
                        in (
                            int(slot)
                            for slot in get_entry_data(config_entry, CONF_SLOTS, {})
                        )
                    )
                    base_unique_id = f"{config_entry.entry_id}|{code_slot}"
                    active = self.ent_reg.async_get_entity_id(
                        SWITCH_DOMAIN, DOMAIN, f"{base_unique_id}|{CONF_ENABLED}"
                    )
                    assert active
                    active_state = self.hass.states.get(active)
                    pin_entity_id = self.ent_reg.async_get_entity_id(
                        TEXT_DOMAIN, DOMAIN, f"{base_unique_id}|{CONF_PIN}"
                    )
                    assert pin_entity_id
                    pin_state = self.hass.states.get(pin_entity_id)
                    assert active_state
                    assert pin_state
                    if code_slot in code_slots:
                        _LOGGER.debug(
                            (
                                "PIN is masked for lock %s code slot %s so "
                                "assuming value from PIN entity %s"
                            ),
                            self.lock.entity_id,
                            code_slot,
                            pin_entity_id,
                        )
                    if active_state.state == STATE_ON and pin_state.state.isnumeric():
                        data[code_slot] = pin_state.state
                    else:
                        data[code_slot] = ""
                else:
                    if code_slot in code_slots:
                        _LOGGER.debug(
                            "Lock %s code slot %s has a PIN",
                            self.lock.entity_id,
                            code_slot,
                        )
                    data[code_slot] = usercode or ""
        except Exception as err:
            raise LockDisconnected from err

        return data
