"""Http views to control the config manager."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import CONF_CALENDAR, CONF_LOCKS, CONF_NAME, CONF_SLOTS, DOMAIN
from .data import get_entry_data
from .providers import BaseLock

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
                    "No lock code manager config entry with title "
                    f"`{config_entry_title}` found"
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
    websocket_api.async_register_command(hass, subscribe_lock_coordinator_data)
    websocket_api.async_register_command(hass, get_locks)

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


def _slot_sort_key(slot: Any) -> tuple[int, str]:
    """Return a stable sort key for slot numbers that may be non-numeric."""
    try:
        return (0, f"{int(slot):010d}")
    except (TypeError, ValueError):
        return (1, str(slot))


def _serialize_slot(
    slot: Any,
    code: int | str | None,
    *,
    reveal: bool,
    name: str | None = None,
    managed: bool | None = None,
) -> dict[str, Any]:
    """Serialize a single slot, optionally masking the code."""
    result: dict[str, Any] = {"slot": slot}
    if name:
        result["name"] = name
    if managed is not None:
        result["managed"] = managed
    if reveal or code is None:
        result["code"] = code
    else:
        # Masked: send code_length instead of actual code
        result["code"] = None
        result["code_length"] = len(str(code))
    return result


def _slot_variants(slot: Any) -> set[Any]:
    """Return comparable variants of a slot identifier (string/int)."""
    variants: set[Any] = {slot}
    try:
        slot_int = int(slot)
    except (TypeError, ValueError):
        variants.add(str(slot))
    else:
        variants.add(slot_int)
        variants.add(str(slot_int))
    return variants


def _get_managed_slots(hass: HomeAssistant, lock_entity_id: str) -> set[Any]:
    """Return slot identifiers managed by LCM for a given lock."""
    managed_slots: set[Any] = set()
    for entry in hass.config_entries.async_entries(DOMAIN):
        if lock_entity_id not in get_entry_data(entry, CONF_LOCKS, []):
            continue
        for slot_num in get_entry_data(entry, CONF_SLOTS, {}):
            managed_slots.update(_slot_variants(slot_num))
    return managed_slots


def _get_slot_names(hass: HomeAssistant, lock_entity_id: str) -> dict[int, str]:
    """Get slot names from LCM text entities for a lock."""
    slot_names: dict[int, str] = {}
    ent_reg = er.async_get(hass)

    # Find config entries that manage this lock
    for entry in hass.config_entries.async_entries(DOMAIN):
        if lock_entity_id not in get_entry_data(entry, CONF_LOCKS, []):
            continue

        # Get slot names from text entities
        for slot_num in get_entry_data(entry, CONF_SLOTS, {}):
            unique_id = f"{entry.entry_id}|{slot_num}|{CONF_NAME}"
            if entity_id := ent_reg.async_get_entity_id(TEXT_DOMAIN, DOMAIN, unique_id):
                if state := hass.states.get(entity_id):
                    if state.state and state.state not in ("unknown", "unavailable"):
                        slot_names[int(slot_num)] = state.state

    return slot_names


def _get_lock_friendly_name(hass: HomeAssistant, lock: BaseLock) -> str:
    """Get the friendly name for a lock, using state attributes as primary source."""
    # Prefer the friendly_name from state (what HA displays in the UI)
    if state := hass.states.get(lock.lock.entity_id):
        if friendly_name := state.attributes.get("friendly_name"):
            return friendly_name
    # Fall back to entity registry name/original_name
    return lock.lock.name or lock.lock.original_name or lock.lock.entity_id


def _serialize_lock_coordinator(
    hass: HomeAssistant, lock: BaseLock, *, reveal: bool = False
) -> dict[str, Any]:
    """Serialize coordinator data for a lock."""
    coordinator = lock.coordinator
    data = coordinator.data if coordinator is not None else {}
    slot_names = _get_slot_names(hass, lock.lock.entity_id)
    managed_slots = _get_managed_slots(hass, lock.lock.entity_id)
    return {
        "lock_entity_id": lock.lock.entity_id,
        "lock_name": _get_lock_friendly_name(hass, lock),
        "slots": [
            _serialize_slot(
                slot,
                code,
                reveal=reveal,
                name=slot_names.get(int(slot)) if str(slot).isdigit() else None,
                managed=slot in managed_slots or str(slot) in managed_slots,
            )
            for slot, code in sorted(
                data.items(), key=lambda item: _slot_sort_key(item[0])
            )
        ],
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/subscribe_lock_coordinator_data",
        vol.Required("lock_entity_id"): str,
        vol.Optional("reveal", default=False): bool,
    }
)
@websocket_api.async_response
async def subscribe_lock_coordinator_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to coordinator data updates for a lock."""
    lock_entity_id = msg["lock_entity_id"]
    reveal = msg["reveal"]
    lock = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {}).get(lock_entity_id)
    if not lock:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            f"Lock {lock_entity_id} is not managed by Lock Code Manager",
        )
        return

    coordinator = lock.coordinator

    def _send_update() -> None:
        connection.send_event(
            msg["id"], _serialize_lock_coordinator(hass, lock, reveal=reveal)
        )

    def _noop() -> None:
        pass

    if coordinator is not None:
        unsub = coordinator.async_add_listener(_send_update)
    else:
        unsub = _noop

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    _send_update()


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/get_locks",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
    }
)
@websocket_api.async_response
async def get_locks(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """
    Return LCM-managed locks, optionally scoped to a config entry.

    Security note: When called without params, this enumerates all LCM-managed
    locks. This is acceptable since lock entity IDs are already visible via the
    Home Assistant API, and no sensitive data (codes) is exposed here.
    """
    all_locks = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})

    # If config entry specified, filter to locks from that entry
    if config_entry_id := msg.get("config_entry_id"):
        entry = hass.config_entries.async_get_entry(config_entry_id)
        entry_locks = get_entry_data(entry, CONF_LOCKS, []) if entry else []
        locks = {k: v for k, v in all_locks.items() if k in entry_locks}
    elif config_entry_title := msg.get("config_entry_title"):
        entry = next(
            (
                e
                for e in hass.config_entries.async_entries(DOMAIN)
                if slugify(e.title) == slugify(config_entry_title)
            ),
            None,
        )
        entry_locks = get_entry_data(entry, CONF_LOCKS, []) if entry else []
        locks = {k: v for k, v in all_locks.items() if k in entry_locks}
    else:
        locks = all_locks

    connection.send_result(
        msg["id"],
        {
            "locks": [
                {
                    "entity_id": lock_id,
                    "name": lock.lock.name
                    or lock.lock.original_name
                    or lock.lock.entity_id,
                }
                for lock_id, lock in locks.items()
            ]
        },
    )
