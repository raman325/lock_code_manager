"""Module for Z-Wave JS locks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.lock import ATTR_CODE_SLOT, ATTR_USERCODE
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import (
    get_usercode,
    get_usercode_from_node,
    get_usercodes,
)

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.zwave_js.const import (
    ATTR_EVENT,
    ATTR_EVENT_LABEL,
    ATTR_HOME_ID,
    ATTR_NODE_ID,
    ATTR_PARAMETERS,
    ATTR_TYPE,
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

# Avoid repeated CC fetches for missing usercode slots.
USERCODE_FETCH_COOLDOWN = 60.0
# Default interval between hard refreshes (checksum-optimized full code refresh)
DEFAULT_HARD_REFRESH_INTERVAL = timedelta(hours=1)
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
    _usercode_fetch_attempts: dict[int, float] = field(init=False, default_factory=dict)

    def _should_fetch_usercode(self, code_slot: int) -> bool:
        """Return True if we should fetch a missing usercode slot."""
        last_attempt = self._usercode_fetch_attempts.get(code_slot)
        if last_attempt is None:
            return True
        return (time.monotonic() - last_attempt) >= USERCODE_FETCH_COOLDOWN

    async def _fetch_usercode_from_node(self, code_slot: int) -> dict[str, Any] | None:
        """Fetch a usercode directly from the node with cooldown."""
        if not self._should_fetch_usercode(code_slot):
            return None
        try:
            usercode_resp = await get_usercode_from_node(self.node, code_slot)
        except Exception:
            self._usercode_fetch_attempts[code_slot] = time.monotonic()
            return None
        self._usercode_fetch_attempts.pop(code_slot, None)
        return usercode_resp

    @property
    def node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """
        Return interval between hard refreshes.

        Z-Wave JS caches user codes and may get out of sync if codes are changed
        at the lock's keypad. The hard refresh uses Z-Wave JS's checksum optimization
        to minimize network traffic when codes haven't changed.
        """
        return DEFAULT_HARD_REFRESH_INTERVAL

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

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock."""
        await super().async_setup(config_entry)
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
        if not (runtime_data := self.lock_config_entry.runtime_data):
            return False

        return bool(
            self.lock_config_entry.state == ConfigEntryState.LOADED
            and runtime_data.client.connected
            and runtime_data.client.driver is not None
        )

    async def async_hard_refresh_codes(self) -> None:
        """
        Perform hard refresh of all codes.

        Uses Z-Wave JS's refresh_cc_values which handles checksum optimization
        internally - it will skip re-fetching codes if the checksum hasn't changed.
        """
        await self.node.async_refresh_cc_values(CommandClass.USER_CODE)

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this value.
        """
        # Check if the code is already set to this value (avoid unnecessary network call)
        try:
            current = get_usercode(self.node, code_slot)
            if current.get("in_use") and str(current.get("usercode", "")) == str(
                usercode
            ):
                _LOGGER.debug(
                    "Lock %s slot %s already has this PIN, skipping set",
                    self.lock.entity_id,
                    code_slot,
                )
                return False
        except Exception:
            # If we can't check the cache, proceed with the set
            pass

        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
            ATTR_USERCODE: usercode,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_SET_LOCK_USERCODE, service_data
        )
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        """
        # Check if the slot is already cleared (avoid unnecessary network call)
        try:
            current = get_usercode(self.node, code_slot)
            if not current.get("in_use"):
                _LOGGER.debug(
                    "Lock %s slot %s already cleared, skipping clear",
                    self.lock.entity_id,
                    code_slot,
                )
                return False
        except Exception:
            # If we can't check the cache, proceed with the clear
            pass

        service_data = {
            ATTR_ENTITY_ID: self.lock.entity_id,
            ATTR_CODE_SLOT: code_slot,
        }
        await self.async_call_service(
            ZWAVE_JS_DOMAIN, SERVICE_CLEAR_LOCK_USERCODE, service_data
        )
        return True

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }
        data: dict[int, int | str] = {}
        code_slot = 1

        if not await self.async_is_connection_up():
            raise LockDisconnected

        try:
            slots = list(get_usercodes(self.node) or [])
            slots_by_num = {int(slot["code_slot"]): slot for slot in slots}

            # Fetch configured slots that aren't tracked by Z-Wave JS yet.
            missing_slots = sorted(code_slots - set(slots_by_num))
            for slot_num in missing_slots:
                if usercode_resp := await self._fetch_usercode_from_node(slot_num):
                    slots.append(usercode_resp)
                    slots_by_num[int(usercode_resp["code_slot"])] = usercode_resp

            for slot in slots:
                code_slot = int(slot["code_slot"])
                usercode: str = slot["usercode"] or ""
                in_use: bool | None = slot["in_use"]
                # Retrieve code slots that haven't been populated yet
                if in_use is None and code_slot in code_slots:
                    usercode_resp = await self._fetch_usercode_from_node(code_slot)
                    if usercode_resp:
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
