"""Lock Code Manager Integration."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import copy
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.lovelace.const import DOMAIN as LOVELACE_DOMAIN
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry, ConfigEntryError
from homeassistant.const import ATTR_ENTITY_ID, CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import Config, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    COORDINATORS,
    DOMAIN,
    PLATFORM_MAP,
    PLATFORMS,
    STRATEGY_FILENAME,
    STRATEGY_PATH,
    Platform,
)
from .coordinator import LockUsercodeUpdateCoordinator
from .helpers import async_create_lock_instance, get_lock_from_entity_id
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

SERVICE_HARD_REFRESH_USERCODES = "hard_refresh_usercodes"

ATTR_SETUP_TASKS = "setup_tasks"
ATTR_ENTITIES_ADDED_TRACKER = "entities_added_tracker"
ATTR_ENTITIES_REMOVED_TRACKER = "entities_removed_tracker"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Set up integration."""
    hass.data.setdefault(DOMAIN, {})

    resources: ResourceStorageCollection
    if resources := hass.data[LOVELACE_DOMAIN].get("resources"):
        hass.http.register_static_path(
            STRATEGY_PATH, Path(__file__).parent / "www" / STRATEGY_FILENAME
        )
        data = await resources.async_create_item(
            {"res_type": "module", "url": STRATEGY_PATH}
        )
        _LOGGER.debug("Registered strategy module (resource ID %s)", data["id"])

    # Hard refresh usercodes
    async def _hard_refresh_usercodes(service: ServiceCall) -> None:
        """Hard refresh all usercodes."""
        _LOGGER.debug("Hard refresh usercodes service called: %s", service.data)
        lock = get_lock_from_entity_id(hass, service.data[ATTR_ENTITY_ID])
        try:
            await lock.async_hard_refresh_codes()
        except Exception as err:
            if not isinstance(err, HomeAssistantError):
                raise HomeAssistantError from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        _hard_refresh_usercodes,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}),
    )

    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up is called when Home Assistant is loading our component."""
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    if entity_id := next(
        (
            entity_id
            for entity_id in hass.config_entries.async_get_entry(entry_id).data.get(
                CONF_LOCKS, {}
            )
            if not ent_reg.async_get(entity_id)
        ),
        None,
    ):
        raise ConfigEntryError(
            f"Unable to start because lock {entity_id} can't be found"
        )

    hass.data[DOMAIN][entry_id] = {
        CONF_LOCKS: {},
        COORDINATORS: {},
        ATTR_SETUP_TASKS: {},
        ATTR_ENTITIES_ADDED_TRACKER: defaultdict(dict),
        ATTR_ENTITIES_REMOVED_TRACKER: defaultdict(dict),
    }

    config_entry.async_create_task(
        hass,
        hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS),
        "setup_platforms",
    )
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_update_listener)
    )

    if config_entry.data:
        # Move data from data to options so update listener can work
        hass.config_entries.async_update_entry(
            config_entry, data={}, options={**config_entry.data}
        )
    else:
        await async_update_listener(hass, config_entry)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    entry_id = config_entry.entry_id
    locks: BaseLock = hass.data[DOMAIN][entry_id][CONF_LOCKS].values()
    await asyncio.gather(*[lock.async_unload() for lock in locks])

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry,
        [
            *PLATFORMS,
            *hass.data[DOMAIN][entry_id][ATTR_SETUP_TASKS].keys(),
        ],
    )

    if unload_ok:
        hass.data[DOMAIN].pop(entry_id, None)

    if not hass.data[DOMAIN]:
        resources: ResourceStorageCollection
        if resources := hass.data[LOVELACE_DOMAIN].get("resources"):
            try:
                resource_id = next(
                    id
                    for id, data in resources.data.items()
                    if data["url"] == STRATEGY_PATH
                )
            except StopIteration:
                pass
            else:
                await resources.async_delete_item(resource_id)
                _LOGGER.debug("Deleted strategy module (resource ID %s)", resource_id)

    return unload_ok


async def async_update_listener(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Update listener."""
    # No need to update if the options match the data
    if not config_entry.options:
        return

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    _LOGGER.info("%s (%s): Creating and/or updating entities", entry_id, entry_title)

    setup_tasks: dict[str | Platform, asyncio.Task] = hass.data[DOMAIN][entry_id][
        ATTR_SETUP_TASKS
    ]

    curr_slots: dict[int, Any] = copy.deepcopy(config_entry.data.get(CONF_SLOTS, {}))
    new_slots: dict[int, Any] = copy.deepcopy(config_entry.options[CONF_SLOTS])
    curr_locks: list[str] = copy.deepcopy(config_entry.data.get(CONF_LOCKS, []))
    new_locks: list[str] = copy.deepcopy(config_entry.options[CONF_LOCKS])

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
        for slot_num in curr_slots:
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_remove_lock", lock_entity_id
            )
        hass.data[DOMAIN][entry_id][CONF_LOCKS].pop(lock_entity_id)
        hass.data[DOMAIN][entry_id][COORDINATORS].pop(lock_entity_id)

    # Notify any existing entities that additional locks have been added then create
    # slot PIN sensors for the new locks
    _LOGGER.debug(
        "%s (%s): Adding following locks: %s",
        entry_id,
        entry_title,
        locks_to_add,
    )
    async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add_locks", locks_to_add)
    for lock_entity_id in locks_to_add:
        lock = hass.data[DOMAIN][entry_id][CONF_LOCKS][
            lock_entity_id
        ] = async_create_lock_instance(
            hass,
            dr.async_get(hass),
            er.async_get(hass),
            config_entry,
            lock_entity_id,
        )
        await lock.async_setup()

        # Make sure lock is up before we proceed
        timeout = 1

        while not await lock.async_is_connection_up():
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

        _LOGGER.debug(
            "%s (%s): Creating coordinator for lock %s", entry_id, entry_title, lock
        )
        coordinator = hass.data[DOMAIN][entry_id][COORDINATORS][
            lock_entity_id
        ] = LockUsercodeUpdateCoordinator(hass, lock)
        await coordinator.async_config_entry_first_refresh()
        for slot_num in new_slots:
            _LOGGER.debug(
                "%s (%s): Adding lock %s slot %s sensor",
                entry_id,
                entry_title,
                lock_entity_id,
                slot_num,
            )
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_lock_slot_sensor", lock, slot_num
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
        entities_to_add: dict[str, str] = hass.data[DOMAIN][entry_id][
            ATTR_ENTITIES_ADDED_TRACKER
        ][slot_num]
        # First we store the set of entities we are adding so we can track when they are
        # done
        entities_to_add.update({CONF_ENABLED: True, CONF_NAME: True, CONF_PIN: True})
        for lock_entity_id, lock in hass.data[DOMAIN][entry_id][CONF_LOCKS].items():
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
                hass, f"{DOMAIN}_{entry_id}_add_lock_slot_sensor", lock, slot_num
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
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add", slot_num)
        for key in entities_to_add:
            _LOGGER.debug(
                "%s (%s): Adding %s entity for slot %s",
                entry_id,
                entry_title,
                key,
                slot_num,
            )
            async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add_{key}", slot_num)

    # For all slots that are in both the old and new config, check if any of the
    # configuration options have changed
    for slot_num in {slot for slot in curr_slots if slot in new_slots}:
        entities_to_remove: dict[str, str] = hass.data[DOMAIN][entry_id][
            ATTR_ENTITIES_REMOVED_TRACKER
        ][slot_num]
        entities_to_add: dict[str, str] = hass.data[DOMAIN][entry_id][
            ATTR_ENTITIES_ADDED_TRACKER
        ][slot_num]
        # Check if number of uses has changed
        old_val = curr_slots[slot_num].get(CONF_NUMBER_OF_USES)
        new_val = new_slots[slot_num].get(CONF_NUMBER_OF_USES)

        # If number of uses value hasn't changed, skip
        if old_val == new_val:
            continue
        # If number of uses value has been removed, fire a signal to remove
        # corresponding entity
        elif old_val not in (None, "") and new_val in (None, ""):
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
            async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add_{key}", slot_num)

    # Existing entities will listen to updates and act on it
    new_data = {CONF_LOCKS: new_locks, CONF_SLOTS: new_slots}
    _LOGGER.info(
        "%s (%s): Done creating and/or updating entities", entry_id, entry_title
    )
    hass.config_entries.async_update_entry(config_entry, data=new_data, options={})
