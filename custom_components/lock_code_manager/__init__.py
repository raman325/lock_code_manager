"""Lock Code Manager Integration."""

from __future__ import annotations

import asyncio
import functools
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    DOMAIN as LL_DOMAIN,
)
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryError
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_ID,
    CONF_NAME,
    CONF_PIN,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
)
from homeassistant.core import (
    Config,
    CoreState,
    Event,
    HomeAssistant,
    ServiceCall,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ATTR_SETUP_TASKS,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    COORDINATORS,
    DOMAIN,
    EVENT_PIN_USED,
    PLATFORM_MAP,
    PLATFORMS,
    SERVICE_HARD_REFRESH_USERCODES,
    STRATEGY_FILENAME,
    STRATEGY_PATH,
    Platform,
)
from .coordinator import LockUsercodeUpdateCoordinator
from .data import get_entry_data
from .helpers import async_create_lock_instance, get_locks_from_targets
from .providers import BaseLock
from .websocket import async_setup as async_websocket_setup

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Set up integration."""
    hass.data.setdefault(DOMAIN, {CONF_LOCKS: {}, COORDINATORS: {}, "resources": False})
    # Expose strategy javascript
    hass.http.register_static_path(
        STRATEGY_PATH, Path(__file__).parent / "www" / STRATEGY_FILENAME
    )
    _LOGGER.debug("Exposed strategy module at %s", STRATEGY_PATH)

    resources: ResourceStorageCollection | ResourceYAMLCollection
    if resources := hass.data.get(LL_DOMAIN, {}).get("resources"):
        # Load resources if needed
        if not resources.loaded:
            await resources.async_load()
            _LOGGER.debug("Manually loaded resources")
            resources.loaded = True

        try:
            res_id = next(
                data.get(CONF_ID)
                for data in resources.async_items()
                if data[CONF_URL] == STRATEGY_PATH
            )
        except StopIteration:
            if isinstance(resources, ResourceYAMLCollection):
                _LOGGER.warning(
                    "Strategy module can't automatically be registered because this "
                    "Home Assistant instance is running in YAML mode for resources. "
                    "Please add a new entry in the list under the resources key in "
                    'the lovelace section of your config as follows:\n  - url: "%s"'
                    "\n    type: module",
                    STRATEGY_PATH,
                )
            else:
                # Register strategy module
                data = await resources.async_create_item(
                    {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: STRATEGY_PATH}
                )
                _LOGGER.debug(
                    "Registered strategy module (resource ID %s)", data[CONF_ID]
                )
                hass.data[DOMAIN]["resources"] = True
        else:
            _LOGGER.debug(
                "Strategy module already registered with resource ID %s", res_id
            )

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
    hass: HomeAssistant, config_entry: ConfigEntry, event: Event | None = None
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


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
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

    hass.data.setdefault(DOMAIN, {CONF_LOCKS: {}, COORDINATORS: {}, "resources": False})
    hass.data[DOMAIN][entry_id] = {
        CONF_LOCKS: {},
        COORDINATORS: {},
        ATTR_SETUP_TASKS: {},
    }

    dev_reg = dr.async_get(hass)
    dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, entry_id)},
        manufacturer="Lock Code Manager",
        name=config_entry.title,
        serial_number=entry_id,
    )

    config_entry.async_create_task(
        hass,
        hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS),
        "setup_platforms",
    )

    if hass.state == CoreState.running:
        _setup_entry_after_start(hass, config_entry)
    else:
        config_entry.async_on_unload(
            hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED,
                functools.partial(_setup_entry_after_start, hass, config_entry),
            )
        )

    return True


async def async_unload_lock(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lock_entity_id: str | None = None,
    remove_permanently: bool = False,
):
    """Unload lock."""
    hass_data = hass.data[DOMAIN]
    entry_id = config_entry.entry_id
    lock_entity_ids = (
        [lock_entity_id] if lock_entity_id else hass_data[entry_id][CONF_LOCKS].copy()
    )
    for lock_entity_id in lock_entity_ids:
        if not any(
            entry != config_entry
            and lock_entity_id
            in entry.data.get(CONF_LOCKS, entry.options.get(CONF_LOCKS, ""))
            for entry in hass.config_entries.async_entries(
                DOMAIN, include_disabled=False, include_ignore=False
            )
        ):
            lock: BaseLock = hass_data[CONF_LOCKS].pop(lock_entity_id)
            await lock.async_unload(remove_permanently)

        hass_data[entry_id][CONF_LOCKS].pop(lock_entity_id)

    for lock_entity_id in lock_entity_ids:
        if not any(
            entry != config_entry
            and lock_entity_id
            in entry.data.get(CONF_LOCKS, entry.options.get(CONF_LOCKS, ""))
            for entry in hass.config_entries.async_entries(
                DOMAIN, include_disabled=False, include_ignore=False
            )
        ):
            coordinator: LockUsercodeUpdateCoordinator = hass_data[COORDINATORS].pop(
                lock_entity_id
            )
            await coordinator.async_shutdown()

        hass_data[entry_id][COORDINATORS].pop(lock_entity_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    entry_id = config_entry.entry_id
    hass_data = hass.data[DOMAIN]

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry,
        {
            *PLATFORMS,
            *hass_data[entry_id][ATTR_SETUP_TASKS].keys(),
        },
    )

    if unload_ok:
        await async_unload_lock(hass, config_entry)
        hass_data.pop(entry_id, None)

    if {k: v for k, v in hass_data.items() if k != "resources"} == {
        CONF_LOCKS: {},
        COORDINATORS: {},
    }:
        resources: ResourceStorageCollection | ResourceYAMLCollection
        if resources := hass.data.get(LL_DOMAIN, {}).get("resources"):
            if hass_data["resources"]:
                try:
                    resource_id = next(
                        data[CONF_ID]
                        for data in resources.async_items()
                        if data[CONF_URL] == STRATEGY_PATH
                    )
                except StopIteration:
                    _LOGGER.debug(
                        "Strategy module not found so there is nothing to remove"
                    )
                else:
                    await resources.async_delete_item(resource_id)
                    _LOGGER.debug(
                        "Removed strategy module (resource ID %s)", resource_id
                    )
            else:
                _LOGGER.debug(
                    "Strategy module not automatically registered, skipping removal"
                )

        hass.data.pop(DOMAIN)

    return unload_ok


async def async_update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Update listener."""
    # No need to update if there are no options because that only happens at the end
    # of this function
    if not config_entry.options:
        return

    hass_data = hass.data[DOMAIN]
    ent_reg = er.async_get(hass)
    entities_to_remove: dict[str, bool] = {}
    entities_to_add: dict[str, bool] = {}

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    _LOGGER.info("%s (%s): Creating and/or updating entities", entry_id, entry_title)

    setup_tasks: dict[str | Platform, asyncio.Task] = hass_data[entry_id][
        ATTR_SETUP_TASKS
    ]

    curr_slots: dict[int, Any] = {**config_entry.data.get(CONF_SLOTS, {})}
    new_slots: dict[int, Any] = {**config_entry.options.get(CONF_SLOTS, {})}
    curr_locks: list[str] = [*config_entry.data.get(CONF_LOCKS, [])]
    new_locks: list[str] = [*config_entry.options.get(CONF_LOCKS, [])]

    # Set up any platforms that the new slot configs need that haven't already been
    # setup
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
            hass.config_entries.async_forward_entry_setup(config_entry, platform),
            "setup_new_platforms",
        )
    await asyncio.gather(*setup_tasks.values())

    # Identify changes that need to be made
    slots_to_add: dict[int, Any] = {
        k: v for k, v in new_slots.items() if k not in curr_slots
    }
    slots_to_remove: dict[int, Any] = {
        k: v for k, v in curr_slots.items() if k not in new_slots
    }
    locks_to_add: list[str] = [lock for lock in new_locks if lock not in curr_locks]
    locks_to_remove: list[str] = [lock for lock in curr_locks if lock not in new_locks]

    # Remove old lock entities (slot sensors)
    for lock_entity_id in locks_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing lock %s entities", entry_id, entry_title, lock_entity_id
        )
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_remove_lock", lock_entity_id)
        lock: BaseLock = hass.data[DOMAIN][CONF_LOCKS][lock_entity_id]
        if lock.device_entry:
            dev_reg = dr.async_get(hass)
            dev_reg.async_update_device(
                lock.device_entry.id, remove_config_entry_id=entry_id
            )
        await async_unload_lock(
            hass, config_entry, lock_entity_id=lock_entity_id, remove_permanently=True
        )

    # Notify any existing entities that additional locks have been added then create
    # slot PIN sensors for the new locks
    if locks_to_add:
        _LOGGER.debug(
            "%s (%s): Adding following locks: %s",
            entry_id,
            entry_title,
            locks_to_add,
        )
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add_locks", locks_to_add)
        for lock_entity_id in locks_to_add:
            if lock_entity_id in hass_data[CONF_LOCKS]:
                _LOGGER.debug(
                    "%s (%s): Reusing lock instance for lock %s",
                    entry_id,
                    entry_title,
                    hass_data[CONF_LOCKS][lock_entity_id],
                )
                lock = hass_data[entry_id][CONF_LOCKS][lock_entity_id] = hass_data[
                    CONF_LOCKS
                ][lock_entity_id]
            else:
                lock = hass_data[CONF_LOCKS][lock_entity_id] = hass.data[DOMAIN][
                    entry_id
                ][CONF_LOCKS][lock_entity_id] = async_create_lock_instance(
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
                await lock.async_setup()

            # Make sure lock is up before we proceed
            timeout = 1

            while not await lock.async_internal_is_connection_up():
                _LOGGER.debug(
                    (
                        "%s (%s): Lock %s is not connected to Home Assistant yet, waiting %s "
                        "seconds before retrying"
                    ),
                    entry_id,
                    entry_title,
                    lock.lock.entity_id,
                    timeout,
                )
                await asyncio.sleep(timeout)
                timeout = min(timeout * 2, 180)

            if lock_entity_id in hass_data[COORDINATORS]:
                _LOGGER.debug(
                    "%s (%s): Reusing coordinator for lock %s",
                    entry_id,
                    entry_title,
                    lock,
                )
                coordinator = hass_data[entry_id][COORDINATORS][lock_entity_id] = (
                    hass_data[COORDINATORS][lock_entity_id]
                )
            else:
                _LOGGER.debug(
                    "%s (%s): Creating coordinator for lock %s",
                    entry_id,
                    entry_title,
                    lock,
                )
                coordinator = hass_data[COORDINATORS][lock_entity_id] = hass_data[
                    entry_id
                ][COORDINATORS][lock_entity_id] = LockUsercodeUpdateCoordinator(
                    hass, lock
                )
                await coordinator.async_config_entry_first_refresh()
            for slot_num in new_slots:
                _LOGGER.debug(
                    "%s (%s): Adding lock %s slot %s sensor and event entity",
                    entry_id,
                    entry_title,
                    lock_entity_id,
                    slot_num,
                )
                async_dispatcher_send(
                    hass, f"{DOMAIN}_{entry_id}_add_lock_slot", lock, slot_num, ent_reg
                )

    # Remove slot sensors that are no longer in the config
    for slot_num in slots_to_remove.keys():
        _LOGGER.debug(
            "%s (%s): Removing slot %s sensors", entry_id, entry_title, slot_num
        )
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_remove_{slot_num}")

    # For each new slot, add standard entities and configuration entities. We also
    # add slot sensors for existing locks only since new locks were already set up
    # above.
    for slot_num, slot_config in slots_to_add.items():
        entities_to_remove.clear()
        # First we store the set of entities we are adding so we can track when they are
        # done
        entities_to_add = {
            CONF_ENABLED: True,
            CONF_NAME: True,
            CONF_PIN: True,
            EVENT_PIN_USED: True,
        }
        for lock_entity_id, lock in hass_data[entry_id][CONF_LOCKS].items():
            if lock_entity_id in locks_to_add:
                continue
            _LOGGER.debug(
                "%s (%s): Adding lock %s slot %s sensor",
                entry_id,
                entry_title,
                lock_entity_id,
                slot_num,
            )
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_lock_slot", lock, slot_num, ent_reg
            )

        # Check if we need to add a number of uses entity
        if slot_config.get(CONF_NUMBER_OF_USES) not in (None, ""):
            entities_to_add[CONF_NUMBER_OF_USES] = True

        _LOGGER.debug(
            "%s (%s): Adding PIN enabled binary sensor for slot %s",
            entry_id,
            entry_title,
            slot_num,
        )
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add", slot_num, ent_reg)
        for key in entities_to_add:
            _LOGGER.debug(
                "%s (%s): Adding %s entity for slot %s",
                entry_id,
                entry_title,
                key,
                slot_num,
            )
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_{key}", slot_num, ent_reg
            )

        for lock_entity_id, lock in hass_data[entry_id][CONF_LOCKS].items():
            if lock_entity_id in locks_to_add:
                continue
            _LOGGER.debug(
                "%s (%s): Adding lock %s slot %s sensor",
                entry_id,
                entry_title,
                lock_entity_id,
                slot_num,
            )
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_lock_slot", lock, slot_num, ent_reg
            )

    # For all slots that are in both the old and new config, check if any of the
    # configuration options have changed
    for slot_num in set(curr_slots).intersection(new_slots):
        entities_to_remove.clear()
        entities_to_add.clear()
        # Check if number of uses has changed
        old_val = curr_slots[slot_num].get(CONF_NUMBER_OF_USES)
        new_val = new_slots[slot_num].get(CONF_NUMBER_OF_USES)

        # If number of uses value hasn't changed, skip
        if old_val == new_val:
            continue

        # If number of uses value has been removed, fire a signal to remove
        # corresponding entity
        if old_val not in (None, "") and new_val in (None, ""):
            entities_to_remove[CONF_NUMBER_OF_USES] = True
        # If number of uses value has been added, fire a signal to add
        # corresponding entity
        elif old_val in (None, "") and new_val not in (None, ""):
            entities_to_add[CONF_NUMBER_OF_USES] = True

        for key in entities_to_remove:
            _LOGGER.debug(
                "%s (%s): Removing %s entity for slot %s due to changed configuration",
                entry_id,
                entry_title,
                key,
                slot_num,
            )
            async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_remove_{slot_num}_{key}")

        for key in entities_to_add:
            _LOGGER.debug(
                "%s (%s): Adding %s entity for slot %s due to changed configuration",
                entry_id,
                entry_title,
                key,
                slot_num,
            )
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_{key}", slot_num, ent_reg
            )

    # Existing entities will listen to updates and act on it
    new_data = {CONF_LOCKS: new_locks, CONF_SLOTS: new_slots}
    _LOGGER.info(
        "%s (%s): Done creating and/or updating entities", entry_id, entry_title
    )
    hass.config_entries.async_update_entry(config_entry, data=new_data, options={})
