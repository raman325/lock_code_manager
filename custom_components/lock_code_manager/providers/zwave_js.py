"""Module for Z-Wave JS locks."""
from __future__ import annotations

from dataclasses import dataclass, field
import functools
from typing import Callable

from zwave_js_server.client import Client
from zwave_js_server.const.command_class.lock import (
    ATTR_CODE_SLOT,
    ATTR_IN_USE,
    ATTR_USERCODE,
)
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import get_usercode_from_node, get_usercodes

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.zwave_js.const import (
    ATTR_EVENT_LABEL,
    ATTR_HOME_ID,
    ATTR_NODE_ID,
    ATTR_PARAMETERS,
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
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import Event

from ..const import CONF_SLOTS, DOMAIN
from ..exceptions import LockDisconnected
from ._base import BaseLock
from .const import LOGGER


@dataclass
class ZWaveJSLock(BaseLock):
    """Class to represent ZWave JS lock."""

    _listeners: list[Callable[[], None]] = field(init=False, default_factory=list)

    @property
    def _node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @callback
    def _handle_zwave_js_event(self, evt: Event):
        """Handle Z-Wave JS event."""
        assert self._node.client.driver
        # Try to find the lock that we are getting an event for, skipping
        # ones that don't match
        if (
            not self._node
            or evt.data[ATTR_HOME_ID] != self._node.client.driver.controller.home_id
            or evt.data[ATTR_NODE_ID] != self._node.node_id
            or evt.data[ATTR_DEVICE_ID] != self.lock.device_id
        ):
            return

        params = evt.data.get(ATTR_PARAMETERS) or {}
        code_slot = params.get("userId", 0)
        self.fire_code_slot_event(code_slot, evt.data.get(ATTR_EVENT_LABEL), "event")

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZWAVE_JS_DOMAIN

    @property
    def device_entry(self) -> dr.DeviceEntry | None:
        """Return device info."""
        device_id = self.lock.device_id
        assert device_id
        return self.dev_reg.async_get(device_id)

    async def async_setup(self) -> None:
        """Set up lock."""
        self._listeners.append(
            self.hass.bus.async_listen(
                ZWAVE_JS_NOTIFICATION_EVENT,
                functools.partial(self._handle_zwave_js_event),
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

        Needed for integraitons where usercodes are cached and may get out of sync with
        the lock.
        """
        for code_slot in self.code_manager_config_entry.data[CONF_SLOTS]:
            await get_usercode_from_node(self._node, code_slot)
        return

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
        data: dict[int, int | str] = {}
        code_slot = 1

        if not await self.async_is_connection_up():
            raise LockDisconnected

        try:
            for slot in get_usercodes(self._node):
                code_slot = int(slot[ATTR_CODE_SLOT])
                usercode: str | None = slot[ATTR_USERCODE]
                in_use: bool | None = slot[ATTR_IN_USE]
                # Retrieve code slots that haven't been populated yet
                if (
                    in_use is None
                    and code_slot
                    in self.code_manager_config_entry.data[CONF_SLOTS].keys()
                ):
                    usercode_resp = await get_usercode_from_node(self._node, code_slot)
                    usercode = slot[ATTR_USERCODE] = usercode_resp[ATTR_USERCODE]
                    in_use = slot[ATTR_IN_USE] = usercode_resp[ATTR_IN_USE]

                model_name = self._node.device_config.label

                masked_pin = usercode and "*" in str(usercode)
                if not in_use:
                    LOGGER.debug("DEBUG: Code slot %s not enabled", code_slot)
                    data[code_slot] = ""
                elif masked_pin and model_name in ("BE469", "FE599"):
                    # Build data from entities
                    active = self.ent_reg.async_get_entity_id(
                        SWITCH_DOMAIN,
                        DOMAIN,
                        f"{self.lock.entity_id}_{code_slot}_{CONF_ENABLED}",
                    )
                    assert active
                    active_state = self.hass.states.get(active)
                    pin_data = self.ent_reg.async_get_entity_id(
                        TEXT_DOMAIN,
                        DOMAIN,
                        f"{self.lock.entity_id}_{code_slot}_{CONF_PIN}",
                    )
                    assert pin_data
                    pin_state = self.hass.states.get(pin_data)
                    assert active_state
                    assert pin_state
                    LOGGER.debug(
                        "Utilizing %s work around code to designate unused slot %s",
                        model_name,
                        code_slot,
                    )
                    if active_state.state == STATE_ON and pin_state.state.isnumeric():
                        data[code_slot] = pin_state.state
                    else:
                        data[code_slot] = ""
                else:
                    LOGGER.debug("DEBUG: Code slot %s value: %s", code_slot, usercode)
                    data[code_slot] = usercode
        except Exception as err:
            raise LockDisconnected from err

        return data
