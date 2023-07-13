"""Lock Code Manager Integration."""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, CONF_NAME
from homeassistant.core import Config, HomeAssistant, ServiceCall
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ATTR_CODE_SLOT,
    ATTR_USERCODE,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    COORDINATORS,
    DOMAIN,
    ISSUE_URL,
    PLATFORMS,
    VERSION,
    Platform,
)
from .exceptions import LockDisconnected
from .helpers import create_lock_instance, get_lock_from_entity_id
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)

SERVICE_GENERATE_LOVELACE = "generate_lovelace"
SERVICE_SET_USERCODE = "set_usercode"
SERVICE_CLEAR_USERCODE = "clear_usercode"
SERVICE_HARD_REFRESH_USERCODES = "hard_refresh_usercodes"

PLATFORM_MAP = {
    CONF_CALENDAR: Platform.CALENDAR,
    CONF_NUMBER_OF_USES: Platform.NUMBER,
}

UNSET_CONFIG_VALUES_MAP = {
    CONF_CALENDAR: False,
    CONF_NUMBER_OF_USES: -1,
}


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Disallow configuration via YAML."""
    _LOGGER.info(
        "Version %s is starting, if you have any issues please report them here: %s",
        VERSION,
        ISSUE_URL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up is called when Home Assistant is loading our component."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config_entry.entry_id] = {
        CONF_LOCKS: {},
        COORDINATORS: {},
        "setup_tasks": {},
    }

    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    )
    config_entry.async_on_unload(
        config_entry.add_update_listener(async_update_listener)
    )

    await async_update_listener(hass, config_entry, False)

    # Add code
    async def _set_usercode(service: ServiceCall) -> None:
        """Set a user code."""
        _LOGGER.debug("Set usercode service: %s", service)
        lock = get_lock_from_entity_id(hass, service.data[ATTR_ENTITY_ID])
        await lock.async_set_usercode(
            service.data[ATTR_CODE_SLOT],
            service.data[ATTR_USERCODE],
            service.data.get(CONF_NAME),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_USERCODE,
        _set_usercode,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ENTITY_ID): cv.entity_id,
                vol.Required(ATTR_CODE_SLOT): vol.Coerce(int),
                vol.Required(ATTR_USERCODE): cv.string,
                vol.Optional(CONF_NAME): cv.string,
            }
        ),
    )

    # Clear code
    async def _clear_usercode(service: ServiceCall) -> None:
        """Clear a user code."""
        _LOGGER.debug("Clear usercode service: %s", service)
        lock = get_lock_from_entity_id(hass, service.data[ATTR_ENTITY_ID])
        await lock.async_clear_usercode(service.data[ATTR_CODE_SLOT])

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_USERCODE,
        _clear_usercode,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ENTITY_ID): cv.entity_id,
                vol.Required(ATTR_CODE_SLOT): vol.Coerce(int),
            }
        ),
    )

    # Hard refresh usercodes
    async def _hard_refresh_usercodes(service: ServiceCall) -> None:
        """Hard refresh all usercodes."""
        _LOGGER.debug("Hard refresh usercodes service: %s", service)
        lock = get_lock_from_entity_id(hass, service.data[ATTR_ENTITY_ID])
        await lock.async_hard_refresh_codes()

    hass.services.async_register(
        DOMAIN,
        SERVICE_HARD_REFRESH_USERCODES,
        _hard_refresh_usercodes,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}),
    )

    # Generate lovelace config
    def _generate_lovelace(service: ServiceCall) -> None:
        """Generate the package files."""
        _LOGGER.debug("Generate lovelace file: %s", service)
        lock = get_lock_from_entity_id(hass, service.data[ATTR_ENTITY_ID])
        # generate_lovelace(hass, lock)

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_LOVELACE,
        _generate_lovelace,
        schema=vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id}),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    locks: BaseLock = hass.data[DOMAIN][config_entry.entry_id][CONF_LOCKS].values()
    await asyncio.gather(*[lock.async_unload() for lock in locks])

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry,
        [*PLATFORMS, *hass.data[DOMAIN][config_entry.entry_id]["setup_tasks"].keys()],
    )

    if unload_ok:
        hass.data[DOMAIN].pop(config_entry.entry_id)

    return unload_ok


async def async_update_listener(
    hass: HomeAssistant, config_entry: ConfigEntry, check_options: bool = True
) -> None:
    """Update listener."""
    # No need to update if the options match the data
    if check_options and not config_entry.options:
        return

    entry_id = config_entry.entry_id
    setup_tasks: dict[str | Platform, asyncio.Task] = hass.data[DOMAIN][entry_id][
        "setup_tasks"
    ]

    if config_entry.options:
        curr_slots: dict[int, Any] = copy.deepcopy(config_entry.data[CONF_SLOTS])
        new_slots: dict[int, Any] = copy.deepcopy(
            config_entry.options.get(CONF_SLOTS, {})
        )
        curr_locks: list[str] = copy.deepcopy(config_entry.data[CONF_LOCKS])
        new_locks: list[str] = copy.deepcopy(config_entry.options.get(CONF_LOCKS, []))
    else:
        curr_slots: dict[int, Any] = {}
        new_slots: dict[int, Any] = copy.deepcopy(config_entry.data[CONF_SLOTS])
        curr_locks: list[str] = []
        new_locks: list[str] = copy.deepcopy(config_entry.data[CONF_LOCKS])

    # Set up any platforms that the new slot configs need that haven't already been
    # setup
    for slot_config in new_slots.values():
        for key, platform in PLATFORM_MAP.items():
            if key in slot_config and not setup_tasks.get(platform):
                setup_tasks[platform] = hass.async_create_task(
                    hass.config_entries.async_forward_entry_setup(
                        config_entry, platform
                    )
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

    # Remove lock entities (slot sensors)
    for lock_entity_id in locks_to_remove:
        for slot_num in curr_slots:
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_remove_lock_slot_sensors_{lock_entity_id}"
            )
        hass.data[DOMAIN][entry_id][CONF_LOCKS].pop(lock_entity_id)
        hass.data[DOMAIN][entry_id][COORDINATORS].pop(lock_entity_id)

    # Set up new lock instances, coordinators, and entities
    for lock_entity_id in locks_to_add:
        lock = hass.data[DOMAIN][entry_id][CONF_LOCKS][
            lock_entity_id
        ] = create_lock_instance(
            hass, dr.async_get(hass), er.async_get(hass), config_entry, lock_entity_id
        )
        await lock.async_setup()
        coordinator = hass.data[DOMAIN][entry_id][COORDINATORS][
            lock_entity_id
        ] = LockUsercodeUpdateCoordinator(hass, lock)
        await coordinator.async_config_entry_first_refresh()
        for slot_num in new_slots:
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_lock_slot_sensor", lock, slot_num
            )

    # Remove slot sensors that are no longer in the config
    for slot_num in slots_to_remove.keys():
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_remove_{slot_num}")

    # For each new slot, add standard entities and configuration entities. We also
    # add slot sensors for existing locks only since new locks were already set up
    # above.
    for slot_num, slot_config in slots_to_add.items():
        async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add", slot_num)
        for lock_entity_id, lock in hass.data[DOMAIN][entry_id][CONF_LOCKS].items():
            if lock_entity_id in locks_to_add:
                continue
            async_dispatcher_send(
                hass, f"{DOMAIN}_{entry_id}_add_lock_slot_sensor", lock, slot_num
            )
        for key in UNSET_CONFIG_VALUES_MAP:
            if (
                slot_config.get(key, UNSET_CONFIG_VALUES_MAP[key])
                != UNSET_CONFIG_VALUES_MAP[key]
            ):
                async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add_{key}", slot_num)

    # For all slots that are in both the old and new config, check if any of the
    # configuration options have changed
    for slot_num in {slot for slot in curr_slots if slot in new_slots}:
        for key in UNSET_CONFIG_VALUES_MAP:
            old_val = curr_slots[slot_num].get(key, UNSET_CONFIG_VALUES_MAP[key])
            new_val = new_slots[slot_num].get(key, UNSET_CONFIG_VALUES_MAP[key])

            # If value hasn't changed, skip
            if old_val == new_val:
                continue
            # If value has been removed, fire a signal to remove corresponding entity
            elif (
                old_val != UNSET_CONFIG_VALUES_MAP[key]
                and new_val == UNSET_CONFIG_VALUES_MAP[key]
            ):
                async_dispatcher_send(
                    hass, f"{DOMAIN}_{entry_id}_remove_{slot_num}_{key}"
                )
            # If value has been added, fire a signal to add corresponding entity
            elif (
                old_val == UNSET_CONFIG_VALUES_MAP[key]
                and new_val != UNSET_CONFIG_VALUES_MAP[key]
            ):
                async_dispatcher_send(hass, f"{DOMAIN}_{entry_id}_add_{key}", slot_num)

    # Existing entities will listen to updates and act on it
    new_data = {CONF_LOCKS: new_locks, CONF_SLOTS: new_slots}
    _LOGGER.error("test")
    hass.config_entries.async_update_entry(config_entry, data=new_data, options={})


class LockUsercodeUpdateCoordinator(DataUpdateCoordinator[dict[int, int | str]]):
    """Class to manage usercode updates."""

    def __init__(self, hass: HomeAssistant, lock: BaseLock) -> None:
        self._lock = lock
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {lock.lock.entity_id}",
            update_method=self.async_get_usercodes,
            update_interval=timedelta(seconds=5),
        )
        self.data: dict[int, int | str] = {}

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Wrapper to update usercodes."""
        try:
            return await self._lock.async_get_usercodes()
        except LockDisconnected as err:
            # We can silently fail if we've never been able to retrieve data
            if not self.data:
                return {}
            raise UpdateFailed from err
