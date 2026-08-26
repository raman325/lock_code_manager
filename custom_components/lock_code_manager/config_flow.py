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
    CONF_MEMBERS,
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
from .domain.config import EntryConfig, declare_codeless
from .domain.names import name_error, normalize_name, validate_user_names
from .domain.queries import get_entry_config
from .domain.slot_assignment import CONF_SLOT_ASSIGNMENT, SlotAssignment
from .providers import resolve_provider_class_for_entity

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

# Every lock entity, from any integration. Not an allowlist of the
# platforms that resolve to a provider: a lock nothing claims may still be
# one Lock Code Manager can manage codes for by holding them itself, and the
# user is asked about exactly those after they submit. Filtering them out of
# the picker instead would make the question unaskable -- a lock that cannot
# be selected cannot be declared about.
LOCK_ENTITY_SELECTOR = sel.EntitySelector(
    sel.EntitySelectorConfig(domain=LOCK_DOMAIN, multiple=True)
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


def _codeless_candidates(
    hass: HomeAssistant, lock_entity_ids: Iterable[str]
) -> list[er.RegistryEntry]:
    """
    Return the selected locks that only a declaration can account for.

    A lock no provider claims is not necessarily a lock this integration
    cannot manage: if it keeps no codes of its own, Lock Code Manager can
    hold them itself and treat the entity as the thing being locked. Which
    of the two it is, is not derivable from anything here -- so these are
    the locks the user is asked about.

    mqtt is left out, and keeps the refusal it already has. Its dispatch is
    per DEVICE, so an unclaimed one means "this bridge is not one of the two
    Lock Code Manager speaks" -- a gap that may close in a later version,
    not a statement that the lock has no code storage. Offering the
    declaration there answers a question nobody asked, and taking it would
    strand that lock's credentials in a Lock Code Manager store on the day
    its bridge became supported.

    An entity missing from the registry is left out too: a declaration is
    keyed by registry id, so there is nothing to key one by, and setup
    refuses that lock on its own.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    return [
        lock_entry
        for entity_id in lock_entity_ids
        if (lock_entry := ent_reg.async_get(entity_id)) is not None
        and lock_entry.platform != MQTT_DOMAIN
        and resolve_provider_class_for_entity(dev_reg, lock_entry) is None
    ]


class CodelessDeclarationFlow(config_entries.ConfigEntryBaseFlow):
    """
    The step that asks whether Lock Code Manager should hold a lock's codes.

    Mixed into every flow that picks locks, because any of them can pick one
    no provider claims, and none of them may store a lock nobody was asked
    about.

    The question is asked after a submission has otherwise been accepted,
    and the submission is held rather than acted on. Either answer then
    re-submits it: the answer changes what the same validation makes of the
    same input, so confirming saves through exactly the path that would have
    run without the detour, and declining lands back on the form with the
    refusal that names the locks -- somewhere the lock selection can be
    changed, rather than a dead end.

    Asked about every lock nothing claims, not only the ones nobody has
    answered for yet, so a declaration made earlier can be taken back. Once
    per flow, because an answer suppresses the question for the re-submission
    it caused.

    Based on the class the config and options flows already share, so the
    steps here are typed against the same flow surface they run on rather
    than asserting one exists.
    """

    def __init__(self) -> None:
        """Start with nothing asked and nothing answered."""
        super().__init__()
        # Answers given in THIS flow, keyed by entity registry id, laid over
        # whatever the entry already holds. They reach storage only when the
        # flow that collected them writes its entry.
        self._codeless_answers: dict[str, bool] = {}
        # The step the pending answer belongs to, and what was submitted to
        # it.
        self._codeless_origin: str = ""
        self._codeless_input: dict[str, Any] = {}
        self._codeless_locks: list[er.RegistryEntry] = []

    async def _async_check_codeless(
        self, origin: str, user_input: dict[str, Any], config: EntryConfig
    ) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, Any]]:
        """
        Ask about, or refuse, the locks in a submission that nothing claims.

        Returns the flow result to return when the question still has to be
        asked, alongside the form error and placeholders for a submission
        that cannot be saved as it stands. ``config`` is what the entry
        already declares, which this flow's own answers override.
        """
        candidates = _codeless_candidates(self.hass, user_input[CONF_LOCKS])
        if unanswered := [
            lock_entry
            for lock_entry in candidates
            if lock_entry.id not in self._codeless_answers
        ]:
            self._codeless_origin = origin
            # Copied: the step this comes back to consumes what it is given.
            self._codeless_input = dict(user_input)
            self._codeless_locks = unanswered
            return await self.async_step_codeless(), {}, {}
        if undeclared := [
            lock_entry
            for lock_entry in candidates
            if not self._is_codeless(config, lock_entry)
        ]:
            return (
                None,
                {CONF_LOCKS: "codeless_declined"},
                {"locks": ", ".join(entry.entity_id for entry in undeclared)},
            )
        return None, {}, {}

    def _is_codeless(self, config: EntryConfig, lock_entry: er.RegistryEntry) -> bool:
        """Return the answer that stands for a member: this flow's, else the entry's."""
        return self._codeless_answers.get(lock_entry.id, config.is_codeless(lock_entry))

    def _declared_members(self, config: EntryConfig) -> dict[str, dict[str, Any]]:
        """Return what to store about this entry's members, answers applied."""
        return declare_codeless(config.members, self._codeless_answers)

    async def async_step_codeless(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Explain what declaring these locks means, and offer both answers."""
        return self.async_show_menu(
            step_id="codeless",
            menu_options=["codeless_confirm", "codeless_decline"],
            description_placeholders={
                "locks": ", ".join(entry.entity_id for entry in self._codeless_locks)
            },
        )

    async def async_step_codeless_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record that Lock Code Manager holds these locks' credentials."""
        return await self._async_answer_codeless(True)

    async def async_step_codeless_decline(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record that it does not, which is also how a declaration is undone."""
        return await self._async_answer_codeless(False)

    async def _async_answer_codeless(self, codeless: bool) -> dict[str, Any]:
        """Record one answer for every lock the step named, and re-submit."""
        self._codeless_answers.update(
            dict.fromkeys((entry.id for entry in self._codeless_locks), codeless)
        )
        # Resolved by name, the way the flow manager resolves every step.
        return await getattr(self, f"async_step_{self._codeless_origin}")(
            dict(self._codeless_input)
        )


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


class LockCodeManagerFlowHandler(
    CodelessDeclarationFlow, config_entries.ConfigFlow, domain=DOMAIN
):
    """Config flow for Lock Code Manager."""

    VERSION = 4
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize config flow."""
        super().__init__()
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
                # A new entry declares nothing yet, so every lock nothing
                # claims is one this step has to ask about. Asked before the
                # name is consumed, because a refusal re-renders this form
                # from what was submitted.
                (
                    ask,
                    errors,
                    description_placeholders,
                ) = await self._async_check_codeless(
                    "user", user_input, EntryConfig.empty()
                )
                if ask is not None:
                    return ask
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
        return self._create_entry()

    def _create_entry(self) -> dict[str, Any]:
        """
        Create the entry, carrying what this flow was told about its members.

        The key is left out entirely when nothing was declared, rather than
        written empty: an entry that was never asked anything should not
        read as one that answered nothing.
        """
        if members := self._declared_members(EntryConfig.empty()):
            self.data[CONF_MEMBERS] = members
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
                return self._create_entry()

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
            if not additional_errors:
                # Reauth renders the same picker as the other two flows, so
                # it is a third way to put a lock nothing claims into the
                # entry, and needs the same question. Left out, a user
                # repairing one broken lock could swap in a codeless one and
                # land straight back in reauth.
                (
                    ask,
                    additional_errors,
                    additional_placeholders,
                ) = await self._async_check_codeless(
                    "reauth_confirm", user_input, entry_config
                )
                if ask is not None:
                    return ask
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
                        **entry_config.to_dict(),
                        **user_input,
                        CONF_MEMBERS: self._declared_members(entry_config),
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


class LockCodeManagerOptionsFlow(CodelessDeclarationFlow, config_entries.OptionsFlow):
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
                    # Asked here, where the submission is otherwise ready to
                    # save, so nobody is asked to declare something about a
                    # submission that was never going to be stored.
                    (
                        ask,
                        codeless_errors,
                        codeless_placeholders,
                    ) = await self._async_check_codeless("init", user_input, config)
                    if ask is not None:
                        return ask
                    errors.update(codeless_errors)
                    description_placeholders.update(codeless_placeholders)
                    if not errors:
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
                                members=self._declared_members(config),
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
