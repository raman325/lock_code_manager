"""Lock Code Manager Integration."""

from __future__ import annotations

import asyncio
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
    EVENT_LOVELACE_UPDATED,
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
    instance_id,
)
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
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
from .data import get_entry_data
from .helpers import async_create_lock_instance, get_locks_from_targets
from .models import LockCodeManagerConfigEntry, LockCodeManagerConfigEntryData
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


@callback
def _async_notify_lovelace_dashboards(hass: HomeAssistant) -> None:
    """Fire lovelace_updated for each registered dashboard.

    This triggers the "Configuration changed" toast in the Home Assistant
    frontend, prompting users to refresh the dashboard so the strategy
    re-generates cards for any added or removed slots/locks.
    """
    lovelace_data = hass.data.get(LL_DOMAIN)
    if not lovelace_data:
        return
    for url_path in lovelace_data.dashboards:
        hass.bus.async_fire(EVENT_LOVELACE_UPDATED, {"url_path": url_path})


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
    hass.data[DOMAIN]["instance_id"] = await instance_id.async_get(hass)
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

    # Create or dismiss repair issue based on deprecated number_of_uses presence
    # across ALL config entries (not just this one) since the issue is global
    has_number_of_uses = any(
        CONF_NUMBER_OF_USES in slot_config
        for entry in hass.config_entries.async_entries(DOMAIN)
        for slot_config in get_entry_data(entry, CONF_SLOTS, {}).values()
    )
    if has_number_of_uses:
        async_create_issue(
            hass,
            DOMAIN,
            "number_of_uses_deprecated",
            is_fixable=True,
            is_persistent=True,
            severity=IssueSeverity.WARNING,
            translation_key="number_of_uses_deprecated",
            translation_placeholders={
                "blueprint_url": "https://github.com/raman325/lock_code_manager/wiki/Blueprints#slot-usage-limiter",
            },
        )
    else:
        async_delete_issue(hass, DOMAIN, "number_of_uses_deprecated")

    if hass.state == CoreState.running:
        _setup_entry_after_start(hass, config_entry)
    else:
        started = [False]

        @callback
        def _on_started(event: Event) -> None:
            started[0] = True
            _setup_entry_after_start(hass, config_entry, event)

        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

        @callback
        def _safe_unsub() -> None:
            if not started[0]:
                unsub()

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
    """Handle removal of an entry.

    Routes through the same lock-removed and slot-removed callbacks that
    async_update_listener uses, so that entities are notified symmetrically
    on unload just as they are during a config update that removes all
    slots and locks.
    """
    hass_data = hass.data[DOMAIN]
    runtime_data = config_entry.runtime_data
    callbacks = runtime_data.callbacks

    # Fire slot entity removal callbacks first so per-slot entities (which
    # reference locks) clean up before the locks are torn down
    curr_slots = config_entry.data.get(CONF_SLOTS, {})
    if curr_slots:
        _LOGGER.debug("Unload: removing slots %s", list(curr_slots))
        await asyncio.gather(
            *(
                callbacks.invoke_entity_removers_for_slot(int(slot_num))
                for slot_num in curr_slots
            )
        )

    # Fire lock-removed callbacks so per-lock entities are notified
    lock_ids = list(runtime_data.locks)
    if lock_ids:
        _LOGGER.debug("Unload: removing locks %s", lock_ids)
        for lock_entity_id in lock_ids:
            callbacks.invoke_lock_removed_handlers(lock_entity_id)

    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry,
        {
            *PLATFORMS,
            *runtime_data.setup_tasks.keys(),
        },
    )

    if unload_ok:
        await async_unload_lock(hass, config_entry)

        # Clean up repair issues for this config entry. Use get_entry_data to
        # check both data and options since data migrates to options during setup.
        entry_id = config_entry.entry_id
        for slot_num in get_entry_data(config_entry, CONF_SLOTS, {}):
            async_delete_issue(hass, DOMAIN, f"slot_disabled_{entry_id}_{slot_num}")
            async_delete_issue(hass, DOMAIN, f"pin_required_{entry_id}_{slot_num}")
        # Only delete lock_offline if no other LCM entry manages this lock
        other_entries = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry_id
        ]
        for lock_entity_id in get_entry_data(config_entry, CONF_LOCKS, []):
            still_managed = any(
                lock_entity_id in get_entry_data(e, CONF_LOCKS, [])
                for e in other_entries
            )
            if not still_managed:
                async_delete_issue(hass, DOMAIN, f"lock_offline_{lock_entity_id}")

    if not hass_data.get(CONF_LOCKS):
        await _async_cleanup_strategy_resource(hass, hass_data)

    return unload_ok


async def _async_setup_new_locks(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    locks_to_add: list[str],
    new_slots: dict[int, Any],
    callbacks: Any,
    ent_reg: er.EntityRegistry,
) -> None:
    """Set up newly added locks and create per-slot entities for them."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    hass_data = hass.data[DOMAIN]
    runtime_data = config_entry.runtime_data

    _LOGGER.debug(
        "%s (%s): Adding following locks: %s",
        entry_id,
        entry_title,
        locks_to_add,
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
            await lock.async_wait_for_setup()
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
            await lock.async_setup_internal(config_entry)

        added_locks.append(lock)

        # Check if lock is connected (but don't wait - entity creation doesn't require it)
        if not await lock.async_internal_is_integration_connected():
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


async def _async_reconcile_slot_entities(
    config_entry: LockCodeManagerConfigEntry,
    slot_num: int,
    old_config: dict[str, Any],
    new_config: dict[str, Any],
    callbacks: Any,
    ent_reg: er.EntityRegistry,
) -> None:
    """Reconcile entities for a slot whose configuration has changed."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    entities_to_remove: set[str] = set()
    entities_to_add: set[str] = set()

    # Check if number of uses has changed
    old_val = old_config.get(CONF_NUMBER_OF_USES)
    new_val = new_config.get(CONF_NUMBER_OF_USES)

    # If number of uses value hasn't changed, skip
    if old_val == new_val:
        return

    # If number of uses value has been removed, fire a signal to remove
    # corresponding entity
    if old_val not in (None, "") and new_val in (None, ""):
        entities_to_remove.add(CONF_NUMBER_OF_USES)
    # If number of uses value has been added, fire a signal to add
    # corresponding entity
    elif old_val in (None, "") and new_val not in (None, ""):
        entities_to_add.add(CONF_NUMBER_OF_USES)

    for key in entities_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing %s entity for slot %s due to changed configuration",
            entry_id,
            entry_title,
            key,
            slot_num,
        )
        await callbacks.invoke_entity_removers_for_key(slot_num, key)

    for key in entities_to_add:
        _LOGGER.debug(
            "%s (%s): Adding %s entity for slot %s due to changed configuration",
            entry_id,
            entry_title,
            key,
            slot_num,
        )
        callbacks.invoke_keyed_adders(key, slot_num, ent_reg)


async def async_update_listener(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """Update listener."""
    # No need to update if there are no options because that only happens at the end
    # of this function
    if not config_entry.options:
        return

    runtime_data = config_entry.runtime_data
    ent_reg = er.async_get(hass)

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    _LOGGER.info("%s (%s): Creating and/or updating entities", entry_id, entry_title)

    setup_tasks = runtime_data.setup_tasks

    curr_slots: dict[int, Any] = {**config_entry.data.get(CONF_SLOTS, {})}
    new_slots: dict[int, Any] = {**config_entry.options.get(CONF_SLOTS, {})}
    curr_locks: list[str] = [*config_entry.data.get(CONF_LOCKS, [])]
    new_locks: list[str] = [*config_entry.options.get(CONF_LOCKS, [])]

    # Strip number_of_uses from slots that didn't previously have it
    # (deprecated — only existing values are preserved). Skip on initial
    # setup (curr_slots empty) since the data just moved from data→options.
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
            hass.config_entries.async_forward_entry_setups(config_entry, [platform]),
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

    callbacks = runtime_data.callbacks

    # Remove slot entities first so per-slot entities (which reference locks)
    # clean up before the locks are torn down
    if slots_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing slots %s", entry_id, entry_title, list(slots_to_remove)
        )
        await asyncio.gather(
            *(
                callbacks.invoke_entity_removers_for_slot(slot_num)
                for slot_num in slots_to_remove
            )
        )

    # Remove old lock entities
    if locks_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing locks %s", entry_id, entry_title, locks_to_remove
        )
    for lock_entity_id in locks_to_remove:
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

    # Notify any existing entities that additional locks have been added then create
    # slot PIN sensors for the new locks
    if locks_to_add:
        await _async_setup_new_locks(
            hass, config_entry, locks_to_add, new_slots, callbacks, ent_reg
        )

    # For each new slot, add standard entities and configuration entities. We also
    # add slot sensors for existing locks only since new locks were already set up
    # above.
    for slot_num, slot_config in slots_to_add.items():
        # First we store the set of entities we are adding so we can track when they
        # are done
        entities_to_add: set[str] = {
            CONF_ENABLED,
            CONF_NAME,
            CONF_PIN,
            EVENT_PIN_USED,
        }

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

    # For all slots that are in both the old and new config, check if any of the
    # configuration options have changed
    for slot_num in set(curr_slots).intersection(new_slots):
        await _async_reconcile_slot_entities(
            config_entry,
            slot_num,
            curr_slots[slot_num],
            new_slots[slot_num],
            callbacks,
            ent_reg,
        )

    # Existing entities will listen to updates and act on it
    new_data = {CONF_LOCKS: new_locks, CONF_SLOTS: new_slots}
    _LOGGER.info(
        "%s (%s): Done creating and/or updating entities", entry_id, entry_title
    )
    hass.config_entries.async_update_entry(config_entry, data=new_data, options={})

    # Notify Lovelace dashboards to re-render when structure changes
    # (slots or locks added/removed), so strategy-generated cards update
    if slots_to_add or slots_to_remove or locks_to_add or locks_to_remove:
        _async_notify_lovelace_dashboards(hass)
