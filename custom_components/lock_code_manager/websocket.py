"""Http views to control the config manager."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import timedelta
from functools import wraps
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    SERVICE_GET_EVENTS,
)
from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.number import DOMAIN as NUMBER_DOMAIN
from homeassistant.components.schedule import (
    ATTR_NEXT_EVENT as SCHEDULE_ATTR_NEXT_EVENT,
    DOMAIN as SCHEDULE_DOMAIN,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_FRIENDLY_NAME,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
    split_entity_id,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util, slugify

from .const import (
    ATTR_ACTIVE,
    ATTR_CALENDAR,
    ATTR_CALENDAR_ACTIVE,
    ATTR_CALENDAR_END_TIME,
    ATTR_CALENDAR_NEXT,
    ATTR_CALENDAR_NEXT_START,
    ATTR_CALENDAR_NEXT_SUMMARY,
    ATTR_CALENDAR_START_TIME,
    ATTR_CALENDAR_SUMMARY,
    ATTR_CODE,
    ATTR_CODE_LENGTH,
    ATTR_CODE_SLOT,
    ATTR_CONDITION_ENTITY,
    ATTR_CONDITION_ENTITY_DOMAIN,
    ATTR_CONDITION_ENTITY_ID,
    ATTR_CONDITION_ENTITY_NAME,
    ATTR_CONDITION_ENTITY_STATE,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_CONFIGURED_CODE,
    ATTR_CONFIGURED_CODE_LENGTH,
    ATTR_EVENT_ENTITY_ID,
    ATTR_IN_SYNC,
    ATTR_LAST_SYNCED,
    ATTR_LAST_USED,
    ATTR_LAST_USED_LOCK,
    ATTR_LOCK_ENTITY_ID,
    ATTR_LOCK_NAME,
    ATTR_MANAGED,
    ATTR_PIN_LENGTH,
    ATTR_SCHEDULE,
    ATTR_SCHEDULE_NEXT_EVENT,
    ATTR_SLOT,
    ATTR_SLOT_NUM,
    ATTR_USERCODE,
    CONDITION_ENTITY_DOMAINS,
    CONF_CONDITIONS,
    CONF_CONFIG_ENTRY,
    CONF_ENTITIES,
    CONF_LOCKS,
    CONF_NAME,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
    EVENT_PIN_USED,
)
from .data import get_entry_data
from .helpers import (
    async_clear_slot_condition,
    async_clear_usercode,
    async_set_slot_condition,
    async_set_usercode,
)
from .models import SlotCode, SlotEntityData, SlotEntityIds, SlotMetadata
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

ERR_NOT_LOADED = "not_loaded"

# Calendar entity state attributes (not exported by HA, defined in CalendarEntity.state_attributes)
CALENDAR_ATTR_MESSAGE = "message"
CALENDAR_ATTR_START_TIME = "start_time"
CALENDAR_ATTR_END_TIME = "end_time"


# =============================================================================
# State Helper Functions
# =============================================================================


def _get_text_state(hass: HomeAssistant, entity_id: str | None) -> str | None:
    """Get text state from an entity, returning None if unavailable."""
    if not entity_id:
        return None
    if state := hass.states.get(entity_id):
        if state.state and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return state.state
    return None


def _get_bool_state(hass: HomeAssistant, entity_id: str | None) -> bool | None:
    """Get boolean state from an entity, returning None if unavailable."""
    if not entity_id:
        return None
    if state := hass.states.get(entity_id):
        if state.state == STATE_ON:
            return True
        if state.state == STATE_OFF:
            return False
    return None


def _get_number_state(hass: HomeAssistant, entity_id: str | None) -> int | None:
    """Get integer state from a number entity, returning None if unavailable."""
    if not entity_id:
        return None
    if state := hass.states.get(entity_id):
        if state.state and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            try:
                return int(float(state.state))
            except (ValueError, TypeError):
                return None
    return None


def _get_last_changed(
    hass: HomeAssistant, entity_id: str | None, require_valid_state: bool = False
) -> str | None:
    """Get last_changed timestamp as ISO string."""
    if not entity_id:
        return None
    if state := hass.states.get(entity_id):
        if require_valid_state and state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        if state.last_changed:
            return state.last_changed.isoformat()
    return None


# =============================================================================
# Config Entry Helpers
# =============================================================================


def _find_config_entry_by_title(hass: HomeAssistant, title: str) -> ConfigEntry | None:
    """Find a config entry by title (slugified comparison)."""
    return next(
        (
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if slugify(entry.title) == slugify(title)
        ),
        None,
    )


def _get_slot_condition_entity_id(
    config_entry: ConfigEntry, slot_num: int
) -> str | None:
    """Get condition entity ID from slot config."""
    slots_data = get_entry_data(config_entry, CONF_SLOTS, {})
    slot_config = slots_data.get(slot_num) or slots_data.get(str(slot_num)) or {}
    if isinstance(slot_config, dict):
        return slot_config.get(CONF_ENTITY_ID)
    return None


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
            config_entry = _find_config_entry_by_title(hass, config_entry_title)
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
            if config_entry_title:
                error_msg = (
                    f"No lock code manager config entry with title "
                    f"`{config_entry_title}` found"
                )
            else:
                error_msg = (
                    f"No lock code manager config entry with ID "
                    f"`{config_entry_id}` found"
                )
            connection.send_error(
                msg["id"],
                websocket_api.const.ERR_NOT_FOUND,
                error_msg,
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
    websocket_api.async_register_command(hass, get_config_entry_data)
    websocket_api.async_register_command(hass, subscribe_lock_codes)
    websocket_api.async_register_command(hass, subscribe_code_slot)
    websocket_api.async_register_command(hass, ws_set_usercode)
    websocket_api.async_register_command(hass, ws_clear_usercode)
    websocket_api.async_register_command(hass, ws_set_slot_condition)
    websocket_api.async_register_command(hass, ws_clear_slot_condition)

    return True


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/get_config_entry_data",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
    }
)
@websocket_api.async_response
@async_get_entry
async def get_config_entry_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """
    Return complete config entry data for Lock Code Manager.

    This is the primary data-fetching command for the frontend. It returns all
    static configuration and entity registry data needed to render the dashboard.

    Frontend usage:
    - generate-view.ts: Fetches slot numbers for section generation, lock entity
      IDs for badges, and lock names for sorting/display
    - slot-section-strategy.ts: Fetches entities for legacy slot card generation
    - dashboard-strategy.ts: Fetches data for dashboard view generation
    - view-strategy.ts: Fetches config entry and entities for view rendering

    Sends:
        config_entry: The config entry JSON fragment (entry_id, title, etc.)
        entities: List of entity registry entries for this config entry
        locks: List of lock objects with entity_id and friendly name
        slots: Mapping of slot numbers to calendar entity IDs (or null)

    """
    all_locks = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})
    entry_lock_ids = get_entry_data(config_entry, CONF_LOCKS, [])

    connection.send_result(
        msg["id"],
        {
            CONF_CONFIG_ENTRY: config_entry.as_json_fragment,
            CONF_ENTITIES: [
                entity.as_partial_dict
                for entity in er.async_entries_for_config_entry(
                    er.async_get(hass), config_entry.entry_id
                )
            ],
            CONF_LOCKS: [
                {
                    ATTR_ENTITY_ID: lock_id,
                    CONF_NAME: _get_lock_friendly_name(hass, lock),
                }
                for lock_id, lock in all_locks.items()
                if lock_id in entry_lock_ids
            ],
            CONF_SLOTS: {
                k: v.get(CONF_ENTITY_ID)
                for k, v in get_entry_data(config_entry, CONF_SLOTS, {}).items()
            },
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
    code: str | SlotCode | None,
    *,
    reveal: bool,
    name: str | None = None,
    managed: bool | None = None,
    configured_code: str | None = None,
    active: bool | None = None,
    enabled: bool | None = None,
    config_entry_id: str | None = None,
) -> dict[str, Any]:
    """
    Serialize a single slot, optionally masking the code.

    - code/code_length: What's actually on the lock (actual state)
    - configured_code/configured_code_length: What LCM has configured (desired state)
      Always included for managed slots, even if code is active on lock.
    - active: True if enabled + conditions met, False if inactive, None if unknown
    - enabled: True if enabled switch is ON, False if OFF, None if unknown
    - config_entry_id: ID of the LCM config entry managing this slot (for navigation)
    """
    result: dict[str, Any] = {ATTR_SLOT: slot}
    if name:
        result[CONF_NAME] = name
    if managed is not None:
        result[ATTR_MANAGED] = managed
    if active is not None:
        result[ATTR_ACTIVE] = active
    if enabled is not None:
        result[CONF_ENABLED] = enabled
    if config_entry_id:
        result[ATTR_CONFIG_ENTRY_ID] = config_entry_id

    # Serialize code: SlotCode sentinels pass through as strings ("empty"/"unknown"),
    # regular codes are masked or revealed, None stays None.
    if isinstance(code, SlotCode):
        result[ATTR_CODE] = str(code)
    elif reveal or code is None:
        result[ATTR_CODE] = code
    else:
        # Masked: send code_length instead of actual code
        result[ATTR_CODE] = None
        result[ATTR_CODE_LENGTH] = len(code)

    # Configured code from LCM (desired state) - always include for managed slots
    if configured_code is not None:
        if reveal:
            result[ATTR_CONFIGURED_CODE] = configured_code
        else:
            result[ATTR_CONFIGURED_CODE_LENGTH] = len(configured_code)

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


def _get_slot_entity_ids(
    hass: HomeAssistant, lock_entity_id: str
) -> dict[int, SlotEntityIds]:
    """
    Get entity IDs for all slots managed by LCM for a lock.

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
                config_entry_id=entry.entry_id,
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
    """
    Get all slot metadata from LCM entities for a lock in one pass.

    Returns a dict mapping slot number to SlotMetadata containing:
    - name: From text entity
    - configured_pin: From text entity
    - active: From binary sensor (True=on, False=off, None=unknown)
    - enabled: From switch (True=on, False=off, None=unknown)
    """
    slot_entities = _get_slot_entity_ids(hass, lock_entity_id)
    return {
        slot_num: SlotMetadata(
            name=_get_text_state(hass, ids.name),
            configured_pin=_get_text_state(hass, ids.pin),
            active=_get_bool_state(hass, ids.active),
            enabled=_get_bool_state(hass, ids.enabled),
        )
        for slot_num, ids in slot_entities.items()
    }


def _get_slot_state_entity_ids(hass: HomeAssistant, lock_entity_id: str) -> list[str]:
    """
    Get entity IDs for slot state tracking (enabled, active, name, PIN).

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
        if friendly_name := state.attributes.get(ATTR_FRIENDLY_NAME):
            return friendly_name
    # Fall back to entity registry name/original_name
    return lock.display_name


def _serialize_lock_coordinator(
    hass: HomeAssistant, lock: BaseLock, *, reveal: bool = False
) -> dict[str, Any]:
    """Serialize coordinator data for a lock."""
    coordinator = lock.coordinator
    data = coordinator.data if coordinator is not None else {}
    managed_slots = _get_managed_slots(hass, lock.lock.entity_id)
    slot_metadata = _get_slot_metadata(hass, lock.lock.entity_id)
    slot_entity_ids = _get_slot_entity_ids(hass, lock.lock.entity_id)

    def _get_metadata(slot: Any) -> SlotMetadata | None:
        if str(slot).isdigit():
            return slot_metadata.get(int(slot))
        return None

    def _get_config_entry_id(slot: Any) -> str | None:
        if str(slot).isdigit():
            slot_ids = slot_entity_ids.get(int(slot))
            return slot_ids.config_entry_id if slot_ids else None
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
                config_entry_id=_get_config_entry_id(slot),
            )
        )

    return {
        ATTR_LOCK_ENTITY_ID: lock.lock.entity_id,
        ATTR_LOCK_NAME: _get_lock_friendly_name(hass, lock),
        CONF_SLOTS: slots,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/subscribe_lock_codes",
        vol.Required(ATTR_LOCK_ENTITY_ID): str,
        vol.Optional("reveal", default=False): bool,
    }
)
@websocket_api.async_response
async def subscribe_lock_codes(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """
    Subscribe to coordinator data and LCM entity state updates for a lock.

    Triggers updates when:
    - Lock coordinator data changes (codes on lock)
    - LCM entity states change (enabled, active, name, configured PIN)
    """
    lock_entity_id = msg[ATTR_LOCK_ENTITY_ID]
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

    # Mutable container for the current state tracking unsubscribe callback,
    # allowing it to be replaced when tracked entities change
    unsub_state_ref: list[Callable[[], None]] = []
    tracked_set: set[str] = set()

    @callback
    def _refresh_lock_state_tracking() -> None:
        """Re-subscribe to state changes if the tracked entity set has changed."""
        new_ids = set(_get_slot_state_entity_ids(hass, lock_entity_id))
        if new_ids == tracked_set:
            return
        tracked_set.clear()
        tracked_set.update(new_ids)
        # Unsubscribe from previous tracking
        if unsub_state_ref:
            unsub_state_ref[0]()
            unsub_state_ref.clear()
        # Subscribe to new set
        if new_ids:
            unsub_state_ref.append(
                async_track_state_change_event(hass, list(new_ids), _on_state_change)
            )

    @callback
    def _send_update() -> None:
        connection.send_event(
            msg["id"], _serialize_lock_coordinator(hass, lock, reveal=reveal)
        )
        # Re-resolve tracked entities to pick up entities created after
        # subscription was established
        _refresh_lock_state_tracking()

    @callback
    def _on_state_change(event: Event[EventStateChangedData]) -> None:
        """Handle Lock Code Manager entity state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Only send update if actual state value changed or entity created/removed
        if old_state is None or new_state is None or old_state.state != new_state.state:
            _send_update()

    # Track coordinator updates (lock code changes).
    # Note: if coordinator is None AND the initial entity set is empty, nothing
    # drives re-resolution of entities. This is acceptable because coordinators
    # are always present for real lock providers; None only occurs in degenerate
    # test scenarios where no actual lock hardware is involved.
    unsub_coordinator = (
        coordinator.async_add_listener(_send_update) if coordinator else lambda: None
    )

    # Track Lock Code Manager entity state changes (enabled, active, name, PIN)
    slot_entity_ids = _get_slot_state_entity_ids(hass, lock_entity_id)
    tracked_set.update(slot_entity_ids)
    if slot_entity_ids:
        unsub_state_ref.append(
            async_track_state_change_event(hass, slot_entity_ids, _on_state_change)
        )

    def _unsub_all() -> None:
        unsub_coordinator()
        if unsub_state_ref:
            unsub_state_ref[0]()

    connection.subscriptions[msg["id"]] = _unsub_all
    connection.send_result(msg["id"])
    _send_update()


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
        event_entity_id=_get_entity_id(EVENT_DOMAIN, EVENT_PIN_USED),
    )


def _get_slot_in_sync_entity_ids(
    hass: HomeAssistant, config_entry: ConfigEntry, slot_num: int
) -> dict[str, str]:
    """
    Get in_sync entity IDs for each lock for a specific slot.

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


def _get_condition_entity_data(
    hass: HomeAssistant, condition_entity_id: str | None
) -> dict[str, Any] | None:
    """
    Get condition entity data from entity state.

    Returns dict with entity info and domain-specific data.
    For calendar entities, includes event details (summary, end_time).
    For other entities, includes basic state info.
    """
    if not condition_entity_id:
        return None

    state = hass.states.get(condition_entity_id)
    if not state:
        return None

    # Extract domain from entity_id
    domain = split_entity_id(condition_entity_id)[0]
    is_active = state.state == STATE_ON

    result: dict[str, Any] = {
        ATTR_CONDITION_ENTITY_ID: condition_entity_id,
        ATTR_CONDITION_ENTITY_DOMAIN: domain,
        ATTR_CONDITION_ENTITY_STATE: state.state,
    }

    # Add friendly name if available
    if friendly_name := state.attributes.get(ATTR_FRIENDLY_NAME):
        result[ATTR_CONDITION_ENTITY_NAME] = friendly_name

    # For calendar entities, include rich event data
    if domain == CALENDAR_DOMAIN:
        result[ATTR_CALENDAR] = {ATTR_CALENDAR_ACTIVE: is_active}
        if is_active:
            if summary := state.attributes.get(CALENDAR_ATTR_MESSAGE):
                result[ATTR_CALENDAR][ATTR_CALENDAR_SUMMARY] = summary
            if start_time := state.attributes.get(CALENDAR_ATTR_START_TIME):
                result[ATTR_CALENDAR][ATTR_CALENDAR_START_TIME] = start_time
            if end_time := state.attributes.get(CALENDAR_ATTR_END_TIME):
                result[ATTR_CALENDAR][ATTR_CALENDAR_END_TIME] = end_time

    # For schedule entities, include next_event timing info
    elif domain == SCHEDULE_DOMAIN:
        schedule_data: dict[str, Any] = {}
        if next_event := state.attributes.get(SCHEDULE_ATTR_NEXT_EVENT):
            # Convert datetime to ISO string if needed
            if hasattr(next_event, "isoformat"):
                schedule_data[ATTR_SCHEDULE_NEXT_EVENT] = next_event.isoformat()
            else:
                schedule_data[ATTR_SCHEDULE_NEXT_EVENT] = str(next_event)
        if schedule_data:
            result[ATTR_SCHEDULE] = schedule_data

    return result


def _get_last_used_info(
    hass: HomeAssistant, event_entity_id: str | None
) -> tuple[str | None, str | None]:
    """
    Get last-used timestamp and lock name from an event entity.

    Returns a tuple of (last_used_timestamp, last_used_lock_name).
    The event entity's state is the ISO timestamp of the last event,
    and its event_type attribute is the lock entity ID where the PIN was used.
    """
    if not event_entity_id:
        return None, None
    event_state = hass.states.get(event_entity_id)
    if not event_state or event_state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None, None
    last_used = event_state.state
    last_used_lock_name: str | None = None
    if last_used_lock_id := event_state.attributes.get("event_type"):
        if lock_state := hass.states.get(last_used_lock_id):
            last_used_lock_name = lock_state.attributes.get(
                ATTR_FRIENDLY_NAME, last_used_lock_id
            )
    return last_used, last_used_lock_name


def _build_lock_status(
    hass: HomeAssistant,
    lock: BaseLock,
    slot_num: int,
    in_sync_map: dict[str, str],
    *,
    reveal: bool,
) -> dict[str, Any]:
    """
    Build the per-lock status dict for a slot card.

    Includes lock name, in-sync state, last synced time, and the code on the lock
    (masked or revealed depending on the reveal flag).
    """
    lock_entity_id = lock.lock.entity_id
    lock_name = _get_lock_friendly_name(hass, lock)
    in_sync_entity_id = in_sync_map.get(lock_entity_id)
    in_sync = _get_bool_state(hass, in_sync_entity_id)
    last_synced = _get_last_changed(hass, in_sync_entity_id)

    # Get code from coordinator — SlotCode sentinels pass through as strings
    coordinator = lock.coordinator
    code_on_lock: str | None = None
    code_length: int | None = None
    if coordinator and coordinator.data:
        raw_code = coordinator.data.get(slot_num)
        if isinstance(raw_code, SlotCode):
            code_on_lock = str(raw_code)
        elif raw_code is not None:
            if reveal:
                code_on_lock = raw_code
            else:
                code_length = len(raw_code)

    lock_status: dict[str, Any] = {
        ATTR_ENTITY_ID: lock_entity_id,
        CONF_NAME: lock_name,
        ATTR_IN_SYNC: in_sync,
    }
    if last_synced:
        lock_status[ATTR_LAST_SYNCED] = last_synced
    if code_on_lock is not None:
        lock_status[ATTR_CODE] = code_on_lock
    elif code_length is not None:
        lock_status[ATTR_CODE] = None
        lock_status[ATTR_CODE_LENGTH] = code_length
    else:
        lock_status[ATTR_CODE] = None

    return lock_status


async def _get_next_calendar_event(
    hass: HomeAssistant, calendar_entity_id: str
) -> dict[str, Any] | None:
    """
    Fetch the next upcoming event from a calendar entity.

    Returns dict with start_time and summary, or None if no upcoming events.
    """
    try:
        # Call the calendar.get_events service
        now = dt_util.now()
        end = now + timedelta(days=7)  # Look ahead 7 days

        result = await hass.services.async_call(
            CALENDAR_DOMAIN,
            SERVICE_GET_EVENTS,
            {
                ATTR_ENTITY_ID: calendar_entity_id,
                "start_date_time": now.isoformat(),
                "end_date_time": end.isoformat(),
            },
            blocking=True,
            return_response=True,
        )

        if not result or calendar_entity_id not in result:
            return None

        events = result[calendar_entity_id].get("events", [])
        if not events:
            return None

        # Return the first event (soonest)
        first_event = events[0]
        next_event_data: dict[str, Any] = {}

        if start := first_event.get("start"):
            next_event_data[ATTR_CALENDAR_NEXT_START] = start
        if summary := first_event.get("summary"):
            next_event_data[ATTR_CALENDAR_NEXT_SUMMARY] = summary

        return next_event_data if next_event_data else None

    except Exception:  # noqa: BLE001 - Catch-all: calendar fetch is best-effort
        _LOGGER.debug("Failed to fetch next calendar event for %s", calendar_entity_id)
        return None


def _serialize_slot_card_data(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    slot_entities: SlotEntityData,
    in_sync_map: dict[str, str],
    *,
    reveal: bool,
    calendar_next_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize slot data for the slot card."""
    # Get slot metadata using module-level helpers
    name = _get_text_state(hass, slot_entities.name_entity_id) or ""
    pin = _get_text_state(hass, slot_entities.pin_entity_id)
    enabled = _get_bool_state(hass, slot_entities.enabled_entity_id)
    active = _get_bool_state(hass, slot_entities.active_entity_id)
    number_of_uses = _get_number_state(hass, slot_entities.number_of_uses_entity_id)

    last_used, last_used_lock_name = _get_last_used_info(
        hass, slot_entities.event_entity_id
    )

    # Get condition entity from config using helper
    condition_entity_id = _get_slot_condition_entity_id(config_entry, slot_num)

    # Build per-lock status
    all_locks = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})
    entry_lock_ids = get_entry_data(config_entry, CONF_LOCKS, [])

    locks_data: list[dict[str, Any]] = [
        _build_lock_status(
            hass,
            lock,
            slot_num,
            in_sync_map,
            reveal=reveal,
        )
        for lock_entity_id in entry_lock_ids
        if (lock := all_locks.get(lock_entity_id))
    ]

    # Build result
    result: dict[str, Any] = {
        ATTR_SLOT_NUM: slot_num,
        ATTR_CONFIG_ENTRY_ID: config_entry.entry_id,
        ATTR_CONFIG_ENTRY_TITLE: config_entry.title,
        CONF_NAME: name,
        CONF_ENABLED: enabled,
        ATTR_ACTIVE: active,
        CONF_ENTITIES: {
            ATTR_ACTIVE: slot_entities.active_entity_id,
            CONF_ENABLED: slot_entities.enabled_entity_id,
            CONF_NAME: slot_entities.name_entity_id,
            CONF_NUMBER_OF_USES: slot_entities.number_of_uses_entity_id,
            CONF_PIN: slot_entities.pin_entity_id,
        },
        CONF_LOCKS: locks_data,
        CONF_CONDITIONS: {},
    }

    # Event entity for navigation/history
    if slot_entities.event_entity_id:
        result[ATTR_EVENT_ENTITY_ID] = slot_entities.event_entity_id

    # Last used timestamp and lock name
    if last_used:
        result[ATTR_LAST_USED] = last_used
        if last_used_lock_name:
            result[ATTR_LAST_USED_LOCK] = last_used_lock_name

    # PIN (masked or revealed)
    if reveal:
        result[CONF_PIN] = pin
    elif pin:
        result[CONF_PIN] = None
        result[ATTR_PIN_LENGTH] = len(pin)
    else:
        result[CONF_PIN] = None

    # Conditions
    if number_of_uses is not None:
        result[CONF_CONDITIONS][CONF_NUMBER_OF_USES] = number_of_uses
    if condition_entity_id:
        # Include condition entity data (handles both calendar and non-calendar entities)
        if condition_data := _get_condition_entity_data(hass, condition_entity_id):
            result[CONF_CONDITIONS][ATTR_CONDITION_ENTITY] = condition_data
            # Add next calendar event if available (for inactive calendar conditions)
            if (
                calendar_next_event
                and condition_data.get(ATTR_CONDITION_ENTITY_DOMAIN) == CALENDAR_DOMAIN
                and condition_data.get(ATTR_CONDITION_ENTITY_STATE) != STATE_ON
            ):
                condition_data[ATTR_CALENDAR_NEXT] = calendar_next_event

    return result


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/subscribe_code_slot",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
        vol.Required(ATTR_SLOT): int,
        vol.Optional("reveal", default=False): bool,
    }
)
@websocket_api.async_response
@async_get_entry
async def subscribe_code_slot(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """
    Subscribe to slot data for a specific slot in a config entry.

    Provides real-time updates when:
    - Slot entities change (name, PIN, enabled, active, number_of_uses)
    - Lock coordinator data changes (codes on locks)
    - In-sync status changes
    - Calendar entity state changes (for event-based access control)
    """
    slot_num = msg[ATTR_SLOT]
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

    # Re-resolve entity IDs on each update to handle entities created after
    # subscription (for example, during initial config setup when entities may
    # not exist yet). These are lightweight entity registry lookups.
    def _resolve_entity_ids() -> tuple[SlotEntityData, dict[str, str], str | None]:
        """Resolve current entity IDs for this slot from the entity registry."""
        return (
            _get_slot_entity_data(hass, config_entry, slot_num),
            _get_slot_in_sync_entity_ids(hass, config_entry, slot_num),
            _get_slot_condition_entity_id(config_entry, slot_num),
        )

    # Initial resolution for state tracking setup
    slot_entities, in_sync_map, condition_entity_id = _resolve_entity_ids()

    # Fetch next calendar event if condition is a calendar
    calendar_next_event: dict[str, Any] | None = None
    if (
        condition_entity_id
        and split_entity_id(condition_entity_id)[0] == CALENDAR_DOMAIN
    ):
        calendar_next_event = await _get_next_calendar_event(hass, condition_entity_id)

    # Mutable container for the current state tracking unsubscribe callback,
    # allowing it to be replaced when tracked entities change
    unsub_state_ref: list[Callable[[], None]] = []

    @callback
    def _send_update(next_event: dict[str, Any] | None = None) -> None:
        # Re-resolve entity IDs each time to pick up entities created after
        # subscription was established
        current_entities, current_in_sync, current_condition = _resolve_entity_ids()
        connection.send_event(
            msg["id"],
            _serialize_slot_card_data(
                hass,
                config_entry,
                slot_num,
                current_entities,
                current_in_sync,
                reveal=reveal,
                calendar_next_event=next_event,
            ),
        )
        # Update state tracking if the set of tracked entities has changed
        _refresh_state_tracking(current_entities, current_in_sync, current_condition)

    async def _async_send_update_with_calendar() -> None:
        """Fetch next calendar event and send update (for state changes)."""
        next_event = None
        current_condition = _get_slot_condition_entity_id(config_entry, slot_num)
        if current_condition and current_condition.startswith(f"{CALENDAR_DOMAIN}."):
            next_event = await _get_next_calendar_event(hass, current_condition)
        _send_update(next_event)

    @callback
    def _on_state_change(event: Event[EventStateChangedData]) -> None:
        """Handle entity state changes."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")

        # Only send update if actual state value changed
        if old_state is None or new_state is None or old_state.state != new_state.state:
            # Re-resolve condition entity identifier for calendar check
            current_condition = _get_slot_condition_entity_id(config_entry, slot_num)
            # For calendar entities, fetch next event asynchronously
            if (
                event.data.get("entity_id") == current_condition
                and current_condition
                and split_entity_id(current_condition)[0] == CALENDAR_DOMAIN
            ):
                hass.async_create_task(_async_send_update_with_calendar())
            else:
                _send_update()

    # Keep track of which entity IDs are currently being tracked so we can
    # detect when the set changes and re-subscribe
    tracked_set: set[str] = set()

    @callback
    def _refresh_state_tracking(
        current_entities: SlotEntityData,
        current_in_sync: dict[str, str],
        current_condition: str | None = None,
    ) -> None:
        """Re-subscribe to state changes if the tracked entity set has changed."""
        new_ids = set(current_entities.all_entity_ids()) | set(current_in_sync.values())
        if current_condition:
            new_ids.add(current_condition)

        if new_ids == tracked_set:
            return

        tracked_set.clear()
        tracked_set.update(new_ids)
        # Unsubscribe from previous tracking
        if unsub_state_ref:
            unsub_state_ref[0]()
            unsub_state_ref.clear()
        # Subscribe to new set
        if new_ids:
            unsub_state_ref.append(
                async_track_state_change_event(hass, list(new_ids), _on_state_change)
            )

    # Initial state tracking setup (reuse _refresh_state_tracking to avoid duplication)
    _refresh_state_tracking(slot_entities, in_sync_map, condition_entity_id)

    # Track coordinator updates for all locks
    unsub_coordinators: list[Any] = []
    all_locks = hass.data.get(DOMAIN, {}).get(CONF_LOCKS, {})
    entry_lock_ids = get_entry_data(config_entry, CONF_LOCKS, [])

    for lock_entity_id in entry_lock_ids:
        lock = all_locks.get(lock_entity_id)
        if lock and lock.coordinator:
            unsub_coordinators.append(lock.coordinator.async_add_listener(_send_update))

    def _unsub_all() -> None:
        if unsub_state_ref:
            unsub_state_ref[0]()
        for unsub in unsub_coordinators:
            unsub()

    connection.subscriptions[msg["id"]] = _unsub_all
    connection.send_result(msg["id"])
    _send_update(calendar_next_event)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/set_usercode",
        vol.Required(ATTR_LOCK_ENTITY_ID): str,
        vol.Required(ATTR_CODE_SLOT): int,
        vol.Required(ATTR_USERCODE): str,
    }
)
@websocket_api.async_response
async def ws_set_usercode(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Set a usercode on a lock slot.

    This is intended for managing unmanaged slots directly on the lock.
    For LCM-managed slots, use the entity services instead.
    """
    try:
        await async_set_usercode(
            hass, msg[ATTR_LOCK_ENTITY_ID], msg[ATTR_CODE_SLOT], msg[ATTR_USERCODE]
        )
        connection.send_result(msg["id"], {"success": True})
    except ServiceValidationError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_NOT_FOUND, str(err))
    except Exception as err:  # noqa: BLE001 - WS handler must catch all to send error response
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNKNOWN_ERROR,
            str(err),
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/clear_usercode",
        vol.Required(ATTR_LOCK_ENTITY_ID): str,
        vol.Required(ATTR_CODE_SLOT): int,
    }
)
@websocket_api.async_response
async def ws_clear_usercode(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Clear a usercode from a lock slot.

    This is intended for managing unmanaged slots directly on the lock.
    For LCM-managed slots, use the entity services instead.
    """
    try:
        await async_clear_usercode(hass, msg[ATTR_LOCK_ENTITY_ID], msg[ATTR_CODE_SLOT])
        connection.send_result(msg["id"], {"success": True})
    except ServiceValidationError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_NOT_FOUND, str(err))
    except Exception as err:  # noqa: BLE001 - WS handler must catch all to send error response
        connection.send_error(
            msg["id"],
            websocket_api.const.ERR_UNKNOWN_ERROR,
            str(err),
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/set_slot_condition",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
        vol.Required(ATTR_SLOT): int,
        vol.Required(CONF_ENTITY_ID): cv.entity_domain(CONDITION_ENTITY_DOMAINS),
    }
)
@websocket_api.async_response
@async_get_entry
async def ws_set_slot_condition(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """Set a condition entity for a slot.

    The condition entity must exist in hass.states and must not belong to an
    excluded platform (for example, the scheduler integration).
    """
    try:
        await async_set_slot_condition(
            hass, config_entry.entry_id, msg[ATTR_SLOT], msg[CONF_ENTITY_ID]
        )
        connection.send_result(msg["id"], {"success": True})
    except ServiceValidationError as err:
        error_str = str(err)
        if "not found" in error_str.lower():
            code = websocket_api.const.ERR_NOT_FOUND
        elif "not supported" in error_str.lower():
            code = websocket_api.const.ERR_NOT_SUPPORTED
        else:
            code = websocket_api.const.ERR_UNKNOWN_ERROR
        connection.send_error(msg["id"], code, error_str)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "lock_code_manager/clear_slot_condition",
        vol.Exclusive("config_entry_title", "entry"): str,
        vol.Exclusive("config_entry_id", "entry"): str,
        vol.Required(ATTR_SLOT): int,
    }
)
@websocket_api.async_response
@async_get_entry
async def ws_clear_slot_condition(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
    config_entry: ConfigEntry,
) -> None:
    """Clear the condition entity from a slot."""
    try:
        await async_clear_slot_condition(hass, config_entry.entry_id, msg[ATTR_SLOT])
        connection.send_result(msg["id"], {"success": True})
    except ServiceValidationError as err:
        connection.send_error(msg["id"], websocket_api.const.ERR_NOT_FOUND, str(err))
