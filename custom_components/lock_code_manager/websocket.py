"""Http views to control the config manager."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ENABLED, CONF_PIN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import slugify

from .const import (
    ATTR_ACTIVE,
    ATTR_IN_SYNC,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_NAME,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
)
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
    websocket_api.async_register_command(hass, subscribe_lock_slot_data)
    websocket_api.async_register_command(hass, subscribe_slot_data)
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
    configured_code: str | None = None,
    active: bool | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Serialize a single slot, optionally masking the code.

    - code/code_length: What's actually on the lock (actual state)
    - configured_code/configured_code_length: What LCM has configured (desired state)
      Always included for managed slots, even if code is active on lock.
    - active: True if enabled + conditions met, False if inactive, None if unknown
    - enabled: True if enabled switch is ON, False if OFF, None if unknown
    """
    result: dict[str, Any] = {"slot": slot}
    if name:
        result["name"] = name
    if managed is not None:
        result["managed"] = managed
    if active is not None:
        result["active"] = active
    if enabled is not None:
        result["enabled"] = enabled

    # Code on the lock (actual state)
    if reveal or code is None:
        result["code"] = code
    else:
        # Masked: send code_length instead of actual code
        result["code"] = None
        result["code_length"] = len(str(code))

    # Configured code from LCM (desired state) - always include for managed slots
    if configured_code is not None:
        if reveal:
            result["configured_code"] = configured_code
        else:
            result["configured_code_length"] = len(configured_code)

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


@dataclass
class SlotEntityIds:
    """Entity IDs for a single slot's LCM entities."""

    slot_num: int
    name: str | None = None
    pin: str | None = None
    active: str | None = None
    enabled: str | None = None

    def all_ids(self) -> list[str]:
        """Return all non-None entity IDs."""
        return [eid for eid in (self.name, self.pin, self.active, self.enabled) if eid]


@dataclass
class SlotMetadata:
    """Metadata for a single slot from LCM entities."""

    name: str | None = None
    configured_pin: str | None = None
    active: bool | None = None
    enabled: bool | None = None


def _get_slot_entity_ids(
    hass: HomeAssistant, lock_entity_id: str
) -> dict[int, SlotEntityIds]:
    """Get entity IDs for all slots managed by LCM for a lock.

    Returns a dict mapping slot number to SlotEntityIds containing the entity IDs
    for name, PIN, active, and enabled entities.

    Note: If multiple config entries manage the same lock with overlapping slot
    numbers (which shouldn't happen in normal use), the last entry wins. This is
    expected behavior since slot conflicts are validated during config flow.
    """
    slot_entities: dict[int, SlotEntityIds] = {}
    ent_reg = er.async_get(hass)

    for entry in hass.config_entries.async_entries(DOMAIN):
        if lock_entity_id not in get_entry_data(entry, CONF_LOCKS, []):
            continue

        for slot_num in get_entry_data(entry, CONF_SLOTS, {}):
            slot_int = int(slot_num)

            # Build unique IDs for each entity type
            name_uid = f"{entry.entry_id}|{slot_num}|{CONF_NAME}"
            pin_uid = f"{entry.entry_id}|{slot_num}|{CONF_PIN}"
            active_uid = f"{entry.entry_id}|{slot_num}|{ATTR_ACTIVE}"
            enabled_uid = f"{entry.entry_id}|{slot_num}|{CONF_ENABLED}"

            slot_entities[slot_int] = SlotEntityIds(
                slot_num=slot_int,
                name=ent_reg.async_get_entity_id(TEXT_DOMAIN, DOMAIN, name_uid),
                pin=ent_reg.async_get_entity_id(TEXT_DOMAIN, DOMAIN, pin_uid),
                active=ent_reg.async_get_entity_id(
                    BINARY_SENSOR_DOMAIN, DOMAIN, active_uid
                ),
                enabled=ent_reg.async_get_entity_id(SWITCH_DOMAIN, DOMAIN, enabled_uid),
            )

    return slot_entities


def _get_slot_metadata(
    hass: HomeAssistant, lock_entity_id: str
) -> dict[int, SlotMetadata]:
    """Get all slot metadata from LCM entities for a lock in one pass.

    Returns a dict mapping slot number to SlotMetadata containing:
    - name: From text entity
    - configured_pin: From text entity
    - active: From binary sensor (True=on, False=off, None=unknown)
    - enabled: From switch (True=on, False=off, None=unknown)
    """

    def _get_text_state(entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        if state := hass.states.get(entity_id):
            if state.state and state.state not in ("unknown", "unavailable"):
                return state.state
        return None

    def _get_bool_state(entity_id: str | None) -> bool | None:
        if not entity_id:
            return None
        if state := hass.states.get(entity_id):
            if state.state == "on":
                return True
            if state.state == "off":
                return False
        return None

    slot_entities = _get_slot_entity_ids(hass, lock_entity_id)
    return {
        slot_num: SlotMetadata(
            name=_get_text_state(ids.name),
            configured_pin=_get_text_state(ids.pin),
            active=_get_bool_state(ids.active),
            enabled=_get_bool_state(ids.enabled),
        )
        for slot_num, ids in slot_entities.items()
    }


def _get_slot_state_entity_ids(hass: HomeAssistant, lock_entity_id: str) -> list[str]:
    """Get entity IDs for slot state tracking (enabled, active, name, PIN).

    Returns the specific LCM entity IDs whose state changes should trigger
    websocket subscription updates for this lock's slots.
    """
    slot_entities = _get_slot_entity_ids(hass, lock_entity_id)
    entity_ids: list[str] = []
    for ids in slot_entities.values():
        entity_ids.extend(ids.all_ids())
    return entity_ids


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
    managed_slots = _get_managed_slots(hass, lock.lock.entity_id)
    slot_metadata = _get_slot_metadata(hass, lock.lock.entity_id)

    def _get_metadata(slot: Any) -> SlotMetadata | None:
        if str(slot).isdigit():
            return slot_metadata.get(int(slot))
        return None

    slots = []
    for slot, code in sorted(data.items(), key=lambda item: _slot_sort_key(item[0])):
        meta = _get_metadata(slot)
        slots.append(
            _serialize_slot(
                slot,
                code,
                reveal=reveal,
                name=meta.name if meta else None,
                managed=slot in managed_slots or str(slot) in managed_slots,
                configured_code=meta.configured_pin if meta else None,
                active=meta.active if meta else None,
                enabled=meta.enabled if meta else None,
            )
        )

    return {
        "lock_entity_id": lock.lock.entity_id,
        "lock_name": _get_lock_friendly_name(hass, lock),
        "slots": slots,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/subscribe_lock_slot_data",
        vol.Required("lock_entity_id"): str,
        vol.Optional("reveal", default=False): bool,
    }
)
@websocket_api.async_response
async def subscribe_lock_slot_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe to coordinator data and LCM entity state updates for a lock.

    Triggers updates when:
    - Lock coordinator data changes (codes on lock)
    - LCM entity states change (enabled, active, name, configured PIN)
    """
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

    @callback
    def _send_update() -> None:
        connection.send_event(
            msg["id"], _serialize_lock_coordinator(hass, lock, reveal=reveal)
        )

    @callback
    def _on_state_change(event: Event[EventStateChangedData]) -> None:
        """Handle LCM entity state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Only send update if actual state value changed or entity created/removed
        if old_state is None or new_state is None or old_state.state != new_state.state:
            _send_update()

    # Track coordinator updates (lock code changes)
    unsub_coordinator = (
        coordinator.async_add_listener(_send_update) if coordinator else lambda: None
    )

    # Track LCM entity state changes (enabled, active, name, PIN)
    slot_entity_ids = _get_slot_state_entity_ids(hass, lock_entity_id)
    unsub_state = (
        async_track_state_change_event(hass, slot_entity_ids, _on_state_change)
        if slot_entity_ids
        else lambda: None
    )

    def _unsub_all() -> None:
        unsub_coordinator()
        unsub_state()

    connection.subscriptions[msg["id"]] = _unsub_all
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
                    "name": _get_lock_friendly_name(hass, lock),
                }
                for lock_id, lock in locks.items()
            ]
        },
    )


@dataclass
class SlotEntityData:
    """Entity IDs and data for a single slot."""

    slot_num: int
    name_entity_id: str | None = None
    pin_entity_id: str | None = None
    enabled_entity_id: str | None = None
    active_entity_id: str | None = None
    number_of_uses_entity_id: str | None = None

    def all_entity_ids(self) -> list[str]:
        """Return all non-None entity IDs for state tracking."""
        return [
            eid
            for eid in (
                self.name_entity_id,
                self.pin_entity_id,
                self.enabled_entity_id,
                self.active_entity_id,
                self.number_of_uses_entity_id,
            )
            if eid
        ]


def _get_slot_entity_data(
    hass: HomeAssistant, config_entry: ConfigEntry, slot_num: int
) -> SlotEntityData:
    """Get entity IDs for a specific slot."""
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id

    def _get_entity_id(domain: str, key: str) -> str | None:
        unique_id = f"{entry_id}|{slot_num}|{key}"
        return ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)

    return SlotEntityData(
        slot_num=slot_num,
        name_entity_id=_get_entity_id(TEXT_DOMAIN, CONF_NAME),
        pin_entity_id=_get_entity_id(TEXT_DOMAIN, CONF_PIN),
        enabled_entity_id=_get_entity_id(SWITCH_DOMAIN, CONF_ENABLED),
        active_entity_id=_get_entity_id(BINARY_SENSOR_DOMAIN, ATTR_ACTIVE),
        number_of_uses_entity_id=_get_entity_id(NUMBER_DOMAIN, CONF_NUMBER_OF_USES),
    )


def _get_slot_in_sync_entity_ids(
    hass: HomeAssistant, config_entry: ConfigEntry, slot_num: int
) -> dict[str, str]:
    """Get in_sync entity IDs for each lock for a specific slot.

    Returns dict mapping lock_entity_id to in_sync_entity_id.
    """
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    lock_entity_ids = get_entry_data(config_entry, CONF_LOCKS, [])

    in_sync_map: dict[str, str] = {}
    for lock_entity_id in lock_entity_ids:
        unique_id = f"{entry_id}|{slot_num}|{ATTR_IN_SYNC}|{lock_entity_id}"
        if entity_id := ent_reg.async_get_entity_id(
            BINARY_SENSOR_DOMAIN, DOMAIN, unique_id
        ):
            in_sync_map[lock_entity_id] = entity_id

    return in_sync_map


def _serialize_slot_card_data(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    slot_entities: SlotEntityData,
    in_sync_map: dict[str, str],
    *,
    reveal: bool,
) -> dict[str, Any]:
    """Serialize slot data for the slot card."""

    def _get_text_state(entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        if state := hass.states.get(entity_id):
            if state.state and state.state not in ("unknown", "unavailable"):
                return state.state
        return None

    def _get_bool_state(entity_id: str | None) -> bool | None:
        if not entity_id:
            return None
        if state := hass.states.get(entity_id):
            if state.state == "on":
                return True
            if state.state == "off":
                return False
        return None

    def _get_number_state(entity_id: str | None) -> int | None:
        if not entity_id:
            return None
        if state := hass.states.get(entity_id):
            if state.state and state.state not in ("unknown", "unavailable"):
                try:
                    return int(float(state.state))
                except (ValueError, TypeError):
                    return None
        return None

    # Get slot metadata
    name = _get_text_state(slot_entities.name_entity_id) or ""
    pin = _get_text_state(slot_entities.pin_entity_id)
    enabled = _get_bool_state(slot_entities.enabled_entity_id)
    active = _get_bool_state(slot_entities.active_entity_id)
    number_of_uses = _get_number_state(slot_entities.number_of_uses_entity_id)

    # Get calendar from config (slot keys can be int or str)
    slots_data = get_entry_data(config_entry, CONF_SLOTS, {})
    slot_config = slots_data.get(slot_num) or slots_data.get(str(slot_num)) or {}
    calendar_entity_id = (
        slot_config.get(CONF_CALENDAR) if isinstance(slot_config, dict) else None
    )

    # Build per-lock status
    locks_data: list[dict[str, Any]] = []
    all_locks = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})
    entry_lock_ids = get_entry_data(config_entry, CONF_LOCKS, [])

    for lock_entity_id in entry_lock_ids:
        lock = all_locks.get(lock_entity_id)
        if not lock:
            continue

        lock_name = _get_lock_friendly_name(hass, lock)
        in_sync_entity_id = in_sync_map.get(lock_entity_id)
        in_sync = _get_bool_state(in_sync_entity_id)

        # Get code from coordinator
        coordinator = lock.coordinator
        code_on_lock = None
        code_length = None
        if coordinator and coordinator.data:
            raw_code = coordinator.data.get(slot_num)
            if raw_code is not None:
                if reveal:
                    code_on_lock = str(raw_code)
                else:
                    code_length = len(str(raw_code))

        lock_status: dict[str, Any] = {
            "entity_id": lock_entity_id,
            "name": lock_name,
            "in_sync": in_sync,
        }
        if reveal and code_on_lock is not None:
            lock_status["code"] = code_on_lock
        elif code_length is not None:
            lock_status["code"] = None
            lock_status["code_length"] = code_length
        else:
            lock_status["code"] = None

        locks_data.append(lock_status)

    # Build result
    result: dict[str, Any] = {
        "slot_num": slot_num,
        "name": name,
        "enabled": enabled,
        "active": active,
        "locks": locks_data,
        "conditions": {},
    }

    # PIN (masked or revealed)
    if reveal:
        result["pin"] = pin
    elif pin:
        result["pin"] = None
        result["pin_length"] = len(pin)
    else:
        result["pin"] = None

    # Conditions
    if number_of_uses is not None:
        result["conditions"]["number_of_uses"] = number_of_uses
    if calendar_entity_id:
        result["conditions"]["calendar_entity_id"] = calendar_entity_id

    return result


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/subscribe_slot_data",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
        vol.Required("slot"): int,
        vol.Optional("reveal", default=False): bool,
    }
)
@websocket_api.async_response
@async_get_entry
async def subscribe_slot_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """Subscribe to slot data for a specific slot in a config entry.

    Provides real-time updates when:
    - Slot entities change (name, PIN, enabled, active, number_of_uses)
    - Lock coordinator data changes (codes on locks)
    - In-sync status changes
    """
    slot_num = msg["slot"]
    reveal = msg["reveal"]

    # Validate slot exists in config (slot keys can be int or str)
    slots = get_entry_data(config_entry, CONF_SLOTS, {})
    if slot_num not in slots and str(slot_num) not in slots:
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_NOT_FOUND,
            f"Slot {slot_num} not found in config entry",
        )
        return

    # Get entity data
    slot_entities = _get_slot_entity_data(hass, config_entry, slot_num)
    in_sync_map = _get_slot_in_sync_entity_ids(hass, config_entry, slot_num)

    @callback
    def _send_update() -> None:
        connection.send_event(
            msg["id"],
            _serialize_slot_card_data(
                hass, config_entry, slot_num, slot_entities, in_sync_map, reveal=reveal
            ),
        )

    @callback
    def _on_state_change(event: Event[EventStateChangedData]) -> None:
        """Handle entity state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Only send update if actual state value changed
        if old_state is None or new_state is None or old_state.state != new_state.state:
            _send_update()

    # Track slot entity state changes
    tracked_entities = slot_entities.all_entity_ids() + list(in_sync_map.values())
    unsub_state = (
        async_track_state_change_event(hass, tracked_entities, _on_state_change)
        if tracked_entities
        else lambda: None
    )

    # Track coordinator updates for all locks
    unsub_coordinators: list[Any] = []
    all_locks = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})
    entry_lock_ids = get_entry_data(config_entry, CONF_LOCKS, [])

    for lock_entity_id in entry_lock_ids:
        lock = all_locks.get(lock_entity_id)
        if lock and lock.coordinator:
            unsub_coordinators.append(lock.coordinator.async_add_listener(_send_update))

    def _unsub_all() -> None:
        unsub_state()
        for unsub in unsub_coordinators:
            unsub()

    connection.subscriptions[msg["id"]] = _unsub_all
    connection.send_result(msg["id"])
    _send_update()
