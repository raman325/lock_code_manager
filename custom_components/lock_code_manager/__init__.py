"""Lock Code Manager Integration."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Iterable, Mapping, Sequence
from types import MappingProxyType
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components.event import DOMAIN as EVENT_DOMAIN
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    DOMAIN as LL_DOMAIN,
)
from homeassistant.components.lovelace.resources import (
    ResourceStorageCollection,
    ResourceYAMLCollection,
)
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.const import (
    ATTR_AREA_ID,
    ATTR_DEVICE_ID,
    ATTR_ENTITY_ID,
    CONF_CONDITION,
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
from homeassistant.util import slugify

from .const import (
    ATTR_CLEAR_CREDENTIALS,
    ATTR_CODE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_ENABLE_IF_DISABLED,
    ATTR_LENGTH,
    ATTR_SOURCE,
    ATTR_TARGET,
    ATTR_TEXT,
    ATTR_VALUE,
    CONDITION_ENTITY_DOMAINS,
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOT,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    LEGACY_EVENT_PIN_USED,
    PER_LOCK_ENTITY_SUFFIX,
    PLATFORM_MAP,
    PLATFORMS,
    RENAMES_KEY,
    SERVICE_ADD_USER,
    SERVICE_CLEAR_CONDITION,
    SERVICE_CLEAR_CREDENTIAL,
    SERVICE_DELETE_USER,
    SERVICE_DEOBFUSCATE_LOG,
    SERVICE_DISABLE_USER,
    SERVICE_ENABLE_USER,
    SERVICE_GENERATE_PIN,
    SERVICE_HARD_REFRESH_USERCODES,
    SERVICE_SET_CONDITION,
    SERVICE_SET_CREDENTIAL,
    SERVICE_USE_CREDENTIAL,
    STRATEGY_FILENAME,
    STRATEGY_PATH,
    SUBENTRY_TYPE_USER,
    Platform,
)
from .domain.config import (
    EntryConfigDiff,
    async_write_entry_config,
    EntryConfig,
    build_slot_device_identifier,
    build_slot_unique_id,
    parse_slot_device_identifier,
    parse_slot_unique_id,
)
from .domain.credentials import CredentialType
from .domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
    UnclaimedLockError,
)
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
from .domain.references import async_notify_moved
from .domain.slot_assignment import CONF_SLOT_ASSIGNMENT
from .domain.services import (
    async_add_users,
    async_clear_condition,
    async_clear_credential,
    async_delete_users,
    async_disable_user,
    async_enable_user,
    async_set_condition,
    async_set_credential,
    async_use_credential,
)
from .domain.slot_coordinator import SlotEntityCoordinator
from .domain.unmanaged import async_sweep_unmanaged_codes
from .domain.user_migration import migrate_to_users
from .domain.util import (
    PER_LOCK_ISSUE_KEYS,
    build_pin_deobfuscation_map,
    deobfuscate_pins,
    lock_display_name,
    per_lock_issue_id,
)
from .entity import build_slot_device_info
from .providers import BaseLock
from .websocket import async_setup as async_websocket_setup

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Either identifier names the same entry, so an action can be pointed at one
# the way a card is. Mirrors the websocket API's vol.Exclusive pair rather
# than inventing a second convention.
_ENTRY_SELECTOR = {
    vol.Exclusive("config_entry_id", "entry"): cv.string,
    vol.Exclusive("config_entry_title", "entry"): cv.string,
}


# One user, as `add_user` takes them.
USER_ENTRY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        # Stripped here as the config flow strips its own field: a submitted
        # code is stripped before it is matched, so a PIN stored with padding
        # matches nothing anybody can type.
        vol.Optional(CONF_PIN): vol.All(cv.string, vol.Strip),
        vol.Optional(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_CONDITION): cv.entity_domain(CONDITION_ENTITY_DOMAINS),
    }
)

# The fields a pre-6.0 `add_user` call carried at the top level.
_FLAT_USER_FIELDS = (CONF_NAME, CONF_PIN, CONF_ENABLED, CONF_CONDITION)


def _flat_user_to_list(data: dict[str, Any]) -> dict[str, Any]:
    """
    Accept the released flat shape as a one-user list.

    ``add_user`` used to take ``name``/``pin``/``enabled``/``condition`` at the
    top level. Those calls have no ``users`` key for ``ensure_list`` to reach,
    so they are folded into one here -- before validation, so everything after
    this point sees a list and nothing has to know two shapes.

    A call already using ``users`` is left alone. Both keys together is a
    caller mixing shapes, which is refused rather than guessed at.
    """
    if CONF_USERS in data:
        if any(field in data for field in _FLAT_USER_FIELDS):
            raise vol.Invalid(
                f"Use either {CONF_USERS} or the individual user fields, not both"
            )
        return data
    if CONF_NAME not in data:
        # Nothing to fold. Let the schema below report what is missing.
        return data
    user = {field: data[field] for field in _FLAT_USER_FIELDS if field in data}
    rest = {key: value for key, value in data.items() if key not in _FLAT_USER_FIELDS}
    return {**rest, CONF_USERS: [user]}


def _entry_schema(fields: dict[Any, Any]) -> vol.Schema:
    """Build a service schema that takes an entry by id or by title."""
    return vol.Schema(
        vol.All(
            {**_ENTRY_SELECTOR, **fields},
            cv.has_at_least_one_key("config_entry_id", "config_entry_title"),
        )
    )


# The keys that stopped living on the entry when users became subentries.
_MOVED_KEYS = frozenset({CONF_USERS, CONF_SLOTS, CONF_SLOT_ASSIGNMENT})


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

    if config_entry.version == 3:
        # Two halves of one version bump: name every slot, then key the
        # configuration by that name and demote the slot number to internal
        # bookkeeping.
        #
        # IDENTIFIERS do not move -- unique IDs keep the slot number, so no
        # entity loses its registry entry, its settings or its history. Only
        # the entity IDs are re-slugged, below, once the names are known.
        new_data, renamed_in_data, dropped_in_data = migrate_to_users(config_entry.data)
        new_options, renamed_in_options, dropped_in_options = migrate_to_users(
            config_entry.options
        )
        renamed = set(renamed_in_data) | set(renamed_in_options)
        dropped = set(dropped_in_data) | set(dropped_in_options)
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=new_options, version=4
        )
        migrated = EntryConfig.from_mapping({**new_data, **new_options})
        _async_rename_event_unique_ids(hass, config_entry, migrated)
        if re_slugged := _async_rename_slot_entity_ids(hass, config_entry, migrated):
            _LOGGER.info(
                "%s (%s): renamed %d entity id(s) onto their user's name: %s",
                config_entry.entry_id,
                config_entry.title,
                len(re_slugged),
                ", ".join(f"{was} -> {now}" for was, now in sorted(re_slugged)),
            )
            # Home Assistant repoints recorder history on a rename, but nothing
            # rewrites an entity id stored inside an automation or script --
            # only the frontend's own rename dialog offers that, and this did
            # not go through it. Automations built from the Slot Usage Limiter
            # and Slot Usage Notifier blueprints hold these ids directly, so
            # the user is given the mapping rather than left to find it.
            # Only recorded here. Telling the user waits for Home Assistant
            # to have started, because working out what still points at the
            # old IDs needs the automation and script components to have
            # loaded, and during a config entry's setup they may not have.
            # Accumulated across entries so several of them produce one
            # message rather than one apiece.
            hass.data.setdefault(DOMAIN, {}).setdefault(RENAMES_KEY, {}).update(
                dict(re_slugged)
            )
        if dropped:
            _async_purge_dropped_slots(hass, config_entry, dropped)
            _LOGGER.info(
                "%s (%s): dropped %d empty slot(s) with neither a name nor a "
                "PIN: %s. They held no credential and named nobody",
                config_entry.entry_id,
                config_entry.title,
                len(dropped),
                ", ".join(sorted(dropped, key=int)),
            )
        if renamed:
            _LOGGER.info(
                "%s (%s): named %d previously-unnamed or conflicting slot(s): %s. "
                "Locks that store a user name will pick this up on the next sync",
                config_entry.entry_id,
                config_entry.title,
                len(renamed),
                ", ".join(sorted(renamed, key=int)),
            )
        _async_remove_hub_device(hass, config_entry)
        # Last, and part of the same version bump: from here on a slot
        # leaving the configuration takes its credential with it, so the
        # codes earlier versions left behind are offered up once, measured
        # against the configuration users will actually have.
        await async_sweep_unmanaged_codes(hass, config_entry)

    if config_entry.version == 4:
        # Users move out of entry data and into their own subentries, each
        # carrying the credential position that used to live in the side
        # table beside them. Nothing renames: unique IDs keep the slot
        # number, so no entity loses its registry entry or its history.
        side: Mapping[str, Any] = next(
            (
                candidate
                for candidate in (config_entry.options, config_entry.data)
                if CONF_USERS in candidate or CONF_SLOTS in candidate
            ),
            {},
        )
        config = EntryConfig.from_mapping(
            {
                **{k: v for k, v in config_entry.data.items() if k not in _MOVED_KEYS},
                CONF_LOCKS: config_entry.options.get(
                    CONF_LOCKS, config_entry.data.get(CONF_LOCKS, [])
                ),
                **{k: v for k, v in side.items() if k in _MOVED_KEYS},
            }
        )

        unplaced = [
            name for name in config.users if config.assignment.slot(name) is None
        ]
        for name in unplaced:
            # Dropped for the reason EntryConfig already skips them: inventing
            # a number would put this user's code on a position somebody else
            # may hold. Named rather than silently discarded.
            _LOGGER.warning(
                "%s (%s): dropping %r, who has no slot number to move",
                config_entry.entry_id,
                config_entry.title,
                name,
            )
        placed = {
            name: fields
            for name, fields in config.users.items()
            if name not in unplaced
        }

        for name, fields in placed.items():
            hass.config_entries.async_add_subentry(
                config_entry,
                ConfigSubentry(
                    data=MappingProxyType(
                        {**dict(fields), CONF_SLOT: config.assignment.slot(name)}
                    ),
                    subentry_type=SUBENTRY_TYPE_USER,
                    title=name,
                    unique_id=None,
                ),
            )

        # The old keys go in the same write that bumps the version. Leaving
        # them would give the entry two answers about who holds which number,
        # and the reader now believes the subentries.
        hass.config_entries.async_update_entry(
            config_entry,
            data={
                k: v for k, v in config_entry.data.items() if k not in _MOVED_KEYS
            },
            options={
                k: v for k, v in config_entry.options.items() if k not in _MOVED_KEYS
            },
            version=5,
        )
        _LOGGER.info(
            "%s (%s): moved %d user(s) into subentries",
            config_entry.entry_id,
            config_entry.title,
            len(placed),
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
            # Join the individual messages, not str(list): the latter iterates
            # the repr's characters and puts a newline between each one.
            errors_str = "\n".join(str(err) for err in errors)
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

    async def _set_condition(service: ServiceCall) -> None:
        """Attach a condition entity to a user."""
        await async_set_condition(
            hass,
            service.data[CONF_NAME],
            service.data[CONF_ENTITY_ID],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CONDITION,
        _set_condition,
        schema=_entry_schema(
            {
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_ENTITY_ID): cv.entity_domain(
                    CONDITION_ENTITY_DOMAINS
                ),
            }
        ),
    )

    async def _clear_condition(service: ServiceCall) -> None:
        """Detach a user's condition entity."""
        await async_clear_condition(
            hass,
            service.data[CONF_NAME],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CONDITION,
        _clear_condition,
        schema=_entry_schema({vol.Required(CONF_NAME): cv.string}),
    )

    async def _set_credential(service: ServiceCall) -> None:
        """Set one of a user's credentials."""
        await async_set_credential(
            hass,
            service.data[CONF_NAME],
            credential_type=service.data[ATTR_CREDENTIAL_TYPE],
            value=service.data[ATTR_VALUE],
            enable_if_disabled=service.data[ATTR_ENABLE_IF_DISABLED],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CREDENTIAL,
        _set_credential,
        schema=_entry_schema(
            {
                vol.Required(CONF_NAME): cv.string,
                # The whole vocabulary, not just what can be stored today.
                # A kind this integration cannot yet write is refused by the
                # handler with a message saying so, which reads better than
                # the schema calling a real credential kind invalid.
                vol.Required(ATTR_CREDENTIAL_TYPE): vol.Coerce(CredentialType),
                # Stripped for the reason every other credential field is: a
                # submitted code is stripped before it is matched, so padding
                # kept here makes a credential nothing typed can match.
                vol.Required(ATTR_VALUE): vol.All(
                    cv.string, vol.Strip, vol.Length(min=1)
                ),
                vol.Optional(ATTR_ENABLE_IF_DISABLED, default=False): cv.boolean,
            }
        ),
    )

    async def _clear_credential(service: ServiceCall) -> None:
        """Clear one of a user's credentials."""
        await async_clear_credential(
            hass,
            service.data[CONF_NAME],
            credential_type=service.data[ATTR_CREDENTIAL_TYPE],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CREDENTIAL,
        _clear_credential,
        schema=_entry_schema(
            {
                vol.Required(CONF_NAME): cv.string,
                vol.Required(ATTR_CREDENTIAL_TYPE): vol.Coerce(CredentialType),
            }
        ),
    )

    async def _enable_user(service: ServiceCall) -> None:
        """Turn a user on."""
        await async_enable_user(
            hass,
            service.data[CONF_NAME],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    async def _disable_user(service: ServiceCall) -> None:
        """Turn a user off."""
        await async_disable_user(
            hass,
            service.data[CONF_NAME],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    for _service_name, _handler in (
        (SERVICE_ENABLE_USER, _enable_user),
        (SERVICE_DISABLE_USER, _disable_user),
    ):
        hass.services.async_register(
            DOMAIN,
            _service_name,
            _handler,
            schema=_entry_schema({vol.Required(CONF_NAME): cv.string}),
        )

    async def _add_user(service: ServiceCall) -> None:
        """Add one or more users to an entry."""
        await async_add_users(
            hass,
            service.data[CONF_USERS],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ADD_USER,
        _add_user,
        schema=vol.All(
            # Before validation, so there is exactly one shape from here on.
            _flat_user_to_list,
            _entry_schema(
                {
                    # ``ensure_list`` so a single user may be given as one
                    # mapping rather than a list of one. NOT vol.Coerce(list),
                    # which on a mapping returns its KEYS -- a user dict would
                    # silently become users named "name" and "pin".
                    vol.Required(CONF_USERS): vol.All(
                        cv.ensure_list, [USER_ENTRY_SCHEMA]
                    ),
                }
            ),
        ),
    )

    async def _delete_user(service: ServiceCall) -> None:
        """Remove one or more users from an entry."""
        await async_delete_users(
            hass,
            service.data[CONF_NAME],
            config_entry_id=service.data.get("config_entry_id"),
            config_entry_title=service.data.get("config_entry_title"),
            clear_credentials=service.data[ATTR_CLEAR_CREDENTIALS],
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_DELETE_USER,
        _delete_user,
        schema=_entry_schema(
            {
                # The field keeps its name, so a released call passing a single
                # string still validates -- it just arrives as a one-item list.
                vol.Required(CONF_NAME): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_CLEAR_CREDENTIALS, default=True): cv.boolean,
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

    async def _use_credential(call: ServiceCall) -> ServiceResponse:
        """Report a credential use and answer whether the code was valid."""
        return await async_use_credential(
            hass,
            call.data[ATTR_CODE],
            source=call.data[ATTR_SOURCE],
            target=call.data[ATTR_TARGET],
            config_entry_id=call.data.get("config_entry_id"),
            config_entry_title=call.data.get("config_entry_title"),
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        _use_credential,
        schema=_entry_schema(
            {
                vol.Required(ATTR_CODE): vol.All(
                    cv.string, vol.Strip, vol.Length(min=1)
                ),
                # Any domain, both of them: a credential is rarely entered
                # on a lock, and what it was used against can be a cover, an
                # alarm panel, or anything else the caller associates it
                # with. Nothing here dereferences either one.
                vol.Required(ATTR_SOURCE): cv.entity_id,
                vol.Required(ATTR_TARGET): cv.entity_id,
            }
        ),
        # OPTIONAL, not ONLY, even though the response is the only thing
        # this produces: ONLY makes Home Assistant reject a caller that
        # omits ``return_response`` outright.
        supports_response=SupportsResponse.OPTIONAL,
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
    # Popped, so it runs once however many entries were migrated. At boot
    # this fires after every entry has, so the renames are complete; on a
    # single entry's reload only that entry's are there to report.
    if moved := hass.data.get(DOMAIN, {}).pop(RENAMES_KEY, None):
        config_entry.async_create_task(
            hass, async_notify_moved(hass, moved), "notify_entity_ids_renamed"
        )

    runtime_data = config_entry.runtime_data
    if not runtime_data.update_listener_registered:
        runtime_data.update_listener_registered = True
        unsub = config_entry.add_update_listener(async_update_listener)

        @callback
        def _clear_listener_registered() -> None:
            runtime_data.update_listener_registered = False
            unsub()

        config_entry.async_on_unload(_clear_listener_registered)

    # Everything is new at setup, so the pass compares against nothing. The
    # data-into-options move this used to do existed only so the listener had
    # something staged to diff; users live in subentries now and the locks
    # need no staging.
    hass.async_create_task(
        _async_apply_entry_update(hass, config_entry, EntryConfig.empty()),
        f"Initial setup for entities for {config_entry.entry_id}",
    )


async def async_setup_entry(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> bool:
    """Set up a config entry."""
    ent_reg = er.async_get(hass)
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

    _async_reclaim_entities_from_foreign_devices(hass, config_entry)
    _async_prune_orphaned_slot_devices(hass, config_entry)

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
    """
    Return True if another (non-disabled, non-ignored) LCM entry manages the lock.

    This asks about entry *presence*, which is what the persistent per-lock
    repair issues care about: they outlive unloads and reloads, so an entry
    that is merely between loads still needs its issue kept. Anything that
    tears down live objects wants ``_lock_managed_by_other_loaded_entry``
    instead.
    """
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


def _lock_still_owned_by_another_entry(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock_entity_id: str,
) -> bool:
    """
    Return True if another entry still holds the shared instance for this lock.

    Teardown is the exact inverse of acquisition, so it asks by calling the
    acquiring side rather than restating its rule: any predicate that drifts
    from ``_find_shared_lock_instance`` leaves an ownerless BaseLock whose
    push subscription and config-entry state listener keep firing, plus a
    second instance beside it on the next setup that doubles every code-slot
    event. Config membership is not the question -- a loaded entry that
    dropped this lock during setup lists it but does not own it.
    """
    return _find_shared_lock_instance(hass, config_entry, lock_entity_id) is not None


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
        if not _lock_still_owned_by_another_entry(hass, config_entry, _lock_entity_id):
            # ``remove_permanently`` discards state that deliberately outlives
            # an unload -- the per-lock setup-failed repair, the provider's
            # stored codes. Only a lock leaving Lock Code Manager altogether
            # should lose those, so an entry that still manages it downgrades
            # this to a plain teardown even while that entry is unloaded.
            await lock.async_unload(
                remove_permanently
                and not _lock_managed_by_other_entry(
                    hass, config_entry, _lock_entity_id
                )
            )
            if lock.coordinator is not None:
                await lock.coordinator.async_shutdown()


async def async_release_locks(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    lock_entity_ids: Iterable[str],
) -> None:
    """
    Take locks out of an entry for good.

    Everything that has to happen when a lock stops being this entry's, in one
    place because there is more than one way for that to happen: the options
    flow reaches it through the update listener's diff, and reauth reaches it
    by swapping a lock out. A second copy of this would drift, and what it
    would drop first is the repair deletion -- the step with no visible
    symptom when it is missed.

    Requires the entry to be loaded. Reauth can run against an entry that
    failed setup, where there is no runtime data to release anything from;
    such an entry never built lock instances, so there is nothing here to do.
    """
    if (runtime_data := getattr(config_entry, "runtime_data", None)) is None:
        return
    for lock_entity_id in lock_entity_ids:
        runtime_data.callbacks.invoke_lock_removed_handlers(lock_entity_id)
        if not _lock_managed_by_other_entry(hass, config_entry, lock_entity_id):
            # A lock that never got an instance has no teardown to clear its
            # repair, and removing it from the entry is exactly what that
            # repair asks the user to do.
            async_delete_issue(
                hass, DOMAIN, per_lock_issue_id("lock_dropped", lock_entity_id)
            )
        # LCM no longer adds its config entry to the lock's device (its
        # per-lock entities link to the device via ``device_entry``), so
        # there is no config-entry association to unmerge here; the per-lock
        # entities are torn down by ``async_unload_lock`` below.
        await async_unload_lock(
            hass, config_entry, lock_entity_id=lock_entity_id, remove_permanently=True
        )


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
    curr_slots = sorted(get_entry_config(config_entry).slot_numbers)
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
    for slot_num in config.slot_numbers:
        async_delete_issue(hass, DOMAIN, f"slot_disabled_{entry_id}_{slot_num}")
        async_delete_issue(hass, DOMAIN, f"pin_required_{entry_id}_{slot_num}")
    for lock_entity_id in config.locks:
        # Only delete per-lock issues if no other LCM entry manages this lock.
        if not _lock_managed_by_other_entry(hass, config_entry, lock_entity_id):
            for issue_key in PER_LOCK_ISSUE_KEYS:
                async_delete_issue(
                    hass, DOMAIN, per_lock_issue_id(issue_key, lock_entity_id)
                )
        for slot_num in config.slot_numbers:
            async_delete_issue(
                hass,
                DOMAIN,
                f"slot_suspended_{entry_id}_{lock_entity_id}_{slot_num}",
            )


@callback
def _async_remove_slot_devices(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    slot_nums: Iterable[int],
) -> None:
    """
    Remove the per-slot devices for slots that are no longer configured.

    Removing a slot's entities is not enough to retire its device: Home
    Assistant's own registry cleanup only reaps devices referenced by
    neither an entity nor a live config entry, and a slot device stays
    attached to this entry forever. Left alone it lingers in the UI with
    no way to delete it short of removing the whole entry (issue #1399).
    """
    dev_reg = dr.async_get(hass)
    for slot_num in slot_nums:
        identifier = build_slot_device_identifier(config_entry.entry_id, slot_num)
        if device := dev_reg.async_get_device(identifiers={(DOMAIN, identifier)}):
            _LOGGER.debug(
                "%s (%s): Removing device for slot %s",
                config_entry.entry_id,
                config_entry.title,
                slot_num,
            )
            dev_reg.async_remove_device(device.id)


@callback
def _async_reclaim_entities_from_foreign_devices(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """
    Move this entry's entities onto its own devices, and drop what is left.

    Before Home Assistant 2026.8 the per-lock entities were attached to the
    lock's own device by reusing that device's identifiers, which also added
    this entry to it. 2026.8 restricts a device to one config entry and split
    every such device in two, leaving a Lock Code Manager-owned copy of the
    lock holding those entities.

    The copy cannot age out on its own: ``dr.async_cleanup`` keeps any device
    referencing a live config entry, whether or not anything is on it. So the
    entities are moved to the slot device they belong to and the emptied copy
    is removed here.

    Runs before the platforms are set up, so a disabled entity -- which no
    platform re-adds -- is moved rather than deleted along with the copy it
    is sitting on. It also runs before the orphaned-slot sweep, so an entity
    whose slot is no longer configured lands on that slot's device and is
    removed with it, instead of the sweep leaving a device behind.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id

    def _is_ours(device: dr.DeviceEntry) -> bool:
        return any(domain == DOMAIN for domain, _ in device.identifiers)

    for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
        if entity.device_id is None:
            continue
        device = dev_reg.async_get(entity.device_id)
        if device is None or _is_ours(device):
            continue
        slot_num = parse_slot_unique_id(entry_id, entity.unique_id)
        if slot_num is None:
            continue
        slot_device = dev_reg.async_get_or_create(
            config_entry_id=entry_id,
            **build_slot_device_info(config_entry, slot_num),
        )
        _LOGGER.debug(
            "%s (%s): Moving %s onto its slot device",
            entry_id,
            config_entry.title,
            entity.entity_id,
        )
        ent_reg.async_update_entity(entity.entity_id, device_id=slot_device.id)

    for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
        if _is_ours(device) or er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        ):
            continue
        _LOGGER.debug(
            "%s (%s): Removing copy of device %s left by the 2026.8 device split",
            entry_id,
            config_entry.title,
            device.name,
        )
        dev_reg.async_remove_device(device.id)


@callback
def _async_remove_hub_device(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """
    Take away the config entry's own device.

    It never held an entity. It existed so the per-user devices could name
    it in ``via_device`` and be drawn beneath it, which bought a line on a
    device page and cost a device on every page that lists them. The users
    are the devices worth having.

    The ``via_device`` goes with it, and had to: Home Assistant reports a
    ``via_device`` naming a device that is not there as a use it intends to
    break, so leaving it behind would log on every registration.
    """
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_device(identifiers={(DOMAIN, config_entry.entry_id)})
    if device is None:
        return
    _LOGGER.debug(
        "%s (%s): Removing the config entry's own device; it holds no entities",
        config_entry.entry_id,
        config_entry.title,
    )
    dev_reg.async_remove_device(device.id)


@callback
def _async_rename_event_unique_ids(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    config: EntryConfig,
) -> None:
    """
    Re-key the event entity from ``pin_used`` to ``credential_used``.

    The key is the last part of the unique ID, so leaving it alone would
    orphan every existing event entity and build a fresh one beside it,
    losing whatever history and settings the old one carried. Updating the
    unique ID keeps the same registry row, and with it the entity ID.

    The event now reports which KIND of credential was used, so naming it
    after one kind was going to read as wrong the moment a second arrived.
    """
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    for slot_num in config.slot_numbers:
        legacy = build_slot_unique_id(entry_id, slot_num, LEGACY_EVENT_PIN_USED)
        if not (entity_id := ent_reg.async_get_entity_id(EVENT_DOMAIN, DOMAIN, legacy)):
            continue
        wanted = build_slot_unique_id(entry_id, slot_num, EVENT_CREDENTIAL_USED)
        if ent_reg.async_get_entity_id(EVENT_DOMAIN, DOMAIN, wanted):
            # Re-keying onto an existing unique ID raises, and a raise here
            # fails the whole migration and leaves the entry unloadable. A
            # duplicate is worth a line in the log; refusing to start is not.
            _LOGGER.warning(
                "Left %s on its old unique id: slot %s already has a %s entity",
                entity_id,
                slot_num,
                EVENT_CREDENTIAL_USED,
            )
            continue
        ent_reg.async_update_entity(entity_id, new_unique_id=wanted)


def _lock_of(entry_id: str, unique_id: str) -> str | None:
    """Return the lock a per-lock entity belongs to, or None if it has none."""
    parts = unique_id.split("|")
    return parts[3] if unique_id.startswith(f"{entry_id}|") and len(parts) > 3 else None


@callback
def _async_purge_dropped_slots(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    dropped: Collection[str],
) -> None:
    """
    Remove the registry rows of slots the migration did not carry across.

    The update listener tears down a slot's entities when it leaves the
    configuration, but it works from the difference between data and options
    and the migration writes the new shape to both -- so from its point of
    view these slots were never there. Left alone they would linger as
    entities named for a user who does not exist, and a device to match.
    """
    slots = {int(slot_num) for slot_num in dropped}
    ent_reg = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id):
        if parse_slot_unique_id(config_entry.entry_id, entity.unique_id) in slots:
            ent_reg.async_remove(entity.entity_id)
    dev_reg = dr.async_get(hass)
    for slot_num in slots:
        identifiers = {
            (DOMAIN, build_slot_device_identifier(config_entry.entry_id, slot_num))
        }
        if device := dev_reg.async_get_device(identifiers):
            dev_reg.async_remove_device(device.id)


@callback
def _async_rename_slot_entity_ids(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    config: EntryConfig,
) -> list[tuple[str, str]]:
    """
    Re-slug every entity ID onto the name of whoever holds the slot.

    Home Assistant only derives an entity ID when the entity is first
    registered, so without this an upgraded install would keep slot-shaped
    IDs forever while a fresh one got name-shaped ones. Recorder history and
    long-term statistics follow the rename: the registry emits
    ``old_entity_id`` and ``recorder.entity_registry`` repoints them.

    The target is rebuilt from what the REGISTRY holds rather than derived
    from the current ID. Deriving it meant reconstructing the old device slug
    from the entry's title, and the title in force now is not necessarily the
    one the entity was created under -- renaming the entry at any point left
    every ID unmatched, so nothing was renamed at all.
    """
    ent_reg = er.async_get(hass)
    entry_id = config_entry.entry_id
    renamed: list[tuple[str, str]] = []
    for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
        slot_num = parse_slot_unique_id(entry_id, entity.unique_id)
        if slot_num is None or not (name := config.name_for(slot_num)):
            continue
        domain, object_id = entity.entity_id.split(".", 1)
        if lock_entity_id := _lock_of(config_entry.entry_id, entity.unique_id):
            # Per-lock entities are one per lock on the SAME slot device, so
            # the user's name alone names them all identically and Home
            # Assistant separates them with a meaningless _2, _3. Their own
            # name carries the lock, and it has to be rebuilt rather than
            # read back: ``original_name`` is the text stored when the entity
            # was created, which on an upgraded install predates this shape.
            suggested = (
                f"{config_entry.title} {name} "
                f"{lock_display_name(hass, lock_entity_id)} "
                f"{PER_LOCK_ENTITY_SUFFIX[entity.unique_id.split('|')[2]]}"
            )
        elif entity.original_name:
            # What Home Assistant would generate today: the device's name
            # followed by the entity's own. ``original_name`` is that name as
            # translated for THIS installation, so a system set up in another
            # language re-slugs into its own words -- and none of it depends
            # on the entry's title, which may have changed since.
            suggested = f"{config_entry.title} {name} {entity.original_name}"
        else:
            # A registry row written by an older Home Assistant may have kept
            # no name at all, so there is nothing to append the way the branch
            # above does. The key inside the unique ID says what the entity is
            # -- it is what the translated name is looked up by -- and unlike
            # the old object id it is there even when the entity was created
            # without a name, which is how the credential-used event was.
            #
            # Guarded on the ID still looking like one this integration
            # generated, so a hand-renamed entity is left alone.
            old_prefix = slugify(f"{config_entry.title} Code slot {slot_num}")
            if object_id != old_prefix and not object_id.startswith(f"{old_prefix}_"):
                continue
            key = entity.unique_id.split("|")[2]
            suggested = f"{config_entry.title} {name} {key.replace('_', ' ')}"
        new_entity_id = ent_reg.async_get_available_entity_id(
            domain, suggested, current_entity_id=entity.entity_id
        )
        if new_entity_id == entity.entity_id:
            continue
        ent_reg.async_update_entity(entity.entity_id, new_entity_id=new_entity_id)
        renamed.append((entity.entity_id, new_entity_id))
    return renamed


@callback
def _async_rename_slot_devices(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """
    Point each slot's device at the name of whoever holds it now.

    DeviceInfo only names a device as it is created, so a rename would
    otherwise leave the old name standing until the entity was rebuilt. It
    runs before the update listener's entity work because the rename that
    matters most -- the name text entity -- writes to data with empty options
    and returns before any of that.

    ``name_by_user`` is untouched, so a device the user renamed themselves
    keeps their name.
    """
    dev_reg = dr.async_get(hass)
    entry_id = config_entry.entry_id
    config = get_entry_config(config_entry)
    for slot_num, name in ((num, config.name_for(num)) for num in config.slot_numbers):
        identifier = build_slot_device_identifier(entry_id, slot_num)
        device = dev_reg.async_get_device(identifiers={(DOMAIN, identifier)})
        # Same shape build_slot_device_info uses, entry title included: a
        # rename that dropped the prefix would leave the device disagreeing
        # with the entity IDs derived from it.
        titled = f"{config_entry.title} {name}" if name else None
        if device is not None and titled and device.name != titled:
            dev_reg.async_update_device(device.id, name=titled)


@callback
def _async_prune_orphaned_slot_devices(
    hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry
) -> None:
    """
    Drop slot devices left behind by a slot that is no longer in config.

    Runs at setup so registries already polluted by the pre-fix behavior get
    cleaned up on the next reload, rather than only newly-removed slots
    benefiting. Sweeps by identifier rather than by diffing against a
    previous config, so it also catches slots removed while the entry was
    unloaded.
    """
    dev_reg = dr.async_get(hass)
    entry_id = config_entry.entry_id
    configured = get_entry_config(config_entry).slot_numbers
    orphaned = {
        slot_num
        for device in dr.async_entries_for_config_entry(dev_reg, entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN
        and (slot_num := parse_slot_device_identifier(entry_id, identifier)) is not None
        and slot_num not in configured
    }
    if orphaned:
        _LOGGER.debug(
            "%s (%s): Pruning devices for unconfigured slots %s",
            entry_id,
            config_entry.title,
            sorted(orphaned),
        )
        _async_remove_slot_devices(hass, config_entry, orphaned)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """
    Allow deleting a slot device from the UI, but only once it is unused.

    Home Assistant renders the device's Delete button only when this hook
    exists, which is why there was previously no way to clear a stale slot
    device (issue #1399). A device whose slot is still configured must stay:
    deleting it would strand the slot's entities, and the next reload would
    recreate it anyway.
    """
    entry_id = config_entry.entry_id
    slot_nums = {
        slot_num
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
        and (slot_num := parse_slot_device_identifier(entry_id, identifier)) is not None
    }
    # No slot identifier means this is the entry's own device, which has to
    # outlive every slot -- it is what the slot devices hang off of.
    if not slot_nums:
        return False
    return slot_nums.isdisjoint(get_entry_config(config_entry).slot_numbers)


@callback
def _async_report_dropped_lock(
    hass: HomeAssistant, lock_entity_id: str, err: UnclaimedLockError
) -> None:
    """
    Raise a repair for a configured lock no provider claims.

    Distinct from ``lock_setup_failed``, which describes a lock that is
    still in the entry with its entities present but unavailable. This
    one is gone: no entities appear for it, nothing is written to it, and
    its only trace was a line in the log.

    Reaching it means the lock stopped resolving to a provider between
    being chosen and being set up -- most likely an entry configured
    before selection-time validation existed, holding an mqtt lock whose
    bridge no provider speaks. Nobody ever told those users, so the
    repair names the entity and quotes what refused it.

    Narrowly typed on purpose. The text diagnoses an unsupported bridge and
    asks the user to remove the entity, which is only the right advice for
    that one failure; raised on any exception that escaped setup it told
    users with a perfectly good lock and a provider bug to throw the lock out.
    """
    async_create_issue(
        hass,
        DOMAIN,
        per_lock_issue_id("lock_dropped", lock_entity_id),
        is_fixable=False,
        is_persistent=True,
        severity=IssueSeverity.ERROR,
        translation_key="lock_dropped",
        translation_placeholders={
            "lock_entity_id": lock_entity_id,
            "error": str(err),
        },
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
        if isinstance(result, asyncio.CancelledError):
            # ``return_exceptions=True`` aggregates a cancelled child rather
            # than propagating it, so cancellation has to be re-raised by hand
            # or Home Assistant shutting down looks exactly like a lock that
            # could not be set up -- and left a persistent repair blaming an
            # unsupported bridge, surviving the restart that cleared the
            # actual condition.
            raise result
        if isinstance(result, BaseException):
            # Transport failures degrade inside async_setup_internal, and
            # structural validation failures are logged there and kept
            # degraded, so a popped lock is either a genuine bug or a lock
            # whose provider stopped resolving between selection and setup --
            # an mqtt device re-discovered under an unrecognized bridge. The
            # config flow refuses that selection, so it cannot arrive here
            # from the selection path.
            _LOGGER.error(
                "%s (%s): Failed to set up lock %s: %s",
                entry_id,
                entry_title,
                lock_entity_id,
                result,
                exc_info=result,
            )
            runtime_data.locks.pop(lock_entity_id, None)
            if isinstance(result, UnclaimedLockError):
                # Only this failure has a diagnosis and an action to offer.
                # Any other exception is a bug in a provider LCM does speak,
                # and a repair telling that user their bridge is unsupported
                # sends them to remove a lock that works.
                _async_report_dropped_lock(hass, lock_entity_id, result)
            continue

        added_locks.append(result)
        async_delete_issue(
            hass, DOMAIN, per_lock_issue_id("lock_dropped", lock_entity_id)
        )

        if not await result.async_internal_is_reachable():
            _LOGGER.debug(
                "%s (%s): Lock %s is not connected yet. Entities will be created "
                "but will be unavailable until the lock comes online. This is normal "
                "during startup if Z-Wave JS is still initializing.",
                entry_id,
                entry_title,
                result.lock.entity_id,
            )

        for slot_num in new_config.slot_numbers:
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
    """
    Update listener.

    Wraps the pass so ``runtime_data.settled`` is set however it ends,
    including the early return and a failure. Anything waiting on it is
    waiting to learn that the entry has finished reacting, and a pass that
    raised has finished reacting as much as it is going to.
    """
    try:
        await _async_apply_entry_update(hass, config_entry)
    finally:
        config_entry.runtime_data.settled.set()


async def _async_apply_entry_update(
    hass: HomeAssistant,
    config_entry: LockCodeManagerConfigEntry,
    old_config: EntryConfig | None = None,
) -> None:
    """Bring entities, devices and locks into line with the entry."""
    # Refresh the cached EntryConfig on EVERY update — including entity-driven
    # writes that go straight to data with empty options (e.g. a slot's name or
    # PIN being edited via its text entity). The early-return below skips the
    # entity-creation pass for those cases, but downstream readers via
    # runtime_data.config still need to see the current data.
    runtime_data = config_entry.runtime_data
    # What the entry looked like when this pass started, captured before the
    # refresh below overwrites it. Users live in subentries and locks may be
    # on either side, so comparing the cached view against the current one
    # is the only comparison that sees both.
    # ``None`` means "compare against what we last saw". Setup passes an empty
    # config instead, because at that point nothing has been created yet and
    # every user is new -- diffing against the cache would find no changes and
    # build nothing.
    if old_config is None:
        old_config = runtime_data.config
    runtime_data.config = new_config = EntryConfig.from_entry(config_entry)

    # Notify per-slot coordinators so derived "active" state and condition-
    # entity subscriptions stay in sync with the refreshed config view.
    # Runs on both the entity-driven path (early return below) and the
    # options-flow path so a calendar/condition swap is picked up the
    # same way.
    for coordinator in runtime_data.slot_coordinators.values():
        coordinator.notify_config_changed()

    _async_rename_slot_devices(hass, config_entry)

    # Nothing changed means nothing to do -- which also makes this its own
    # recursion guard, because the settle write at the end of this function
    # produces no diff when it re-enters.
    diff = EntryConfigDiff(old=old_config, new=new_config)
    if not diff.has_changes:
        return

    ent_reg = er.async_get(hass)

    entry_id = config_entry.entry_id
    entry_title = config_entry.title
    _LOGGER.info("%s (%s): Creating and/or updating entities", entry_id, entry_title)

    setup_tasks = runtime_data.setup_tasks

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
        # After the entity removers have run, so the registry teardown order
        # matches a normal removal (entities first, then their device).
        _async_remove_slot_devices(hass, config_entry, slots_to_remove)
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
    # Drained, not read: a hand-off applies to the write that requested it,
    # and leaving the pair behind would spare the next occupant of that slot
    # number the cleanup it does need.
    retained_pairs = runtime_data.retained_pairs
    runtime_data.retained_pairs = set()
    for lock_entity_id, slot_num in diff.pairs_removed:
        release_lock = runtime_data.locks.get(lock_entity_id)
        if release_lock is None:
            continue
        if (lock_entity_id, slot_num) in retained_pairs:
            _LOGGER.info(
                "%s (%s): leaving slot %s on lock %s programmed; it is no longer "
                "managed here",
                entry_id,
                entry_title,
                slot_num,
                lock_entity_id,
            )
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
    await async_release_locks(hass, config_entry, locks_to_remove)

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
