"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Container, Iterable, Mapping, Sequence
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.mqtt import DOMAIN as MQTT_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector as sel,
)
from homeassistant.util import slugify

from .const import (
    CONDITION_ENTITY_DOMAINS,
    CONF_LOCKS,
    CONF_NUM_USERS,
    CONF_USERS,
    DEFAULT_NUM_USERS,
    DOMAIN,
    EXCLUDED_CONDITION_PLATFORMS,
)
from .domain.allocation import (
    SlotAllocationError,
    async_allocate_for,
    async_check_slot_capacity,
)
from .domain.config import EntryConfig
from .domain.names import name_error, normalize_name, validate_user_names
from .domain.queries import get_entry_config
from .domain.slot_assignment import CONF_SLOT_ASSIGNMENT, SlotAssignment
from .providers import CONFIG_FLOW_PLATFORMS, resolve_provider_class_for_entity

_LOGGER = logging.getLogger(__name__)

# Stripped where it enters rather than where it is read: every consumer
# compares a submitted code stripped, so a PIN stored with padding is one
# nothing anybody can type will ever match. Not vol.Length(min=1) -- an
# empty PIN is how a user without one is expressed.
STRIPPED_PIN = vol.All(cv.string, str.strip)

CODE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_PIN): STRIPPED_PIN,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_CONDITION): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=CONDITION_ENTITY_DOMAINS)
        ),
    }
)


def _slot_enabled_without_pin(slot: dict[str, Any]) -> bool:
    """Return True if a slot is enabled but has no PIN set."""
    return bool(slot.get(CONF_ENABLED)) and not slot.get(CONF_PIN)


def enabled_requires_pin(data: dict[str, Any]) -> dict[str, Any]:
    """Validate that if enabled is True, pin is set."""
    if any(_slot_enabled_without_pin(val) for val in data.values()):
        raise vol.Invalid("PIN must be set if enabled is True")
    return data


# The editor's shape: users keyed by name, with no slot number anywhere. The
# name is the key rather than a field, so a duplicate is unrepresentable
# rather than rejected -- the same reason the stored configuration is keyed
# that way.
USER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_PIN): STRIPPED_PIN,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_CONDITION): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=CONDITION_ENTITY_DOMAINS)
        ),
    }
)

USERS_SCHEMA = vol.All(vol.Schema({cv.string: USER_SCHEMA}), enabled_requires_pin)

LOCKS_FILTER_CONFIG = [
    sel.EntityFilterSelectorConfig(integration=platform, domain=LOCK_DOMAIN)
    for platform in CONFIG_FLOW_PLATFORMS
]
LOCK_ENTITY_SELECTOR = sel.EntitySelector(
    sel.EntitySelectorConfig(filter=LOCKS_FILTER_CONFIG, multiple=True)
)
SLOTS_YAML_SELECTOR = sel.ObjectSelector(sel.ObjectSelectorConfig())


POSITIVE_INT = vol.All(vol.Coerce(int), vol.Range(min=1))


def _check_unclaimed_mqtt_locks(
    hass: HomeAssistant,
    lock_entity_ids: Iterable[str],
    already_configured: Container[str] = (),
) -> tuple[dict[str, str], dict[str, Any]]:
    """
    Turn any newly selected unclaimed mqtt lock into the form error that names it.

    The entity selector filter can only express integration+domain, so
    per-device dispatch has to be enforced here at submit time -- otherwise
    an unclaimed mqtt lock is accepted and only refused at setup.

    ``already_configured`` is what the entry holds now, and locks in it are
    waved through. An entry configured before this check existed can be
    carrying an unclaimed lock, and every options and reauth submission
    re-renders that entry's whole lock list -- so validating all of it made
    one grandfathered lock refuse every subsequent edit: no PIN could be
    changed and no reauth could complete, for a lock the form was not being
    asked to add. That lock is not silently accepted either; it is dropped at
    setup and says so in its own repair.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    unclaimed = [
        entity_id
        for entity_id in lock_entity_ids
        if entity_id not in already_configured
        and (entry := ent_reg.async_get(entity_id)) is not None
        and entry.platform == MQTT_DOMAIN
        and resolve_provider_class_for_entity(dev_reg, entry) is None
    ]
    if unclaimed:
        return {CONF_LOCKS: "unsupported_mqtt_lock"}, {"locks": ", ".join(unclaimed)}
    return {}, {}


def _check_common_slots(
    hass: HomeAssistant,
    locks: Iterable[str],
    slots_list: Iterable[int | str],
    config_entry: ConfigEntry | None = None,
) -> tuple[dict, dict]:
    """Check if slots are already configured."""
    try:
        lock, common_slots, entry_title = next(
            (lock, common_slots, entry.title)
            for lock in locks
            for entry in hass.config_entries.async_entries(DOMAIN)
            if (config := get_entry_config(entry)).has_lock(lock)
            and (
                common_slots := sorted(
                    config.slot_numbers & {int(s) for s in slots_list}
                )
            )
            and not (config_entry and config_entry == entry)
        )
    except StopIteration:
        return {}, {}
    else:
        return {"base": "slots_already_configured"}, {
            "common_slots": ", ".join(str(slot) for slot in common_slots),
            "lock": lock,
            "entry_title": entry_title,
        }


async def _async_validate_users_yaml(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    raw_users: dict[Any, Any],
    locks: Iterable[str],
) -> tuple[dict | None, dict, dict]:
    """
    Validate a users submission for the editor and the yaml setup path.

    Slot numbers are not part of this shape at all: the editor names users,
    and the numbers are allocated afterwards from whatever the locks leave
    free. What can still fail is a name that is empty or that means the same
    person as another, and a count of users no lock could hold.

    ``config_entry`` is the entry the submission is for, or ``None`` from
    the flow that is still creating one; it is what the lock reads this
    performs are made on behalf of.

    Returns the parsed users -- or ``None`` when validation failed -- with the
    accumulated errors and description placeholders.
    """
    # A block still keyed by slot number coerces cleanly into users named
    # "1", "2", which is a silently wrong reading of what was pasted.
    if raw_users and all(isinstance(key, int) for key in raw_users):
        return None, {"base": "users_keyed_by_slot"}, {}

    try:
        parsed_users = USERS_SCHEMA(raw_users)
    except vol.Invalid as err:
        _LOGGER.error("Invalid users: %s", err)
        return None, {"base": "invalid_config"}, {}

    if problem := validate_user_names(parsed_users):
        name, error = problem
        return None, {"base": error}, {"name": name}

    # The same refusal the guided path gives. Both write the one field, so a
    # condition entity this integration cannot read must fail on both routes
    # or the editor becomes a way around the check.
    ent_reg = er.async_get(hass)
    for name, fields in parsed_users.items():
        if not (condition := fields.get(CONF_CONDITION)):
            continue
        entity_entry = ent_reg.async_get(condition)
        if entity_entry and entity_entry.platform in EXCLUDED_CONDITION_PLATFORMS:
            return (
                None,
                {"base": "excluded_platform"},
                {
                    "name": name,
                    "integration": entity_entry.platform,
                    "docs_url": (
                        "https://github.com/raman325/lock_code_manager/wiki/"
                        "Unsupported-Condition-Entity-Integrations"
                    ),
                },
            )

    # The count is what a lock has to hold, now that nobody picks a number.
    try:
        await async_check_slot_capacity(hass, config_entry, locks, [len(parsed_users)])
    except SlotAllocationError as err:
        return (
            None,
            {"base": "too_many_users"},
            {**err.placeholders, "num_users": str(len(parsed_users))},
        )
    return parsed_users, {}, {}


async def _allocate_for(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    locks: Sequence[str],
    num_users: int,
) -> tuple[frozenset[int] | None, dict[str, str], dict[str, Any]]:
    """
    Find numbers for ``num_users``, or say why it could not.

    Allocation itself lives in ``domain.allocation``, which the services call
    too; this only turns its refusals into the form errors a flow renders.

    ``config_entry`` is the entry being allocated for, whose own numbers do
    not constrain it: kept ones are held by tenure, released ones are free
    for whoever comes next. A flow that is still creating its entry passes
    ``None``, which by the same rule holds nothing.
    """
    try:
        unavailable = await async_allocate_for(hass, config_entry, locks, num_users)
    except SlotAllocationError as err:
        return None, {"base": err.translation_key}, err.placeholders
    return unavailable, {}, {}


class LockCodeManagerFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Lock Code Manager."""

    VERSION = 4
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize config flow."""
        self.data: dict[str, Any] = {}
        self.title: str = ""
        self.ent_reg: er.EntityRegistry = None
        self.dev_reg: dr.DeviceRegistry = None
        self._users_to_configure = 0
        # The numbers allocation must avoid, settled when the user said how
        # many users they wanted.
        self._unavailable: frozenset[int] = frozenset()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle a flow initialized by the user."""
        if not self.ent_reg:
            self.ent_reg = er.async_get(self.hass)
        if not self.dev_reg:
            self.dev_reg = dr.async_get(self.hass)

        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {}
        if user_input is not None:
            # Checked before the step consumes anything from user_input: a
            # refusal re-renders this form from what was submitted, and the
            # name has to still be in there when it does.
            errors, description_placeholders = _check_unclaimed_mqtt_locks(
                self.hass, user_input[CONF_LOCKS]
            )
            if not errors:
                self.title = user_input.pop(CONF_NAME)
                await self.async_set_unique_id(slugify(self.title))
                self._abort_if_unique_id_configured()
                self.data = user_input
                return await self.async_step_choose_path()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_LOCKS): LOCK_ENTITY_SELECTOR,
                    }
                ),
                user_input,
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=False,
        )

    async def async_step_choose_path(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Allow user to choose a path for configuration."""
        return self.async_show_menu(step_id="choose_path", menu_options=["ui", "yaml"])

    async def async_step_ui(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ask how many users to configure."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {}
        if user_input is not None:
            num_users = user_input[CONF_NUM_USERS]
            # Settled before a single name is collected. Which users get
            # configured does not change which numbers allocation issues, so
            # the count alone decides whether they fit -- and a refusal here
            # is one the user can still act on. The ``None`` is the entry the
            # lock reads are made for: this flow is what creates it.
            (
                unavailable,
                errors,
                description_placeholders,
            ) = await _allocate_for(self.hass, None, self.data[CONF_LOCKS], num_users)
            if unavailable is not None:
                self._users_to_configure = num_users
                self._unavailable = unavailable
                return await self.async_step_code_slot()

        return self.async_show_form(
            step_id="ui",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(
                            CONF_NUM_USERS, default=DEFAULT_NUM_USERS
                        ): POSITIVE_INT,
                    }
                ),
                # A refusal comes back to this form, so it comes back holding
                # what was refused rather than making the user find it again.
                user_input,
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=False,
        )

    async def async_step_code_slot(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Collect one user's configuration, repeating until the count is met."""
        errors: dict[str, str] = {}
        self.data.setdefault(CONF_USERS, {})
        configured = len(self.data[CONF_USERS])
        description_placeholders: dict[str, Any] = {
            "user_num": configured + 1,
            "num_users": self._users_to_configure,
        }

        if user_input is not None:
            if _slot_enabled_without_pin(user_input):
                errors[CONF_PIN] = "missing_pin_if_enabled"

            if error := name_error(user_input.get(CONF_NAME)):
                errors[CONF_NAME] = error
            else:
                # Normalize BOTH sides. Comparing a stripped candidate against
                # unstripped stored names lets "Raman " and "Raman" both
                # through in one order but not the other.
                user_input[CONF_NAME] = normalize_name(user_input[CONF_NAME])
                if any(
                    name.casefold() == user_input[CONF_NAME].casefold()
                    for name in self.data[CONF_USERS]
                ):
                    errors[CONF_NAME] = "name_not_unique"

            # A single registry lookup covers the excluded-platform check.
            # self.ent_reg is set in async_step_user, which always runs first.
            if entity_id := user_input.get(CONF_CONDITION):
                entity_entry = self.ent_reg.async_get(entity_id)
                if (
                    entity_entry
                    and entity_entry.platform in EXCLUDED_CONDITION_PLATFORMS
                ):
                    errors[CONF_CONDITION] = "excluded_platform"
                    description_placeholders["integration"] = entity_entry.platform
                    description_placeholders["docs_url"] = (
                        "https://github.com/raman325/lock_code_manager/wiki/"
                        "Unsupported-Condition-Entity-Integrations"
                    )

            if not errors:
                validated = CODE_SLOT_SCHEMA(user_input)
                name = validated.pop(CONF_NAME)
                self.data[CONF_USERS][name] = validated
                configured = len(self.data[CONF_USERS])
                if configured >= self._users_to_configure:
                    return await self._async_finish_ui_setup()
                description_placeholders["user_num"] = configured + 1

        return self.async_show_form(
            step_id="code_slot",
            data_schema=CODE_SLOT_SCHEMA,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=len(self.data[CONF_USERS]) + 1 == self._users_to_configure,
        )

    async def _async_finish_ui_setup(self) -> dict[str, Any]:
        """Assign slots to the collected users and create the entry."""
        # Both refusals -- unreadable locks and a count that will not fit --
        # were settled in async_step_ui, against the same occupancy and the
        # same count. Nothing collected since can change either answer.
        assignment = SlotAssignment.empty().reconcile(
            self.data[CONF_USERS], start=1, unavailable=self._unavailable
        )
        self.data[CONF_SLOT_ASSIGNMENT] = dict(assignment.slots)
        return self.async_create_entry(title=self.title, data=self.data)

    async def async_step_yaml(self, user_input: dict[str, Any] | None = None):
        """Take a block of users, then allocate their numbers."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {}
        if not user_input:
            user_input = {}
        if user_input:
            (
                users,
                validation_errors,
                validation_placeholders,
            ) = await _async_validate_users_yaml(
                self.hass, None, user_input[CONF_USERS], self.data[CONF_LOCKS]
            )
            errors.update(validation_errors)
            description_placeholders.update(validation_placeholders)

            if not errors:
                assert users is not None
                # Same allocation the guided path uses, against the same
                # occupancy: nobody picks a number on either route, so
                # neither can land on one a lock already holds. The ``None``
                # is the entry the lock reads are made for, which this flow
                # has not created yet.
                (
                    unavailable,
                    allocation_errors,
                    allocation_placeholders,
                ) = await _allocate_for(
                    self.hass, None, self.data[CONF_LOCKS], len(users)
                )
                if unavailable is None:
                    return self.async_show_form(
                        step_id="yaml",
                        data_schema=self._users_schema(user_input),
                        errors=allocation_errors,
                        description_placeholders=allocation_placeholders,
                        last_step=True,
                    )
                self.data[CONF_USERS] = users
                assignment = SlotAssignment.empty().reconcile(
                    users, start=1, unavailable=unavailable
                )
                self.data[CONF_SLOT_ASSIGNMENT] = dict(assignment.slots)
                return self.async_create_entry(title=self.title, data=self.data)

        return self.async_show_form(
            step_id="yaml",
            data_schema=self._users_schema(user_input),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=True,
        )

    @staticmethod
    def _users_schema(user_input: dict[str, Any]) -> vol.Schema:
        """Return the users editor, redisplaying whatever was submitted."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_USERS, default=user_input.get(CONF_USERS, {})
                ): SLOTS_YAML_SELECTOR,
            }
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any] | None = None):
        """
        Entry point for reauth. Home Assistant passes the ENTRY'S OWN DATA here.

        Delegated immediately, and the argument ignored, so the confirm step
        can read ``user_input is None`` as "render the form" rather than
        inspecting the payload to guess whether its caller was Home Assistant
        or the user. Guessing wrong updates the entry and reloads it.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """
        Handle the reauth form.

        Reached only through :meth:`async_step_reauth`, so ``user_input is
        None`` unambiguously means "render the form".
        """
        config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        assert config_entry
        errors = {}
        description_placeholders = {
            **self.context["title_placeholders"],
            "lock": self.context["lock_entity_id"],
        }

        if user_input is None:
            # Either the frontend re-invoking the step to render the form, or
            # the initial call carrying the entry's data. Seed the lock
            # selector from the entry's current config either way.
            user_input = {CONF_LOCKS: list(get_entry_config(config_entry).locks)}
        else:
            entry_config = get_entry_config(config_entry)
            existing_slots = entry_config.slot_numbers
            additional_errors, additional_placeholders = _check_unclaimed_mqtt_locks(
                self.hass, user_input[CONF_LOCKS], entry_config.locks
            )
            if not additional_errors:
                additional_errors, additional_placeholders = _check_common_slots(
                    self.hass,
                    user_input[CONF_LOCKS],
                    existing_slots,
                    config_entry,
                )
            if not additional_errors:
                # Reauth is where a lock gets swapped, so it is also where an
                # already-valid slot set can become too large for the new lock.
                try:
                    await async_check_slot_capacity(
                        self.hass, config_entry, user_input[CONF_LOCKS], existing_slots
                    )
                except SlotAllocationError as err:
                    additional_errors = {"base": err.translation_key}
                    additional_placeholders = err.placeholders
            errors.update(additional_errors)
            description_placeholders.update(additional_placeholders)
            if not errors:
                # Consume any options-flow save that sat unprocessed while
                # the entry was failed (no update listener registered in
                # that state) and clear it: the data→options migration
                # merges options-preferred, so leaving stale options in
                # place would silently override this reauth fix on the
                # next load.
                # Resolved through EntryConfig, not by merging raw dicts: the
                # two sides may be in different shapes, and a raw merge carries
                # both, discarding the very save this block exists to consume.
                self.hass.config_entries.async_update_entry(
                    config_entry,
                    data={
                        **get_entry_config(config_entry).to_dict(),
                        **user_input,
                    },
                    options={},
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(config_entry.entry_id),
                    f"Reload config entry {config_entry.entry_id}",
                )
                return self.async_abort(reason="locks_updated")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOCKS, default=user_input[CONF_LOCKS]
                    ): LOCK_ENTITY_SELECTOR
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        """Get options flow."""
        return LockCodeManagerOptionsFlow()


class LockCodeManagerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Lock Code Manager."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Edit the entry's users. Numbers are not part of this."""
        errors: dict[str, str] = {}
        description_placeholders: dict[str, Any] = {}
        if not user_input:
            user_input = {}

        if user_input:
            # Accumulated alongside the users validation rather than
            # short-circuiting it: both refusals render together, so one round
            # trip shows everything wrong with the submission.
            lock_errors, lock_placeholders = _check_unclaimed_mqtt_locks(
                self.hass,
                user_input[CONF_LOCKS],
                get_entry_config(self.config_entry).locks,
            )
            errors.update(lock_errors)
            description_placeholders.update(lock_placeholders)

            (
                users,
                validation_errors,
                validation_placeholders,
            ) = await _async_validate_users_yaml(
                self.hass,
                self.config_entry,
                user_input[CONF_USERS],
                user_input[CONF_LOCKS],
            )
            errors.update(validation_errors)
            description_placeholders.update(validation_placeholders)

            if not errors:
                assert users is not None
                (
                    unavailable,
                    allocation_errors,
                    allocation_placeholders,
                ) = await _allocate_for(
                    self.hass,
                    self.config_entry,
                    user_input[CONF_LOCKS],
                    len(users),
                )
                if unavailable is None:
                    errors.update(allocation_errors)
                    description_placeholders.update(allocation_placeholders)
                else:
                    config = get_entry_config(self.config_entry)
                    # Reconciled against what the entry already holds, so a
                    # user who was here before keeps their number and only
                    # newcomers are issued one. Moving somebody would rewrite
                    # their credential on every lock.
                    assignment = config.assignment.reconcile(
                        users, start=1, unavailable=unavailable
                    )
                    # Written through EntryConfig so whatever else the entry
                    # carries survives the edit. Building the dict by hand
                    # drops every key this form does not ask about.
                    return self.async_create_entry(
                        title="",
                        data=EntryConfig(
                            locks=tuple(user_input[CONF_LOCKS]),
                            members=config.members,
                            users=users,
                            assignment=assignment,
                            extra=config.extra,
                        ).to_dict(),
                    )

        config = get_entry_config(self.config_entry)
        # Plain dict/list, because the form selectors cannot serialize the
        # deeply read-only mappings EntryConfig uses internally.
        defaults = {
            CONF_LOCKS: list(config.locks),
            CONF_USERS: {name: dict(user) for name, user in config.users.items()},
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOCKS,
                        default=user_input.get(CONF_LOCKS, defaults[CONF_LOCKS]),
                    ): LOCK_ENTITY_SELECTOR,
                    vol.Required(
                        CONF_USERS,
                        default=user_input.get(CONF_USERS, defaults[CONF_USERS]),
                    ): SLOTS_YAML_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=True,
        )
