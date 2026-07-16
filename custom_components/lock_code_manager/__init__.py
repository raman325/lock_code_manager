"""Lock Code Manager Integration."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
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
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_ENTITY_ID,
    CONF_ID,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_LOVELACE_UPDATED,
)
from homeassistant.core import (
    CoreState,
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
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
    ATTR_CODE_SLOT,
    ATTR_LENGTH,
    ATTR_LOCK_ENTITY_ID,
    ATTR_SLOT,
    ATTR_TEXT,
    ATTR_USERCODE,
    CONDITION_ENTITY_DOMAINS,
    CONF_CALENDAR,
    CONF_SLOTS,
    DOMAIN,
    PLATFORM_MAP,
    PLATFORMS,
    SERVICE_CLEAR_SLOT_CONDITION,
    SERVICE_CLEAR_USERCODE,
    SERVICE_DEOBFUSCATE_LOG,
    SERVICE_GENERATE_PIN,
    SERVICE_HARD_REFRESH_USERCODES,
    SERVICE_SET_SLOT_CONDITION,
    SERVICE_SET_USERCODE,
    STRATEGY_FILENAME,
    STRATEGY_PATH,
    Platform,
)
from .domain.config import EntryConfig
from .domain.exceptions import LockDisconnected, LockOperationFailed
from .domain.locks import async_create_lock_instance, get_locks_from_targets
from .domain.models import (
    LockCodeManagerConfigEntry,
    LockCodeManagerConfigEntryRuntimeData,
)
from .domain.pin_generator import (
    DEFAULT_PIN_LENGTH,
    MAX_PIN_LENGTH,
    MIN_PIN_LENGTH,
    generate_pin,
)
from .domain.queries import get_entry_config
from .domain.services import (
    async_clear_slot_condition,
    async_clear_usercode,
    async_set_slot_condition,
    async_set_usercode,
)
from .domain.slot_coordinator import SlotEntityCoordinator
from .domain.util import build_pin_deobfuscation_map, deobfuscate_pins
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

    if config_entry.version == 2:
        # Strip the deprecated number_of_uses field from slot configs and
        # surface a one-time informational repair pointing users to the Slot
        # Usage Limiter blueprint replacement. Running here (before setup)
        # ensures the deprecated field never reaches any EntryConfig consumer.
        async_delete_issue(hass, DOMAIN, "number_of_uses_deprecated")
        new_data = {**config_entry.data}
        new_options = {**config_entry.options}
        entry_impacted: set[str] = set()
        for data_dict in (new_data, new_options):
            if CONF_SLOTS not in data_dict:
                continue
            new_slots = {}
            for slot_num, slot_config in data_dict[CONF_SLOTS].items():
                new_slot = {**slot_config}
                if "number_of_uses" in new_slot:
                    new_slot.pop("number_of_uses")
                    entry_impacted.add(str(slot_num))
                new_slots[slot_num] = new_slot
            data_dict[CONF_SLOTS] = new_slots
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=3
        )
        if entry_impacted:
            impacted_slots = sorted(entry_impacted, key=int)
            async_create_issue(
                hass,
                DOMAIN,
                f"number_of_uses_removed_{config_entry.entry_id}",
                is_fixable=True,
                is_persistent=True,
                severity=IssueSeverity.WARNING,
                translation_key="number_of_uses_removed",
                translation_placeholders={
                    "impacted": (
                        f"- **{config_entry.title}**: slots {', '.join(impacted_slots)}"
                    ),
                    "blueprint_url": (
                        "https://github.com/raman325/lock_code_manager/wiki/"
                        "Blueprints#slot-usage-limiter"
                    ),
                },
            )
            _LOGGER.warning(
                "Removed deprecated number_of_uses from %s slot(s): %s. "
                "Use the Slot Usage Limiter blueprint instead.",
                config_entry.title,
                ", ".join(impacted_slots),
            )

    return True


@callback
def _async_notify_lovelace_dashboards(hass: HomeAssistant) -> None:
    """
    Fire lovelace_updated for each registered dashboard.

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
    hass.data.setdefault(DOMAIN, {"resources": False})
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

    await async_websocket_setup(hass)
    _LOGGER.debug("Finished setting up websocket API")

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

    async def _set_usercode(service: ServiceCall) -> None:
        """Set a usercode on a lock slot."""
        await async_set_usercode(
            hass,
            service.data[ATTR_LOCK_ENTITY_ID],
            service.data[ATTR_CODE_SLOT],
            service.data[ATTR_USERCODE],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_USERCODE,
        _set_usercode,
        schema=vol.Schema(
            {
                vol.Required(ATTR_LOCK_ENTITY_ID): cv.entity_domain("lock"),
                vol.Required(ATTR_CODE_SLOT): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
                vol.Required(ATTR_USERCODE): vol.All(
                    cv.string, str.strip, vol.Length(min=1)
                ),
            }
        ),
    )

    async def _clear_usercode(service: ServiceCall) -> None:
        """Clear a usercode from a lock slot."""
        await async_clear_usercode(
            hass,
            service.data[ATTR_LOCK_ENTITY_ID],
            service.data[ATTR_CODE_SLOT],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_USERCODE,
        _clear_usercode,
        schema=vol.Schema(
            {
                vol.Required(ATTR_LOCK_ENTITY_ID): cv.entity_domain("lock"),
                vol.Required(ATTR_CODE_SLOT): vol.All(
                    vol.Coerce(int), vol.Range(min=1)
                ),
            }
        ),
    )

    async def _set_slot_condition(service: ServiceCall) -> None:
        """Set a condition entity for a slot."""
        await async_set_slot_condition(
            hass,
            service.data["config_entry_id"],
            service.data[ATTR_SLOT],
            service.data[CONF_ENTITY_ID],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SLOT_CONDITION,
        _set_slot_condition,
        schema=vol.Schema(
            {
                vol.Required("config_entry_id"): cv.string,
                vol.Required(ATTR_SLOT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=9999)
                ),
                vol.Required(CONF_ENTITY_ID): cv.entity_domain(
                    CONDITION_ENTITY_DOMAINS
                ),
            }
        ),
    )

    async def _clear_slot_condition(service: ServiceCall) -> None:
        """Clear the condition entity from a slot."""
        await async_clear_slot_condition(
            hass,
            service.data["config_entry_id"],
            service.data[ATTR_SLOT],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_SLOT_CONDITION,
        _clear_slot_condition,
        schema=vol.Schema(
            {
                vol.Required("config_entry_id"): cv.string,
                vol.Required(ATTR_SLOT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=9999)
                ),
            }
        ),
    )

    async def _generate_pin(call: ServiceCall) -> ServiceResponse:
        """Generate a random PIN that avoids known unsafe patterns."""
        return {"pin": generate_pin(call.data[ATTR_LENGTH])}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GENERATE_PIN,
        _generate_pin,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_LENGTH, default=DEFAULT_PIN_LENGTH): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_PIN_LENGTH, max=MAX_PIN_LENGTH)
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    async def _deobfuscate_log(call: ServiceCall) -> ServiceResponse:
        """Reverse mask_pin() tokens in pasted log text against the current config."""
        instance_id = hass.data.get(DOMAIN, {}).get("instance_id", "")
        if not instance_id:
            raise HomeAssistantError(
                "Lock Code Manager is not fully set up yet; try again in a moment"
            )
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        table = build_pin_deobfuscation_map(entries, instance_id)
        deobfuscated, summary = deobfuscate_pins(call.data[ATTR_TEXT], table)
        # Sentinel banner so users see at a glance that the response contains
        # plaintext PINs and must not be pasted into a public issue.
        wrapped = (
            "=== BEGIN DEOBFUSCATED — DO NOT SHARE ===\n"
            f"{deobfuscated}\n"
            "=== END DEOBFUSCATED ==="
        )
        return {"deobfuscated_text": wrapped, "summary": summary}

    hass.services.async_register(
        DOMAIN,
        SERVICE_DEOBFUSCATE_LOG,
        _deobfuscate_log,
        schema=vol.Schema({vol.Required(ATTR_TEXT): cv.string}),
        supports_response=SupportsResponse.ONLY,
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

    Should only be run once Home Assistant has started. Update-listener
    registration is guarded by ``runtime_data.update_listener_registered`` so
    a reload racing with EVENT_HOMEASSISTANT_STARTED cannot stack multiple
    listeners on the same entry.
    """
    runtime_data = config_entry.runtime_data
    if not runtime_data.update_listener_registered:
        runtime_data.update_listener_registered = True
        unsub = config_entry.add_update_listener(async_update_listener)

        @callback
        def _clear_listener_registered() -> None:
            runtime_data.update_listener_registered = False
            unsub()

        config_entry.async_on_unload(_clear_listener_registered)

    if config_entry.data:
        # Move data from data to options so update listener can work.
        # Merge options-preferred (matching EntryConfig.from_entry): a
        # non-empty options here holds an options-flow save the entry
        # could not process (no listener was registered while it was
        # failed) — overwriting it with data would silently discard the
        # user's fix.
        hass.config_entries.async_update_entry(
            config_entry,
            data={},
            options={**config_entry.data, **config_entry.options},
        )
    else:
        hass.async_create_task(
            async_update_listener(hass, config_entry),
            f"Initial setup for entities for {config_entry.entry_id}",
        )


async def async_setup_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> bool:
    """Set up a config entry."""
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    try:
        entity_id = next(
            entity_id
            for entity_id in get_entry_config(config_entry).locks
            if not ent_reg.async_get(entity_id)
        )
    except StopIteration:
        pass
    else:
        config_entry.async_start_reauth(hass, context={"lock_entity_id": entity_id})
        raise ConfigEntryError(
            f"Unable to start because lock {entity_id} can't be found"
        )

    hass.data.setdefault(DOMAIN, {"resources": False})
    await _async_register_strategy_resource(hass)

    config_entry.runtime_data = LockCodeManagerConfigEntryRuntimeData(
        config=EntryConfig.from_entry(config_entry),
    )

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
        # async_listen_once self-unsubscribes when it fires, so calling
        # unsub() again would error. Track whether it fired so unload only
        # tears down the listener if HA never started before unload.
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


def _lock_managed_by_other_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock_entity_id: str,
) -> bool:
    """Return True if another (non-disabled, non-ignored) LCM entry manages the lock."""
    return any(
        entry.entry_id != config_entry.entry_id
        and get_entry_config(entry).has_lock(lock_entity_id)
        for entry in hass.config_entries.async_entries(
            DOMAIN, include_disabled=False, include_ignore=False
        )
    )


def _find_shared_lock_instance(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock_entity_id: str,
) -> BaseLock | None:
    """
    Return an existing BaseLock for ``lock_entity_id`` from another loaded entry.

    A single physical lock may be referenced by multiple Lock Code Manager
    entries. We keep one BaseLock instance and share it: when a new entry
    references a lock that another loaded entry is already managing,
    reuse that entry's instance instead of creating a duplicate.
    """
    return next(
        (
            entry.runtime_data.locks[lock_entity_id]
            for entry in hass.config_entries.async_entries(
                DOMAIN, include_disabled=False, include_ignore=False
            )
            if entry.entry_id != config_entry.entry_id
            and entry.state is ConfigEntryState.LOADED
            and lock_entity_id in entry.runtime_data.locks
        ),
        None,
    )


async def async_unload_lock(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock_entity_id: str | None = None,
    remove_permanently: bool = False,
):
    """Unload lock."""
    runtime_data = config_entry.runtime_data
    lock_entity_ids = (
        [lock_entity_id] if lock_entity_id else list(runtime_data.locks.keys())
    )
    for _lock_entity_id in lock_entity_ids:
        lock = runtime_data.locks.pop(_lock_entity_id, None)
        if lock is None:
            continue
        if not _lock_managed_by_other_entry(hass, config_entry, _lock_entity_id):
            await lock.async_unload(remove_permanently)
            if lock.coordinator is not None:
                await lock.coordinator.async_shutdown()


async def async_unload_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> bool:
    """Unload an entry, stopping tick managers before tearing down platforms."""
    hass_data = hass.data[DOMAIN]
    runtime_data = config_entry.runtime_data
    callbacks = runtime_data.callbacks

    # Stop tick managers FIRST so no in-flight tick can keep calling
    # _perform_sync, coordinator.async_refresh, or _write_state once
    # downstream teardown begins. SlotSyncManager.async_stop is idempotent,
    # so the binary sensor's later async_will_remove_from_hass call into the
    # same manager is a cheap no-op.
    if runtime_data.sync_managers:
        _LOGGER.debug(
            "Unload: stopping %s sync manager(s)", len(runtime_data.sync_managers)
        )
        mgrs_to_stop = list(runtime_data.sync_managers)
        stop_results = await asyncio.gather(
            *(mgr.async_stop() for mgr in mgrs_to_stop),
            return_exceptions=True,
        )
        # Clear the registry explicitly so the lock-removed callbacks fired
        # below observe an empty set. Entity removal also discards each
        # manager during async_will_remove_from_hass, but that path only
        # runs if invoke_entity_removers_for_slot has populated slots --
        # which it may not when config has been migrated to options.
        runtime_data.sync_managers.clear()
        for mgr, result in zip(mgrs_to_stop, stop_results, strict=True):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                _LOGGER.warning(
                    "%s: Sync manager stop raised during unload: %s",
                    mgr.log_prefix,
                    result,
                    exc_info=result,
                )

    # Fire slot entity removal callbacks first so per-slot entities (which
    # reference locks) clean up before the locks are torn down. Read
    # current slots from the cached EntryConfig view because
    # ``_setup_entry_after_start`` migrates the entry's data to options
    # at first setup, so ``config_entry.data`` is empty for any
    # normally-loaded entry.
    curr_slots = list(get_entry_config(config_entry).slots)
    if curr_slots:
        _LOGGER.debug("Unload: removing slots %s", curr_slots)
        await asyncio.gather(
            *(
                callbacks.invoke_entity_removers_for_slot(slot_num)
                for slot_num in curr_slots
            )
        )

    # Stop per-slot coordinators after entity removal so the entities'
    # async_will_remove_from_hass can still call into them. One raising
    # stop must not block the rest -- the registry is cleared whether
    # individual stops succeed or fail.
    for coordinator in list(runtime_data.slot_coordinators.values()):
        try:
            coordinator.async_stop()
        except Exception:
            _LOGGER.exception("Unload: slot coordinator stop raised")
    runtime_data.slot_coordinators.clear()

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

    # Only clean up the strategy resource if no other Lock Code Manager
    # entries remain loaded. The current entry is still listed (in
    # UNLOAD_IN_PROGRESS / NOT_LOADED state at this point) so filter it
    # out before checking.
    other_loaded_entries = any(
        entry.entry_id != config_entry.entry_id
        and entry.state is ConfigEntryState.LOADED
        for entry in hass.config_entries.async_entries(
            DOMAIN, include_disabled=False, include_ignore=False
        )
    )
    if not other_loaded_entries:
        await _async_cleanup_strategy_resource(hass, hass_data)

    return unload_ok


async def async_remove_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """
    Clean up persistent repair issues when the entry is fully removed.

    Called by Home Assistant only on entry deletion -- not on unload,
    reload, disable, or HA restart. The repair issues created by this
    integration are flagged ``is_persistent=True`` so they survive
    restarts and reloads; clearing them belongs here, not in
    ``async_unload_entry``, so they outlive any non-deletion unload.
    """
    entry_id = config_entry.entry_id
    config = get_entry_config(config_entry)
    for slot_num in config.slots:
        async_delete_issue(hass, DOMAIN, f"slot_disabled_{entry_id}_{slot_num}")
        async_delete_issue(hass, DOMAIN, f"pin_required_{entry_id}_{slot_num}")
    for lock_entity_id in config.locks:
        # Only delete per-lock issues if no other LCM entry manages this lock.
        if not _lock_managed_by_other_entry(hass, config_entry, lock_entity_id):
            async_delete_issue(hass, DOMAIN, f"lock_offline_{lock_entity_id}")
            async_delete_issue(hass, DOMAIN, f"lock_setup_failed_{lock_entity_id}")
        for slot_num in config.slots:
            async_delete_issue(
                hass,
                DOMAIN,
                f"slot_suspended_{entry_id}_{lock_entity_id}_{slot_num}",
            )


async def _async_setup_new_locks(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    locks_to_add: Sequence[str],
    new_config: EntryConfig,
    callbacks: Any,
    ent_reg: er.EntityRegistry,
) -> None:
    """Set up newly added locks and create per-slot entities for them."""
    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    runtime_data = config_entry.runtime_data

    _LOGGER.debug(
        "%s (%s): Adding following locks: %s",
        entry_id,
        entry_title,
        locks_to_add,
    )

    async def _setup_one_lock(lock_entity_id: str) -> BaseLock:
        existing_lock = _find_shared_lock_instance(hass, config_entry, lock_entity_id)
        if existing_lock is not None:
            _LOGGER.debug(
                "%s (%s): Reusing lock instance for lock %s",
                entry_id,
                entry_title,
                existing_lock,
            )
            runtime_data.locks[lock_entity_id] = existing_lock
            await existing_lock.async_wait_for_setup()
            return existing_lock

        lock = runtime_data.locks[lock_entity_id] = async_create_lock_instance(
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
        return lock

    # Set up locks concurrently. Each lock's initial usercode fetch can take
    # seconds (Z-Wave node poll, Schlage HTTP, Matter device read); serial
    # setup meant lock N+1 only began once lock N finished. return_exceptions
    # isolates per-lock failures so one bad lock does not block the others.
    setup_results = await asyncio.gather(
        *(_setup_one_lock(lock_entity_id) for lock_entity_id in locks_to_add),
        return_exceptions=True,
    )

    added_locks: list[BaseLock] = []
    for lock_entity_id, result in zip(locks_to_add, setup_results, strict=True):
        if isinstance(result, BaseException):
            # Only unexpected exceptions land here: transport failures
            # degrade inside async_setup_internal and structural
            # validation failures are logged there and kept degraded, so
            # a popped lock indicates a genuine bug, not a lock state.
            _LOGGER.error(
                "%s (%s): Failed to set up lock %s: %s",
                entry_id,
                entry_title,
                lock_entity_id,
                result,
                exc_info=result,
            )
            runtime_data.locks.pop(lock_entity_id, None)
            continue

        added_locks.append(result)

        if not await result.async_internal_is_reachable():
            _LOGGER.debug(
                "%s (%s): Lock %s is not connected yet. Entities will be created "
                "but will be unavailable until the lock comes online. This is normal "
                "during startup if Z-Wave JS is still initializing.",
                entry_id,
                entry_title,
                result.lock.entity_id,
            )

        for slot_num in new_config.slots:
            _LOGGER.debug(
                "%s (%s): Adding lock %s slot %s sensor and event entity",
                entry_id,
                entry_title,
                lock_entity_id,
                slot_num,
            )
            callbacks.invoke_lock_slot_adders(result, slot_num, ent_reg)

    if added_locks:
        callbacks.invoke_lock_added_handlers(added_locks)


async def async_update_listener(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """Update listener."""
    # Refresh the cached EntryConfig on EVERY update — including entity-driven
    # writes that go straight to data with empty options (e.g. a slot's name or
    # PIN being edited via its text entity). The early-return below skips the
    # entity-creation pass for those cases, but downstream readers via
    # runtime_data.config still need to see the current data.
    runtime_data = config_entry.runtime_data
    runtime_data.config = EntryConfig.from_entry(config_entry)

    # Notify per-slot coordinators so derived "active" state and condition-
    # entity subscriptions stay in sync with the refreshed config view.
    # Runs on both the entity-driven path (early return below) and the
    # options-flow path so a calendar/condition swap is picked up the
    # same way.
    for coordinator in runtime_data.slot_coordinators.values():
        coordinator.notify_config_changed()

    # No need to do entity creation/removal work if there are no options
    # because that only happens at the end of this function (data + empty
    # options = the post-listener state we just wrote ourselves).
    if not config_entry.options:
        return

    ent_reg = er.async_get(hass)

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    _LOGGER.info("%s (%s): Creating and/or updating entities", entry_id, entry_title)

    setup_tasks = runtime_data.setup_tasks

    # Build EntryConfig views of data (old) and options (new) so all
    # downstream slot lookups use int keys regardless of how the storage
    # round-tripped them. Callers that need a plain dict (e.g. to hand
    # back to async_update_entry at the end of this function) call
    # to_dict() at the write site.
    old_config = EntryConfig.from_mapping(config_entry.data)
    new_config = EntryConfig.from_mapping(config_entry.options)
    new_slots = new_config.slots

    # Set up any platforms that the new slot configs need that haven't
    # already been set up. The number_of_uses deprecation cleanup runs
    # in async_setup_entry before platform forwarding, not here.
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

    diff = old_config - new_config
    slots_to_add = diff.slots_added
    slots_to_remove = diff.slots_removed
    locks_to_add = diff.locks_added
    locks_to_remove = diff.locks_removed

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
        for slot_num in slots_to_remove:
            coordinator = runtime_data.slot_coordinators.pop(slot_num, None)
            if coordinator is None:
                continue
            try:
                coordinator.async_stop()
            except Exception:
                _LOGGER.exception(
                    "%s (%s): slot %s coordinator stop raised",
                    entry_id,
                    entry_title,
                    slot_num,
                )

    # Release lock-side state LCM owns for any (lock, slot) pair that no
    # longer exists in config. Native-user providers (Matter, Z-Wave User
    # Credential CC under the user-tag idempotency design) override
    # ``async_release_managed_slot`` to delete the LCM-tagged user that
    # anchored the slot; slot-only providers leave the default no-op in
    # place. This runs before ``locks_to_remove`` processing so providers
    # in ``runtime_data.locks`` are still usable.
    for lock_entity_id, slot_num in diff.pairs_removed:
        release_lock = runtime_data.locks.get(lock_entity_id)
        if release_lock is None:
            continue
        try:
            await release_lock.async_release_managed_slot(slot_num)
        except (LockDisconnected, LockOperationFailed) as err:
            # The slot is gone from LCM config either way; lock-side cleanup
            # is best-effort and must not block the teardown.
            _LOGGER.warning(
                "%s (%s): could not release slot %s on lock %s: %s",
                entry_id,
                entry_title,
                slot_num,
                lock_entity_id,
                err,
            )

    if locks_to_remove:
        _LOGGER.debug(
            "%s (%s): Removing locks %s", entry_id, entry_title, locks_to_remove
        )
    for lock_entity_id in locks_to_remove:
        callbacks.invoke_lock_removed_handlers(lock_entity_id)
        lock: BaseLock = runtime_data.locks[lock_entity_id]
        if lock.device_entry:
            dev_reg = dr.async_get(hass)
            dev_reg.async_update_device(
                lock.device_entry.id, remove_config_entry_id=entry_id
            )
        await async_unload_lock(
            hass, config_entry, lock_entity_id=lock_entity_id, remove_permanently=True
        )

    # Create per-slot coordinators for new slots BEFORE setting up new
    # locks. _async_setup_new_locks awaits per-lock connection checks,
    # giving the event loop opportunities to drain entity-add tasks it
    # scheduled for prior locks; those per-lock entities (`code`,
    # `in_sync`) look up the slot coordinator in async_added_to_hass and
    # would warn if it did not yet exist.
    for slot_num in slots_to_add:
        coordinator = SlotEntityCoordinator(hass, config_entry, slot_num)
        runtime_data.slot_coordinators[slot_num] = coordinator
        coordinator.async_start()

    if locks_to_add:
        await _async_setup_new_locks(
            hass, config_entry, locks_to_add, new_config, callbacks, ent_reg
        )

    # For each new slot: add standard entities, then per-lock entities for
    # existing locks (new locks already got their per-lock entities above).
    for slot_num in slots_to_add:
        _LOGGER.debug(
            "%s (%s): Adding standard entities for slot %s",
            entry_id,
            entry_title,
            slot_num,
        )
        callbacks.invoke_standard_adders(slot_num, ent_reg)

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

    # Use to_dict() so the stored data has plain dicts (not the read-only
    # MappingProxyType wrappers EntryConfig uses internally) — HA's
    # storage layer can't serialize MappingProxyType.
    _LOGGER.info(
        "%s (%s): Done creating and/or updating entities", entry_id, entry_title
    )
    hass.config_entries.async_update_entry(
        config_entry, data=new_config.to_dict(), options={}
    )
    # The async_update_entry above re-triggers this listener, which
    # refreshes runtime_data.config at the top before the early-return.

    # Notify Lovelace dashboards to re-render when structure changes
    # (slots or locks added/removed), so strategy-generated cards update
    if diff.has_changes:
        _async_notify_lovelace_dashboards(hass)
