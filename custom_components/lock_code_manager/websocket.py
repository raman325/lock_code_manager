"""Http views to control the config manager."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, Coroutine

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import CONF_CALENDAR, CONF_LOCKS, CONF_SLOTS, DOMAIN
from .data import get_entry_data

ERR_NOT_LOADED = "not_loaded"


def async_get_entry(
    orig_func: Callable[
        [HomeAssistant, websocket_api.ActiveConnection, dict[str, Any], ConfigEntry],
        Coroutine[Any, Any, None],
    ],
) -> Callable[
    [HomeAssistant, websocket_api.ActiveConnection, dict[str, Any]],
    Coroutine[Any, Any, None],
]:
    """Decorate async function to get entry."""

    @wraps(orig_func)
    async def async_get_entry_func(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        """Provide user specific data and store to function."""
        if config_entry_title := msg.get("config_entry_title"):
            config_entry = next(
                (
                    entry
                    for entry in hass.config_entries.async_entries(DOMAIN)
                    if slugify(entry.title) == slugify(config_entry_title)
                ),
                None,
            )
        elif config_entry_id := msg.get("config_entry_id"):
            config_entry = hass.config_entries.async_get_entry(config_entry_id)
        else:
            connection.send_error(
                msg["id"],
                websocket_api.const.ERR_INVALID_FORMAT,
                "Neither config_entry_title nor config_entry_id provided",
            )
            return

        if not config_entry:
            connection.send_error(
                msg["id"],
                websocket_api.const.ERR_NOT_FOUND,
                (
                    "No lock code manager config entry with tite `"
                    f"{config_entry_title}` found"
                ),
            )
            return

        if config_entry.state is not ConfigEntryState.LOADED:
            connection.send_error(
                msg["id"],
                ERR_NOT_LOADED,
                f"Config entry {config_entry.entry_id} not loaded",
            )
            return

        await orig_func(hass, connection, msg, config_entry)

    return async_get_entry_func


async def async_setup(hass: HomeAssistant) -> bool:
    """Enable the websocket_commands."""
    websocket_api.async_register_command(hass, get_slot_calendar_data)
    websocket_api.async_register_command(hass, get_config_entry_entities)

    return True


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/get_slot_calendar_data",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
    }
)
@websocket_api.async_response
@async_get_entry
async def get_slot_calendar_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """Return lock_code_manager config entry data."""
    connection.send_result(
        msg["id"],
        {
            CONF_LOCKS: get_entry_data(config_entry, CONF_LOCKS, []),
            CONF_SLOTS: {
                k: v.get(CONF_CALENDAR)
                for k, v in get_entry_data(config_entry, CONF_SLOTS, {}).items()
            },
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/get_config_entry_entities",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
    }
)
@websocket_api.async_response
@async_get_entry
async def get_config_entry_entities(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """Return lock_code_manager config entry data."""
    connection.send_result(
        msg["id"],
        {
            "config_entry": config_entry.as_json_fragment,
            "entities": [
                entity.as_partial_dict
                for entity in er.async_entries_for_config_entry(
                    er.async_get(hass), config_entry.entry_id
                )
            ],
        },
    )
