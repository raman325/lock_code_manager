"""Base integration module."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import functools
from typing import Any, Literal, final

from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    ATTR_STATE,
    CONF_NAME,
    STATE_LOCKED,
    STATE_UNLOCKED,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_EXTRA_DATA,
    ATTR_FROM,
    ATTR_LCM_CONFIG_ENTRY_ID,
    ATTR_LOCK_CONFIG_ENTRY_ID,
    ATTR_NOTIFICATION_SOURCE,
    ATTR_TO,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from ..data import get_entry_data
from .const import LOGGER


@dataclass(repr=False, eq=False)
class BaseLock:
    """Base for lock instance."""

    hass: HomeAssistant = field(repr=False)
    dev_reg: dr.DeviceRegistry = field(repr=False)
    ent_reg: er.EntityRegistry = field(repr=False)
    lock_config_entry: ConfigEntry | None = field(repr=False)
    lock: er.RegistryEntry
    device_entry: dr.DeviceEntry | None = field(default=None, init=False)
    _aio_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    @final
    @callback
    def __post_init__(self) -> None:
        """Post initialization."""
        if not (device_id := self.lock.device_id):
            return
        self.device_entry = self.dev_reg.async_get(device_id)

    @final
    def __repr__(self) -> str:
        """Return string representation of self."""
        return f"{self.__class__.__name__}(domain={self.domain}, lock={self.lock.entity_id})"

    @final
    def __hash__(self) -> int:
        """Return hash of self."""
        return hash(self.lock.entity_id)

    @final
    def __eq__(self, other: Any) -> bool:
        """Return whether self is equal to other."""
        if not isinstance(other, BaseLock):
            return False
        return self.lock.entity_id == other.lock.entity_id

    @property
    def domain(self) -> str:
        """Return integration domain."""
        raise NotImplementedError()

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=1)

    def setup(self) -> None:
        """Set up lock."""
        pass

    async def async_setup(self) -> None:
        """Set up lock."""
        await self.hass.async_add_executor_job(self.setup)

    def unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        pass

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        await self.hass.async_add_executor_job(self.unload, remove_permanently)

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        raise NotImplementedError()

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return await self.hass.async_add_executor_job(self.is_connection_up)

    @final
    async def async_internal_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return await self.async_is_connection_up()

    def hard_refresh_codes(self) -> None:
        """
        Perform hard refresh all codes.

        Needed for integraitons where usercodes are cached and may get out of sync with
        the lock.
        """
        raise HomeAssistantError from NotImplementedError()

    async def async_hard_refresh_codes(self) -> None:
        """
        Perform hard refresh of all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock.
        """
        await self.hass.async_add_executor_job(self.hard_refresh_codes)

    @final
    async def async_internal_hard_refresh_codes(self) -> None:
        """
        Perform hard refresh of all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock.
        """
        async with self._aio_lock:
            await self.async_hard_refresh_codes()

    def set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        raise HomeAssistantError from NotImplementedError()

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        await self.hass.async_add_executor_job(
            functools.partial(self.set_usercode, code_slot, usercode, name=name)
        )

    @final
    async def async_internal_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        async with self._aio_lock:
            await self.async_set_usercode(code_slot, usercode, name=name)

    def clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        raise HomeAssistantError from NotImplementedError()

    async def async_clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        await self.hass.async_add_executor_job(self.clear_usercode, code_slot)

    @final
    async def async_internal_clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        async with self._aio_lock:
            await self.async_clear_usercode(code_slot)

    def get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }
        """
        raise NotImplementedError()

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }
        """
        return await self.hass.async_add_executor_job(self.get_usercodes)

    @final
    async def async_internal_get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }
        """
        async with self._aio_lock:
            return await self.async_get_usercodes()

    @final
    def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = True,
    ):
        """Call a hass service and log a failure on an error."""
        try:
            self.hass.services.call(
                domain, service, service_data=service_data, blocking=blocking
            )
        except Exception as err:
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )

    @final
    async def async_call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = True,
    ):
        """Call a hass service and log a failure on an error."""
        try:
            await self.hass.services.async_call(
                domain, service, service_data=service_data, blocking=blocking
            )
        except Exception as err:
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )

    @final
    @callback
    def async_fire_code_slot_event(
        self,
        code_slot: int | None = None,
        to_locked: bool | None = None,
        action_text: str | None = None,
        source_data: Event | State | dict[str, Any] | None = None,
    ) -> None:
        """
        Fire a code slot event.

        Sub-classes should call this whenever a code slot is used. source_data can
        include any data that is JSON serializable if the source is not a Home
        Assistant event or state.
        """
        name_state: State | None = None
        lock_entity_id = self.lock.entity_id
        lock_device_id = self.lock.device_id
        config_entry_id: str | None = None

        try:
            config_entry = next(
                config_entry
                for config_entry in self.hass.config_entries.async_entries(DOMAIN)
                if (
                    self.lock.entity_id in get_entry_data(config_entry, CONF_LOCKS, [])
                    and code_slot is not None
                    and int(code_slot)
                    in (
                        int(slot)
                        for slot in get_entry_data(config_entry, CONF_SLOTS, {})
                    )
                    and (
                        name_entity_id := self.ent_reg.async_get_entity_id(
                            TEXT_DOMAIN,
                            DOMAIN,
                            f"{config_entry.entry_id}|{code_slot}|{CONF_NAME}",
                        )
                    )
                )
            )
        except StopIteration:
            pass
        else:
            config_entry_id = config_entry.entry_id
            name_state = self.hass.states.get(name_entity_id)

        from_state: str | None = None
        to_state: str | None = None
        if to_locked:
            from_state = STATE_UNLOCKED
            to_state = STATE_LOCKED
        elif to_locked is False:
            from_state = STATE_LOCKED
            to_state = STATE_UNLOCKED

        notification_source: Literal["event", "state"] | None = None
        extra_data: dict[str, Any] | None = None
        if isinstance(source_data, Event):
            notification_source = "event"
            extra_data = {
                "event_type": source_data.event_type,
                "data": source_data.data,
                "time_fired": source_data.time_fired.isoformat(),
            }
        elif isinstance(source_data, State):
            notification_source = "state"
            last_changed_isoformat = source_data.last_changed.isoformat()
            if source_data.last_changed == source_data.last_updated:
                last_updated_isoformat = last_changed_isoformat
            else:
                last_updated_isoformat = source_data.last_updated.isoformat()
            extra_data = {
                "entity_id": source_data.entity_id,
                "state": source_data.state,
                "attributes": source_data.attributes,
                "last_changed": last_changed_isoformat,
                "last_updated": last_updated_isoformat,
            }
        elif isinstance(source_data, dict):
            extra_data = source_data

        event_data = {
            ATTR_NOTIFICATION_SOURCE: notification_source,
            ATTR_ENTITY_ID: lock_entity_id,
            ATTR_DEVICE_ID: lock_device_id,
            ATTR_LCM_CONFIG_ENTRY_ID: config_entry_id,
            ATTR_STATE: (
                state.state if (state := self.hass.states.get(lock_entity_id)) else ""
            ),
            ATTR_ACTION_TEXT: action_text,
            ATTR_CODE_SLOT: code_slot or 0,
            ATTR_CODE_SLOT_NAME: name_state.state if name_state else "",
            ATTR_FROM: from_state,
            ATTR_TO: to_state,
            ATTR_EXTRA_DATA: extra_data,
        }

        if self.lock_config_entry:
            event_data[ATTR_LOCK_CONFIG_ENTRY_ID] = self.lock_config_entry.entry_id

        self.hass.bus.async_fire(EVENT_LOCK_STATE_CHANGED, event_data=event_data)
