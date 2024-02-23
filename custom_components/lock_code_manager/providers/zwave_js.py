"""Module for Z-Wave JS locks."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Callable, Iterable

from zwave_js_server.client import Client
from zwave_js_server.const.command_class.lock import (
    ATTR_CODE_SLOT,
    ATTR_IN_USE,
    ATTR_USERCODE,
)
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
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_PIN,
    STATE_ON,
)
from homeassistant.core import Event, callback

from ..const import CONF_SLOTS, DOMAIN
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


@dataclass(repr=False)
class ZWaveJSLock(BaseLock):
    """Class to represent ZWave JS lock."""

    _listeners: list[Callable[[], None]] = field(init=False, default_factory=list)

    @property
    def node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @callback
    def _zwave_js_event_filter(self, evt: Event) -> bool:
        """Filter out events."""
        # Try to find the lock that we are getting an event for, skipping
        # ones that don't match
        assert self.node.client.driver
        return (
            self.node
            and evt.data[ATTR_HOME_ID] == self.node.client.driver.controller.home_id
            and evt.data[ATTR_NODE_ID] == self.node.node_id
            and evt.data[ATTR_DEVICE_ID] == self.lock.device_id
        )

    @callback
    def _handle_zwave_js_event(self, evt: Event) -> None:
        """Handle Z-Wave JS event."""
        if evt.data[ATTR_TYPE] != NotificationType.ACCESS_CONTROL:
            _LOGGER.debug(
                "%s (%s): Lock %s received non Access Control event: %s",
                self.config_entry.entry_id,
                self.config_entry.title,
                self.lock.entity_id,
                evt.as_dict(),
            )
            return

        params = evt.data.get(ATTR_PARAMETERS) or {}
        code_slot = params.get("userId", 0)
        self.fire_code_slot_event(
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

    async def async_unload(self) -> None:
        """Unload lock."""
        for listener in self._listeners:
            listener()
        self._listeners.clear()

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        client: Client = self.hass.data[ZWAVE_JS_DOMAIN][
            self.lock_config_entry.entry_id
        ][DATA_CLIENT]
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
        for code_slot in self.config_entry.data[CONF_SLOTS]:
            await get_usercode_from_node(self.node, code_slot)

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
            self.config_entry.data.get(
                CONF_SLOTS, self.config_entry.options.get(CONF_SLOTS)
            ).keys()
            or []
        )
        data: dict[int, int | str] = {}
        code_slot = 1

        if not await self.async_is_connection_up():
            raise LockDisconnected

        try:
            for slot in get_usercodes(self.node):
                code_slot = int(slot[ATTR_CODE_SLOT])
                usercode: str = slot[ATTR_USERCODE] or ""
                in_use: bool | None = slot[ATTR_IN_USE]
                # Retrieve code slots that haven't been populated yet
                if in_use is None and code_slot in code_slots:
                    usercode_resp = await get_usercode_from_node(self.node, code_slot)
                    usercode = slot[ATTR_USERCODE] = usercode_resp[ATTR_USERCODE] or ""
                    in_use = slot[ATTR_IN_USE] = usercode_resp[ATTR_IN_USE]

                if not in_use:
                    if code_slot in code_slots:
                        _LOGGER.debug(
                            "%s (%s): Lock %s code slot %s not enabled",
                            self.config_entry.entry_id,
                            self.config_entry.title,
                            self.lock.entity_id,
                            code_slot,
                        )
                    data[code_slot] = ""
                # Special handling if usercode is all *'s
                elif usercode and len(str(usercode)) * "*" == str(usercode):
                    # Build data from entities
                    base_unique_id = f"{self.base_unique_id}|{code_slot}"
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
                                "%s (%s): PIN is masked for lock %s code slot %s so "
                                "assuming value from PIN entity %s"
                            ),
                            self.config_entry.entry_id,
                            self.config_entry.title,
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
                            "%s (%s): Lock %s code slot %s has a PIN",
                            self.config_entry.entry_id,
                            self.config_entry.title,
                            self.lock.entity_id,
                            code_slot,
                        )
                    data[code_slot] = usercode or ""
        except Exception as err:
            raise LockDisconnected from err

        return data
