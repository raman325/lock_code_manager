"""Base integration module."""
from __future__ import annotations

from dataclasses import dataclass
import functools
from typing import Any, Literal, final

from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, ATTR_STATE, CONF_NAME
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo

from ..const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_NOTIFICATION_SOURCE,
    EVENT_LOCK_STATE_CHANGED,
)
from .const import LOGGER


@dataclass
class BaseLock:
    """Base for lock instance."""

    hass: HomeAssistant
    dev_reg: dr.DeviceRegistry
    ent_reg: er.EntityRegistry
    code_manager_config_entry: ConfigEntry
    lock_config_entry: ConfigEntry
    lock: er.RegistryEntry

    @property
    def domain(self) -> str:
        """Return integration domain."""
        raise NotImplementedError()

    @property
    def device_info(self) -> DeviceInfo | None:
        """
        Return device info.

        Typically attaches to the device registry entry for the lock.
        """
        return None

    def __post_init__(self) -> None:
        """Post initialization."""
        pass

    def setup(self) -> None:
        """Set up lock."""
        pass

    async def async_setup(self) -> None:
        """Set up lock."""
        await self.hass.async_add_executor_job(self.setup)

    def unload(self) -> None:
        """Unload lock."""
        pass

    async def async_unload(self) -> None:
        """Unload lock."""
        await self.hass.async_add_executor_job(self.unload)

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        raise NotImplementedError()

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return await self.hass.async_add_executor_job(self.is_connection_up)

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

        Needed for integraitons where usercodes are cached and may get out of sync with
        the lock.
        """
        await self.hass.async_add_executor_job(self.hard_refresh_codes)

    def set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        raise NotImplementedError()

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        await self.hass.async_add_executor_job(
            functools.partial(self.set_usercode, code_slot, usercode, name=name)
        )

    def clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        raise NotImplementedError()

    async def async_clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        await self.hass.async_add_executor_job(self.clear_usercode, code_slot)

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
        return self.hass.async_add_executor_job(self.get_usercodes)

    @final
    def call_service(
        self, domain: str, service: str, service_data: dict[str, Any] | None = None
    ):
        """Call a hass service and log a failure on an error."""
        try:
            self.hass.services.call(
                domain, service, service_data=service_data, blocking=True
            )
        except Exception as err:
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )
            raise err

    @final
    async def async_call_service(
        self, domain: str, service: str, service_data: dict[str, Any] | None = None
    ):
        """Call a hass service and log a failure on an error."""
        try:
            await self.hass.services.async_call(
                domain, service, service_data=service_data, blocking=True
            )
        except Exception as err:
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )
            raise err

    @final
    @callback
    def fire_code_slot_event(
        self,
        code_slot: int | None = None,
        action_text: str | None = None,
        notification_source: Literal["event", "state"] = "event",
    ) -> None:
        """
        Fire a code slot event.

        Sub-classes should call this whenever a code slot is used.
        """
        name_state: State | None = None
        lock_entity_id = self.lock.entity_id
        if name_entity_id := self.ent_reg.async_get_entity_id(
            TEXT_DOMAIN, self.domain, f"{lock_entity_id}_{code_slot}_{CONF_NAME}"
        ):
            name_state = self.hass.states.get(name_entity_id)
        self.hass.bus.async_fire(
            EVENT_LOCK_STATE_CHANGED,
            event_data={
                ATTR_NOTIFICATION_SOURCE: notification_source,
                ATTR_ENTITY_ID: lock_entity_id,
                ATTR_STATE: state.state
                if (state := self.hass.states.get(lock_entity_id))
                else "",
                ATTR_ACTION_TEXT: action_text,
                ATTR_CODE_SLOT: code_slot or 0,
                ATTR_CODE_SLOT_NAME: name_state.state if name_state is not None else "",
            },
        )
