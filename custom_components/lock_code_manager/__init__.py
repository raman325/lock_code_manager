"""Lock Code Manager Integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    DOMAIN as LL_DOMAIN,
)
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_ID,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import (
    CoreState,
    Event,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.core_config import Config
from homeassistant.exceptions import (
    ConfigEntryError,
    HomeAssistantError,
)
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from .const import (
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
    EVENT_PIN_USED,
    PLATFORM_MAP,
    PLATFORMS,
    SERVICE_HARD_REFRESH_USERCODES,
    STRATEGY_FILENAME,
    STRATEGY_PATH,
    Platform,
)
from .data import (
    LockCodeManagerConfigEntry,
    LockCodeManagerConfigEntryData,
    get_entry_data,
)
from .helpers import async_create_lock_instance, get_locks_from_targets
from .providers import BaseLock
from .websocket import async_setup as async_websocket_setup

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> bool:
    """Migrate old entry data to new format."""
    if config_entry.version == 1:
        _LOGGER.debug(
            "%s (%s): Migrating from version 1 to 2",
            config_entry.entry_id,
            config_entry.title,
        )

        # Migrate CONF_CALENDAR to CONF_ENTITY_ID in slot configs
        new_data = {**config_entry.data}
        new_options = {**config_entry.options}

        for data_dict in (new_data, new_options):
            if CONF_SLOTS in data_dict:
                new_slots = {}
                for slot_num, slot_config in data_dict[CONF_SLOTS].items():
                    new_slot = {**slot_config}
                    # Migrate calendar to entity_id if not already set
                    if CONF_CALENDAR in new_slot and CONF_ENTITY_ID not in new_slot:
                        new_slot[CONF_ENTITY_ID] = new_slot.pop(CONF_CALENDAR)
                    elif CONF_CALENDAR in new_slot:
                        # Remove calendar if entity_id is already set
                        new_slot.pop(CONF_CALENDAR)
                    new_slots[slot_num] = new_slot
                data_dict[CONF_SLOTS] = new_slots

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=2
        )
        _LOGGER.info(
            "%s (%s): Migration to version 2 complete",
            config_entry.entry_id,
            config_entry.title,
        )

    return True


def _get_lovelace_resources(
    hass: HomeAssistant,
) -> ResourceStorageCollection | ResourceYAMLCollection | None:
    """Return the Lovelace resource collection if available."""
    if lovelace_data := hass.data.get(LL_DOMAIN):
        return lovelace_data.resources
    return None


async def _async_register_strategy_resource(hass: HomeAssistant) -> None:
    """Register the Lovelace strategy resource when supported."""
    resources = _get_lovelace_resources(hass)
    if not resources:
        return

    if not resources.loaded:
        await resources.async_load()
        _LOGGER.debug("Manually loaded resources")
        resources.loaded = True

    # Check if resource already exists (YAML resources don't have CONF_ID)
    resource_exists = any(
        data[CONF_URL] == STRATEGY_PATH for data in resources.async_items()
    )

    if isinstance(resources, ResourceYAMLCollection):
        if resource_exists:
            _LOGGER.debug("Strategy module already in YAML resources")
        else:
            _LOGGER.warning(
                "Strategy module can't automatically be registered because this "
                "Home Assistant instance is running in YAML mode for resources. "
                "Please add a new entry in the list under the resources key in "
                'the lovelace section of your config as follows:\n  - url: "%s"'
                "\n    type: module",
                STRATEGY_PATH,
            )
        return

    if resource_exists:
        res_id = next(
            data[CONF_ID]
            for data in resources.async_items()
            if data[CONF_URL] == STRATEGY_PATH
        )
        _LOGGER.debug("Strategy module already registered with resource ID %s", res_id)
        return

    data = await resources.async_create_item(
        {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: STRATEGY_PATH}
    )
    _LOGGER.debug("Registered strategy module (resource ID %s)", data[CONF_ID])
    hass.data[DOMAIN]["resources"] = True


async def _async_cleanup_strategy_resource(
    hass: HomeAssistant, hass_data: dict[str, Any]
) -> None:
    """Remove the Lovelace strategy resource if we registered it."""
    resources = _get_lovelace_resources(hass)
    if not resources:
        return

    if isinstance(resources, ResourceYAMLCollection) and hass_data["resources"]:
        _LOGGER.debug(
            "Resources switched to YAML mode after registration, "
            "skipping automatic removal for %s",
            STRATEGY_PATH,
        )
        return

    if not hass_data["resources"]:
        _LOGGER.debug("Strategy module not automatically registered, skipping removal")
        return

    try:
        resource_id = next(
            data[CONF_ID]
            for data in resources.async_items()
            if data[CONF_URL] == STRATEGY_PATH
        )
    except StopIteration:
        _LOGGER.debug("Strategy module not found so there is nothing to remove")
        return

    await resources.async_delete_item(resource_id)
    _LOGGER.debug("Removed strategy module (resource ID %s)", resource_id)


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Set up integration."""
    hass.data.setdefault(DOMAIN, {CONF_LOCKS: {}, "resources": False})
    # Expose strategy javascript
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STRATEGY_PATH, Path(__file__).parent / "www" / STRATEGY_FILENAME, False
            )
        ]
    )
    _LOGGER.debug("Exposed strategy module at %s", STRATEGY_PATH)

    await _async_register_strategy_resource(hass)

    # Set up websocket API
    await async_websocket_setup(hass)
    _LOGGER.debug("Finished setting up websocket API")

    # Hard refresh usercodes
    async def _hard_refresh_usercodes(service: ServiceCall) -> None:
        """Hard refresh all usercodes."""
        _LOGGER.debug("Hard refresh usercodes service called: %s", service.data)
        locks = get_locks_from_targets(hass, service.data)
        results = await asyncio.gather(
            *(lock.async_internal_hard_refresh_codes() for lock in locks),
            return_exceptions=True,
        )
        errors = [err for err in results if isinstance(err, Exception)]
        if errors:
            errors_str = "\n".join(str(errors))
            raise HomeAssistantError(
                "The following errors occurred while processing this service "
                f"request:\n{errors_str}"
            )

    hass.services.async_register(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        _hard_refresh_usercodes,
        schema=vol.All(
            vol.Schema(
                {
                    vol.Optional(ATTR_AREA_ID): vol.All(cv.ensure_list, [cv.string]),
                    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
                    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
                }
            ),
            cv.has_at_least_one_key(ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID),
            cv.has_at_most_one_key(ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID),
        ),
    )

    return True


@callback
def _setup_entry_after_start(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    event: Event | None = None,
) -> None:
    """
    Set up config entry.

    Should only be run once Home Assistant has started.
    """
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_update_listener)
    )

    if config_entry.data:
        # Move data from data to options so update listener can work
        hass.config_entries.async_update_entry(
            config_entry, data={}, options={**config_entry.data}
        )
    else:
        hass.async_create_task(
            async_update_listener(hass, config_entry),
            f"Initial setup for entities for {config_entry.entry_id}",
        )


async def async_setup_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> bool:
    """Set up is called when Home Assistant is loading our component."""
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    try:
        entity_id = next(
            entity_id
            for entity_id in get_entry_data(config_entry, CONF_LOCKS, [])
            if not ent_reg.async_get(entity_id)
        )
    except StopIteration:
        pass
    else:
        config_entry.async_start_reauth(hass, context={"lock_entity_id": entity_id})
        raise ConfigEntryError(
            f"Unable to start because lock {entity_id} can't be found"
        )

    hass.data.setdefault(DOMAIN, {CONF_LOCKS: {}, "resources": False})
    await _async_register_strategy_resource(hass)
    config_entry.runtime_data = LockCodeManagerConfigEntryData()

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, entry_id)},
        manufacturer="Lock Code Manager",
        name=config_entry.title,
        serial_number=entry_id,
    )

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    if hass.state == CoreState.running:
        _setup_entry_after_start(hass, config_entry)
    else:
        # One-time listeners auto-remove when they fire, so unsubscribing
        # during unload may fail if the event already fired. Ignore that error.
        @callback
        def _on_started(event: Event) -> None:
            _setup_entry_after_start(hass, config_entry, event)

        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

        @callback
        def _safe_unsub() -> None:
            try:
                unsub()
            except ValueError:
                pass  # Listener already removed when event fired

        config_entry.async_on_unload(_safe_unsub)

    return True


async def async_unload_lock(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock_entity_id: str | None = None,
    remove_permanently: bool = False,
):
    """Unload lock."""
    hass_data = hass.data[DOMAIN]
    runtime_data = config_entry.runtime_data
    lock_entity_ids = (
        [lock_entity_id] if lock_entity_id else list(runtime_data.locks.keys())
    )
    for _lock_entity_id in lock_entity_ids:
        if not any(
            entry != config_entry
            and _lock_entity_id
            in entry.data.get(CONF_LOCKS, entry.options.get(CONF_LOCKS, ""))
            for entry in hass.config_entries.async_entries(
                DOMAIN, include_disabled=False, include_ignore=False
            )
        ):
            lock: BaseLock = hass_data[CONF_LOCKS].pop(_lock_entity_id)
            await lock.async_unload(remove_permanently)
            if lock.coordinator is not None:
                await lock.coordinator.async_shutdown()

        runtime_data.locks.pop(_lock_entity_id, None)


async def async_unload_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> bool:
    """Handle removal of an entry."""
    hass_data = hass.data[DOMAIN]
    runtime_data = config_entry.runtime_data

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry,
        {
            *PLATFORMS,
            *runtime_data.setup_tasks.keys(),
        },
    )

    if unload_ok:
        await async_unload_lock(hass, config_entry)

    if {k: v for k, v in hass_data.items() if k != "resources"} == {
        CONF_LOCKS: {},
    }:
        await _async_cleanup_strategy_resource(hass, hass_data)
        hass.data.pop(DOMAIN)

    return unload_ok


# ---------------------------------------------------------------------------
# Config update listener helpers
# ---------------------------------------------------------------------------


@dataclass
class _ConfigDiff:
    """Computed differences between old and new configuration."""

    curr_slots: dict[int, Any]
    new_slots: dict[int, Any]
    curr_locks: list[str]
    new_locks: list[str]
    slots_to_add: dict[int, Any]
    slots_to_remove: dict[int, Any]
    locks_to_add: list[str]
    locks_to_remove: list[str]


def _compute_config_diff(config_entry: LockCodeManagerConfigEntry) -> _ConfigDiff:
    """Compute differences between current and new configuration."""
    curr_slots: dict[int, Any] = {**config_entry.data.get(CONF_SLOTS, {})}
    new_slots: dict[int, Any] = {**config_entry.options.get(CONF_SLOTS, {})}
    curr_locks: list[str] = [*config_entry.data.get(CONF_LOCKS, [])]
    new_locks: list[str] = [*config_entry.options.get(CONF_LOCKS, [])]

    return _ConfigDiff(
        curr_slots=curr_slots,
        new_slots=new_slots,
        curr_locks=curr_locks,
        new_locks=new_locks,
        slots_to_add={k: v for k, v in new_slots.items() if k not in curr_slots},
        slots_to_remove={k: v for k, v in curr_slots.items() if k not in new_slots},
        locks_to_add=[lock for lock in new_locks if lock not in curr_locks],
        locks_to_remove=[lock for lock in curr_locks if lock not in new_locks],
    )


async def _setup_new_platforms(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    new_slots: dict[int, Any],
) -> None:
    """Set up any platforms that the new slot configs need."""
    setup_tasks = config_entry.runtime_data.setup_tasks
    for platform in {
        platform
        for slot_config in new_slots.values()
        for key, platform in PLATFORM_MAP.items()
        if key in slot_config
        and platform not in setup_tasks
        and platform != Platform.CALENDAR
    }:
        setup_tasks[platform] = config_entry.async_create_task(
            hass,
            hass.config_entries.async_forward_entry_setups(config_entry, [platform]),
            "setup_new_platforms",
        )
    await asyncio.gather(*setup_tasks.values())


async def _handle_locks_removed(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    locks_to_remove: list[str],
) -> None:
    """Handle removal of locks from config."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    callbacks = config_entry.runtime_data.callbacks

    for lock_entity_id in locks_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing lock %s entities", entry_id, entry_title, lock_entity_id
        )
        callbacks.invoke_lock_removed_handlers(lock_entity_id)
        lock: BaseLock = hass.data[DOMAIN][CONF_LOCKS][lock_entity_id]
        if lock.device_entry:
            dev_reg = dr.async_get(hass)
            dev_reg.async_update_device(
                lock.device_entry.id, remove_config_entry_id=entry_id
            )
        await async_unload_lock(
            hass, config_entry, lock_entity_id=lock_entity_id, remove_permanently=True
        )


async def _handle_locks_added(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    locks_to_add: list[str],
    new_slots: dict[int, Any],
    ent_reg: er.EntityRegistry,
) -> None:
    """Handle addition of new locks to config."""
    if not locks_to_add:
        return

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    hass_data = hass.data[DOMAIN]
    runtime_data = config_entry.runtime_data
    callbacks = runtime_data.callbacks

    _LOGGER.debug(
        "%s (%s): Adding following locks: %s", entry_id, entry_title, locks_to_add
    )
    added_locks: list[BaseLock] = []

    for lock_entity_id in locks_to_add:
        if lock_entity_id in hass_data[CONF_LOCKS]:
            _LOGGER.debug(
                "%s (%s): Reusing lock instance for lock %s",
                entry_id,
                entry_title,
                hass_data[CONF_LOCKS][lock_entity_id],
            )
            lock = runtime_data.locks[lock_entity_id] = hass_data[CONF_LOCKS][
                lock_entity_id
            ]
        else:
            lock = hass_data[CONF_LOCKS][lock_entity_id] = runtime_data.locks[
                lock_entity_id
            ] = async_create_lock_instance(
                hass,
                dr.async_get(hass),
                ent_reg,
                config_entry,
                lock_entity_id,
            )
            _LOGGER.debug(
                "%s (%s): Creating lock instance for lock %s",
                entry_id,
                entry_title,
                lock,
            )
            await lock.async_setup(config_entry)

        added_locks.append(lock)

        # Check if lock is connected (but don't wait - entity creation doesn't require it)
        if not await lock.async_internal_is_connection_up():
            _LOGGER.debug(
                "%s (%s): Lock %s is not connected yet. Entities will be created "
                "but will be unavailable until the lock comes online. This is normal "
                "during startup if Z-Wave JS is still initializing.",
                entry_id,
                entry_title,
                lock.lock.entity_id,
            )

        for slot_num in new_slots:
            _LOGGER.debug(
                "%s (%s): Adding lock %s slot %s sensor and event entity",
                entry_id,
                entry_title,
                lock_entity_id,
                slot_num,
            )
            callbacks.invoke_lock_slot_adders(lock, slot_num, ent_reg)

    # Notify existing entities about the new locks
    if added_locks:
        callbacks.invoke_lock_added_handlers(added_locks)


async def _handle_slots_removed(
    config_entry: LockCodeManagerConfigEntry,
    slots_to_remove: dict[int, Any],
) -> None:
    """Handle removal of slots from config."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    callbacks = config_entry.runtime_data.callbacks

    for slot_num in slots_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing slot %s sensors", entry_id, entry_title, slot_num
        )
        await callbacks.invoke_entity_removers_for_slot(slot_num)


def _handle_slots_added(
    config_entry: LockCodeManagerConfigEntry,
    slots_to_add: dict[int, Any],
    locks_to_add: list[str],
    ent_reg: er.EntityRegistry,
) -> None:
    """Handle addition of new slots to config."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    runtime_data = config_entry.runtime_data
    callbacks = runtime_data.callbacks

    for slot_num, slot_config in slots_to_add.items():
        entities_to_add = {CONF_ENABLED, CONF_NAME, CONF_PIN, EVENT_PIN_USED}

        # Check if we need to add a number of uses entity
        if slot_config.get(CONF_NUMBER_OF_USES) not in (None, ""):
            entities_to_add.add(CONF_NUMBER_OF_USES)

        _LOGGER.debug(
            "%s (%s): Adding PIN enabled binary sensor for slot %s",
            entry_id,
            entry_title,
            slot_num,
        )
        callbacks.invoke_standard_adders(slot_num, ent_reg)

        for key in entities_to_add:
            _LOGGER.debug(
                "%s (%s): Adding %s entity for slot %s",
                entry_id,
                entry_title,
                key,
                slot_num,
            )
            if key in callbacks.add_keyed_entity:
                callbacks.invoke_keyed_adders(key, slot_num, ent_reg)

        # Add slot sensors for existing locks only (new locks already set up above)
        for lock_entity_id, lock in runtime_data.locks.items():
            if lock_entity_id in locks_to_add:
                continue
            _LOGGER.debug(
                "%s (%s): Adding lock %s slot %s sensor",
                entry_id,
                entry_title,
                lock_entity_id,
                slot_num,
            )
            callbacks.invoke_lock_slot_adders(lock, slot_num, ent_reg)


async def _handle_slots_modified(
    config_entry: LockCodeManagerConfigEntry,
    curr_slots: dict[int, Any],
    new_slots: dict[int, Any],
    ent_reg: er.EntityRegistry,
) -> None:
    """Handle modification of existing slots (number_of_uses changes)."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    callbacks = config_entry.runtime_data.callbacks

    for slot_num in set(curr_slots).intersection(new_slots):
        old_val = curr_slots[slot_num].get(CONF_NUMBER_OF_USES)
        new_val = new_slots[slot_num].get(CONF_NUMBER_OF_USES)

        if old_val == new_val:
            continue

        # Number of uses removed
        if old_val not in (None, "") and new_val in (None, ""):
            _LOGGER.debug(
                "%s (%s): Removing %s entity for slot %s due to changed configuration",
                entry_id,
                entry_title,
                CONF_NUMBER_OF_USES,
                slot_num,
            )
            await callbacks.invoke_entity_removers_for_key(
                slot_num, CONF_NUMBER_OF_USES
            )
        # Number of uses added
        elif old_val in (None, "") and new_val not in (None, ""):
            _LOGGER.debug(
                "%s (%s): Adding %s entity for slot %s due to changed configuration",
                entry_id,
                entry_title,
                CONF_NUMBER_OF_USES,
                slot_num,
            )
            callbacks.invoke_keyed_adders(CONF_NUMBER_OF_USES, slot_num, ent_reg)


async def async_update_listener(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """Handle config entry updates by computing diffs and applying changes."""
    # No need to update if there are no options (happens at the end of this function)
    if not config_entry.options:
        return

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    _LOGGER.info("%s (%s): Creating and/or updating entities", entry_id, entry_title)

    # Compute what changed
    diff = _compute_config_diff(config_entry)
    ent_reg = er.async_get(hass)

    # Set up any new platforms needed
    await _setup_new_platforms(hass, config_entry, diff.new_slots)

    # Process changes in order: removals first, then additions
    await _handle_locks_removed(hass, config_entry, diff.locks_to_remove)
    await _handle_locks_added(
        hass, config_entry, diff.locks_to_add, diff.new_slots, ent_reg
    )
    await _handle_slots_removed(config_entry, diff.slots_to_remove)
    _handle_slots_added(config_entry, diff.slots_to_add, diff.locks_to_add, ent_reg)
    await _handle_slots_modified(config_entry, diff.curr_slots, diff.new_slots, ent_reg)

    # Finalize: update config entry data and clear options
    new_data = {CONF_LOCKS: diff.new_locks, CONF_SLOTS: diff.new_slots}
    _LOGGER.info(
        "%s (%s): Done creating and/or updating entities", entry_id, entry_title
    )
    hass.config_entries.async_update_entry(config_entry, data=new_data, options={})
