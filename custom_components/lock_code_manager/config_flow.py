"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Container, Iterable, Mapping, Sequence
import logging
from typing import Any, NamedTuple

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
    CONF_CODELESS_LOCKS,
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
from .providers import CONFIG_FLOW_PLATFORMS, resolve_provider_class_for_entity
from .providers.codeless import CodelessLock

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

# The locks a provider can be resolved for. Wider than what actually
# resolves, because mqtt dispatch is per DEVICE and a selector can only
# express an integration -- an unrecognized bridge is refused at submit
# time. Nothing is hidden by narrowing this: the field beside it offers
# exactly the locks no provider claims.
LOCKS_FILTER_CONFIG = [
    sel.EntityFilterSelectorConfig(integration=platform, domain=LOCK_DOMAIN)
    for platform in CONFIG_FLOW_PLATFORMS
]
LOCK_ENTITY_SELECTOR = sel.EntitySelector(
    sel.EntitySelectorConfig(filter=LOCKS_FILTER_CONFIG, multiple=True)
)
SLOTS_YAML_SELECTOR = sel.ObjectSelector(sel.ObjectSelectorConfig())


POSITIVE_INT = vol.All(vol.Coerce(int), vol.Range(min=1))


class _SiblingAnswer(NamedTuple):
    """What another entry that manages a member already says about it."""

    codeless: bool
    entry_title: str


def _sibling_declarations(
    hass: HomeAssistant, config_entry: ConfigEntry | None
) -> dict[str, _SiblingAnswer]:
    """
    Return what every other entry says about each member it holds.

    Whether a lock has credential storage is a fact about the DEVICE, not
    about a configuration: an ESPHome lock with no keypad has none no matter
    how many entries manage it. Two entries answering differently is
    therefore not a configuration but a contradiction, and one with teeth --
    the two answers resolve to two providers over one entity, so a Personal
    Identification Number lands in whichever store the caller happened to
    reach. Every surface that saves a lock selection consults this.

    Keyed by ``er.RegistryEntry.id``, the same handle the declarations
    themselves are keyed by. The value carries the entry's title alongside
    its answer, because a refusal that does not name the other configuration
    leaves the user with nowhere to go.

    Only entries that HOLD the member answer for it. A declaration left
    behind by an entry that no longer lists the lock is not an opinion about
    anything -- nothing resolves it -- and reading one would refuse a
    selection no live provider contradicts.

    ``config_entry`` is the entry being edited, left out because its own
    stored answer is the one being replaced. A flow that is still creating
    its entry passes ``None`` and excludes nothing.

    Every entry is consulted, disabled ones included. A disabled entry holds
    its configuration and resolves it again the moment it is re-enabled, so
    an answer that contradicts it is a contradiction deferred rather than
    avoided.

    The first holder found answers for a member. Siblings cannot disagree
    with each other -- this is what stops them -- so which one is arbitrary
    only in the sense that they all say the same thing.
    """
    ent_reg = er.async_get(hass)
    declarations: dict[str, _SiblingAnswer] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if config_entry is not None and entry.entry_id == config_entry.entry_id:
            continue
        sibling = get_entry_config(entry)
        for entity_id in sibling.locks:
            if (lock_entry := ent_reg.async_get(entity_id)) is not None:
                declarations.setdefault(
                    lock_entry.id,
                    _SiblingAnswer(sibling.is_codeless(lock_entry), entry.title),
                )
    return declarations


def _declared_codeless(
    hass: HomeAssistant, config: EntryConfig, config_entry: ConfigEntry | None
) -> set[str]:
    """
    Return the members some configuration already declares codeless.

    What both the codeless picker and its refusal treat as settled. A
    declaration outranks platform dispatch permanently
    (``domain.locks.resolve_member_provider_class``), so a member declared
    before Lock Code Manager learned to claim its lock keeps resolving to
    the Lock Code Manager store -- and hiding it from the picker, or
    refusing it there, would leave that entry unable to save any edit at all
    while its way out is to move the lock to the other field.

    A sibling's declarations count for the same reason they gate the
    conflict refusal: an entry adding a lock another entry already declares
    has to be able to agree with it, and a picker that excluded the lock
    would make agreeing the one answer it could not give.
    """
    ent_reg = er.async_get(hass)
    declared = {
        lock_entry.id
        for entity_id in config.locks
        if (lock_entry := ent_reg.async_get(entity_id)) is not None
        and config.is_codeless(lock_entry)
    }
    declared.update(
        registry_id
        for registry_id, answer in _sibling_declarations(hass, config_entry).items()
        if answer.codeless
    )
    return declared


def _codeless_lock_selector(
    hass: HomeAssistant, declared: Container[str]
) -> sel.EntitySelector:
    """
    Return the picker for locks that keep no codes of their own.

    Every ``lock`` entity is on offer except the ones a provider claims, so
    the two pickers are disjoint by construction and each offers only what
    belongs in it. The exclusions are computed from the entity registry each
    time the form is built, which is what keeps them honest -- a lock whose
    integration Lock Code Manager learns to claim in a later version moves
    fields the next time somebody opens the dialog.

    Home Assistant enforces this on submit as well as on render: an
    ``EntitySelector`` validates its own ``exclude_entities``. So what
    reaches ``_check_lock_selection`` from the UI is a selection the
    exclusions of the LAST RENDERED form allowed, which is why that refusal
    exists too -- dispatch can start claiming a lock between the render and
    the submit.

    Dispatch is what decides, so an mqtt lock from a bridge Lock Code
    Manager does not recognize is offered here -- and, because the other
    picker allowlists the mqtt PLATFORM, appears in both. That is not a
    contradiction to resolve: the platform is supported and this particular
    bridge is not, and the user is the one who knows whether the lock has a
    keypad.

    ``declared`` is what some configuration already declares codeless, and
    is offered whatever dispatch now makes of it -- see
    :func:`_declared_codeless`.
    """
    dev_reg = dr.async_get(hass)
    return sel.EntitySelector(
        sel.EntitySelectorConfig(
            domain=LOCK_DOMAIN,
            multiple=True,
            exclude_entities=sorted(
                lock_entry.entity_id
                for lock_entry in er.async_get(hass).entities.values()
                if lock_entry.domain == LOCK_DOMAIN
                and lock_entry.id not in declared
                and resolve_provider_class_for_entity(dev_reg, lock_entry) is not None
            ),
        )
    )


def _selected_locks(user_input: Mapping[str, Any]) -> list[str]:
    """Return the roster the two pickers add up to, in the order they were shown."""
    return [*user_input[CONF_LOCKS], *user_input[CONF_CODELESS_LOCKS]]


def _registry_ids(hass: HomeAssistant, entity_ids: Iterable[str]) -> set[str]:
    """Return the registry ids of the entities the registry knows."""
    ent_reg = er.async_get(hass)
    return {
        lock_entry.id
        for entity_id in entity_ids
        if (lock_entry := ent_reg.async_get(entity_id)) is not None
    }


def _stored_selection(hass: HomeAssistant, config: EntryConfig) -> dict[str, list[str]]:
    """
    Return an entry's roster split back into the two fields that express it.

    What every surface seeds its form from, so the picker a lock appears in
    is the declaration the entry holds about it. A member the registry no
    longer knows lands in the ordinary field, where ``lock_not_registered``
    is waiting for it -- there is no id to read a declaration by, so there
    is nothing to put it in the other one on.
    """
    ent_reg = er.async_get(hass)
    selection: dict[str, list[str]] = {CONF_LOCKS: [], CONF_CODELESS_LOCKS: []}
    for entity_id in config.locks:
        lock_entry = ent_reg.async_get(entity_id)
        field = (
            CONF_CODELESS_LOCKS
            if lock_entry is not None and config.is_codeless(lock_entry)
            else CONF_LOCKS
        )
        selection[field].append(entity_id)
    return selection


def _check_lock_selection(
    hass: HomeAssistant,
    user_input: Mapping[str, Any],
    config: EntryConfig,
    config_entry: ConfigEntry | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """
    Turn a lock selection that cannot be set up into the errors that name it.

    A backstop rather than the way the rule is taught. The two pickers are
    disjoint by construction -- one offers the locks a provider claims, the
    other exactly the locks nothing claims -- so a user is shown where each
    lock belongs instead of being told afterwards. Selector filters are
    UI-only though: a YAML import or a direct submission to the flow can
    send anything, and every refusal below is what stands between that and
    an entry that cannot load.

    Refusals that CAN render together are accumulated, and ones that cannot
    are guarded. A dict holds one message per key, so two refusals on one
    key means the later silently replaces the earlier and the user is
    refused twice for a submission that was wrong in two ways from the
    start. The three keys here -- ``base``, and one per field -- are
    therefore filled at most once each, and their placeholders are named
    apart so one flat mapping renders all three.

    An entity with no entity registry row is refused outright, in either
    field. It is the same predicate ``async_setup_entry`` refuses on, moved
    to where the lock is chosen: there is no id to key a declaration or a
    device to, so the entry cannot load with one in its roster. It is NOT
    grandfathered the way the two unclaimed refusals are -- an entry holding
    one is not running at all, so there is no working configuration to lock
    somebody out of, and the reauth that setup starts is the flow that has
    to refuse the lock for the loop to ever end.

    What the entry already holds in the ordinary field IS grandfathered. An
    entry configured before these checks existed can be carrying an
    unclaimed lock, and every options and reauth submission re-renders that
    entry's whole roster -- so validating all of it made one such lock
    refuse every subsequent edit: no PIN could be changed and no reauth
    could complete, for a lock the form was not being asked to add. That
    lock is not silently accepted either; it is dropped at setup and says so
    in its own repair. A member the entry declares CODELESS is deliberately
    not grandfathered here: moving one into the ordinary field is how a
    declaration is taken back, and taking it back for a lock nothing claims
    leaves an entry with a member nothing can manage.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    errors: dict[str, str] = {}
    placeholders: dict[str, Any] = {}

    picked = list(user_input[CONF_LOCKS])
    declared = list(user_input[CONF_CODELESS_LOCKS])

    if unregistered := [
        entity_id
        for entity_id in (*picked, *declared)
        if ent_reg.async_get(entity_id) is None
    ]:
        errors["base"] = "lock_not_registered"
        placeholders["unregistered_locks"] = ", ".join(unregistered)

    grandfathered = _stored_selection(hass, config)[CONF_LOCKS]
    unclaimed = [
        lock_entry
        for entity_id in picked
        if entity_id not in grandfathered
        and (lock_entry := ent_reg.async_get(entity_id)) is not None
        and resolve_provider_class_for_entity(dev_reg, lock_entry) is None
    ]
    # mqtt keeps its own wording: its platform IS supported and this bridge
    # is not, which is a different thing to know than "nothing claims this".
    # Guarded rather than accumulated because both name the same field; mqtt
    # goes first because it is the only one of the two the picker can put
    # there, so a user reaching this reads the message about their own lock.
    if mqtt_unclaimed := [
        lock_entry for lock_entry in unclaimed if lock_entry.platform == MQTT_DOMAIN
    ]:
        errors[CONF_LOCKS] = "unsupported_mqtt_lock"
        placeholders["locks"] = ", ".join(
            lock_entry.entity_id for lock_entry in mqtt_unclaimed
        )
    elif unclaimed:
        errors[CONF_LOCKS] = "unclaimed_lock"
        placeholders["locks"] = ", ".join(
            lock_entry.entity_id for lock_entry in unclaimed
        )

    siblings = _sibling_declarations(hass, config_entry)
    settled = _declared_codeless(hass, config, config_entry)
    codeless_ids = _registry_ids(hass, declared)
    # Guarded for the same reason, and ordered by how readily each is
    # reached: a lock offered in both pickers can be selected in both, a
    # sibling can be contradicted straight from the form, and a claimed lock
    # gets into the codeless field only if dispatch started claiming it
    # between the render and the submit.
    picked_ids = set(picked)
    if both := [entity_id for entity_id in declared if entity_id in picked_ids]:
        errors[CONF_CODELESS_LOCKS] = "lock_in_both_fields"
        placeholders["codeless_locks"] = ", ".join(both)
    elif contradicted := [
        f"{lock_entry.entity_id} ({sibling.entry_title})"
        for entity_id in (*picked, *declared)
        if (lock_entry := ent_reg.async_get(entity_id)) is not None
        and (sibling := siblings.get(lock_entry.id)) is not None
        and sibling.codeless != (lock_entry.id in codeless_ids)
    ]:
        errors[CONF_CODELESS_LOCKS] = "codeless_conflict"
        placeholders["codeless_locks"] = ", ".join(contradicted)
    elif claimed := [
        lock_entry.entity_id
        for entity_id in declared
        if (lock_entry := ent_reg.async_get(entity_id)) is not None
        and lock_entry.id not in settled
        and resolve_provider_class_for_entity(dev_reg, lock_entry) is not None
    ]:
        errors[CONF_CODELESS_LOCKS] = "codeless_lock_claimed"
        placeholders["codeless_locks"] = ", ".join(claimed)

    return errors, placeholders


def _declared_members(
    hass: HomeAssistant, config: EntryConfig, user_input: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """
    Return what to store about this entry's members, as the two fields say it.

    The roster the entry is about to have is resolved here and passed along,
    so a declaration about a member this submission drops goes with it.
    Every entity resolves, because :func:`_check_lock_selection` refuses the
    whole submission over one that does not and runs first at every call
    site, and is asserted rather than skipped: one quietly missing from the
    roster would take its declaration with it while its entity id stayed in
    ``locks``, leaving an entry that cannot load and no longer remembers
    what it was told.
    """
    ent_reg = er.async_get(hass)
    roster = set()
    for entity_id in _selected_locks(user_input):
        lock_entry = ent_reg.async_get(entity_id)
        assert lock_entry
        roster.add(lock_entry.id)
    return declare_codeless(
        config.members, _registry_ids(hass, user_input[CONF_CODELESS_LOCKS]), roster
    )


async def _async_discard_reclaimed_credentials(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    config: EntryConfig,
    user_input: Mapping[str, Any],
) -> None:
    """
    Retire the store of every member this submission stops declaring codeless.

    A member leaves the codeless field two ways -- moved to the other
    picker, or dropped from the entry entirely -- and both mean the same
    thing: Lock Code Manager is no longer the place that lock's credentials
    live. Leaving the store behind leaves a file of cleartext Personal
    Identification Numbers on disk that nothing will ever read again, and
    nothing will ever collect either, because the entry no longer declares
    the member it would have been collected under.

    Done here rather than left to the redeclaration teardown, which runs
    from the update listener. That listener is registered during setup and
    released on unload, so it is absent for exactly the entries this gets
    edited on: a failed one being repaired through reauth, an unloaded or
    disabled one being edited through options. Asked unconditionally rather
    than only when the listener is known to be missing -- naming a store and
    removing it is idempotent, and this is awaited before the save that
    would trigger the listener, so when the teardown does also run it finds
    the file already gone.

    A store another configuration still reads is kept, which is the same
    gate entry deletion puts on its own sweep. The conflict refusal already
    stops a lock this entry KEEPS from disagreeing with a sibling, but a
    lock this entry DROPS is outside what any refusal looks at, and
    discarding one a sibling still holds would delete that entry's codes.

    Instances are built fresh and only to name the store: nothing here
    reaches the lock.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    keeping = _registry_ids(hass, user_input[CONF_CODELESS_LOCKS])
    siblings = _sibling_declarations(hass, config_entry)
    for entity_id in config.locks:
        lock_entry = ent_reg.async_get(entity_id)
        if lock_entry is None or not config.is_codeless(lock_entry):
            continue
        if lock_entry.id in keeping:
            continue
        if (sibling := siblings.get(lock_entry.id)) is not None and sibling.codeless:
            continue
        lock = CodelessLock(
            hass,
            dev_reg,
            ent_reg,
            hass.config_entries.async_get_entry(lock_entry.config_entry_id),
            lock_entry,
        )
        await lock.async_remove_stored_credentials()


def _pending_config(
    hass: HomeAssistant, config: EntryConfig, user_input: Mapping[str, Any]
) -> EntryConfig:
    """
    Return the entry's configuration as this submission would leave it.

    What the locks get read through. Reading the STORED declaration instead
    made a pending change invisible to the very validation the change
    triggers: moving a member out of the codeless field checked the slot
    numbers against the Lock Code Manager store -- no capacity, and empty --
    rather than against the provider the lock is about to become, so a
    configuration far too large for the real lock saved without a word.

    An entity the registry does not know is skipped rather than asserted,
    unlike :func:`_declared_members`. This runs beside the lock-selection
    check rather than after it, so it can see a selection that is about to
    be refused; the write path runs only once that refusal has not happened,
    and there an entity missing here would silently drop a declaration.
    """
    return config.with_members(
        declare_codeless(
            config.members,
            _registry_ids(hass, user_input[CONF_CODELESS_LOCKS]),
            _registry_ids(hass, _selected_locks(user_input)),
        )
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
    config: EntryConfig | None = None,
) -> tuple[dict | None, dict, dict]:
    """
    Validate a users submission for the editor and the yaml setup path.

    Slot numbers are not part of this shape at all: the editor names users,
    and the numbers are allocated afterwards from whatever the locks leave
    free. What can still fail is a name that is empty or that means the same
    person as another, and a count of users no lock could hold.

    ``config_entry`` is the entry the submission is for, or ``None`` from
    the flow that is still creating one; it is what the lock reads this
    performs are made on behalf of. ``config`` is what that entry would
    declare about its members once this submission is saved, so a
    declaration taken back in this same flow is read through the provider
    the lock is about to become rather than the one it is leaving.

    Returns the parsed users -- or ``None`` when validation failed -- with the
    accumulated errors and description placeholders.

    Every refusal is keyed to the users field rather than to ``base``, so it
    renders beside the block it is about and, more to the point, alongside
    whatever the lock list was refused for. A dict holds one message per key:
    sharing ``base`` with ``_check_lock_selection`` meant the later of the two
    silently replaced the earlier, and the user was refused twice for a
    submission that was wrong in two ways from the start.
    """
    # A block still keyed by slot number coerces cleanly into users named
    # "1", "2", which is a silently wrong reading of what was pasted.
    if raw_users and all(isinstance(key, int) for key in raw_users):
        return None, {CONF_USERS: "users_keyed_by_slot"}, {}

    try:
        parsed_users = USERS_SCHEMA(raw_users)
    except vol.Invalid as err:
        _LOGGER.error("Invalid users: %s", err)
        return None, {CONF_USERS: "invalid_config"}, {}

    if problem := validate_user_names(parsed_users):
        name, error = problem
        return None, {CONF_USERS: error}, {"name": name}

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
                {CONF_USERS: "excluded_platform"},
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
        await async_check_slot_capacity(
            hass, config_entry, locks, [len(parsed_users)], config
        )
    except SlotAllocationError as err:
        return (
            None,
            {CONF_USERS: "too_many_users"},
            {**err.placeholders, "num_users": str(len(parsed_users))},
        )
    return parsed_users, {}, {}


async def _allocate_for(
    hass: HomeAssistant,
    config_entry: ConfigEntry | None,
    locks: Sequence[str],
    num_users: int,
    config: EntryConfig | None = None,
) -> tuple[frozenset[int] | None, dict[str, str], dict[str, Any]]:
    """
    Find numbers for ``num_users``, or say why it could not.

    Allocation itself lives in ``domain.allocation``, which the services call
    too; this only turns its refusals into the form errors a flow renders.

    ``config_entry`` is the entry being allocated for, whose own numbers do
    not constrain it: kept ones are held by tenure, released ones are free
    for whoever comes next. A flow that is still creating its entry passes
    ``None``, which by the same rule holds nothing. ``config`` is what that
    entry would declare once the submission is saved, so the locks are read
    as the answers in hand leave them.
    """
    try:
        unavailable = await async_allocate_for(
            hass, config_entry, locks, num_users, config
        )
    except SlotAllocationError as err:
        return None, {"base": err.translation_key}, err.placeholders
    return unavailable, {}, {}


class LockCodeManagerFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
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
        # The two pickers as they were submitted, kept because the steps
        # after this one still have to know which field each lock came from:
        # the entry stores one merged roster, and which picker a lock was in
        # is the whole of what it declares about that lock.
        self._selection: dict[str, list[str]] = {
            CONF_LOCKS: [],
            CONF_CODELESS_LOCKS: [],
        }

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
            errors, description_placeholders = _check_lock_selection(
                self.hass, user_input, EntryConfig.empty()
            )
            if not errors:
                self.title = user_input.pop(CONF_NAME)
                await self.async_set_unique_id(slugify(self.title))
                self._abort_if_unique_id_configured()
                self._selection = {
                    CONF_LOCKS: list(user_input[CONF_LOCKS]),
                    CONF_CODELESS_LOCKS: list(user_input[CONF_CODELESS_LOCKS]),
                }
                self.data = {CONF_LOCKS: _selected_locks(user_input)}
                return await self.async_step_choose_path()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_LOCKS, default=list): LOCK_ENTITY_SELECTOR,
                        vol.Required(
                            CONF_CODELESS_LOCKS, default=list
                        ): _codeless_lock_selector(
                            self.hass,
                            _declared_codeless(self.hass, EntryConfig.empty(), None),
                        ),
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
            ) = await _allocate_for(
                self.hass,
                None,
                self.data[CONF_LOCKS],
                num_users,
                _pending_config(self.hass, EntryConfig.empty(), self._selection),
            )
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
        if members := _declared_members(
            self.hass, EntryConfig.empty(), self._selection
        ):
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
                self.hass,
                None,
                user_input[CONF_USERS],
                self.data[CONF_LOCKS],
                _pending_config(self.hass, EntryConfig.empty(), self._selection),
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
                    self.hass,
                    None,
                    self.data[CONF_LOCKS],
                    len(users),
                    _pending_config(self.hass, EntryConfig.empty(), self._selection),
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

        entry_config = get_entry_config(config_entry)
        if user_input is None:
            # Either the frontend re-invoking the step to render the form, or
            # the initial call carrying the entry's data. Seed both pickers
            # from the entry's current config either way, so each lock comes
            # back in the field that expresses what the entry declares about
            # it.
            user_input = _stored_selection(self.hass, entry_config)
        else:
            existing_slots = entry_config.slot_numbers
            selected = _selected_locks(user_input)
            additional_errors, additional_placeholders = _check_lock_selection(
                self.hass, user_input, entry_config, config_entry
            )
            if not additional_errors:
                additional_errors, additional_placeholders = _check_common_slots(
                    self.hass,
                    selected,
                    existing_slots,
                    config_entry,
                )
            if not additional_errors:
                # Reauth is where a lock gets swapped, so it is also where an
                # already-valid slot set can become too large for the new
                # lock -- including a member moved out of the codeless field,
                # which is sized against the provider it is about to become
                # rather than the empty Lock Code Manager store it is
                # leaving.
                try:
                    await async_check_slot_capacity(
                        self.hass,
                        config_entry,
                        selected,
                        existing_slots,
                        _pending_config(self.hass, entry_config, user_input),
                    )
                except SlotAllocationError as err:
                    additional_errors = {"base": err.translation_key}
                    additional_placeholders = err.placeholders
            errors.update(additional_errors)
            description_placeholders.update(additional_placeholders)
            if not errors:
                await _async_discard_reclaimed_credentials(
                    self.hass, config_entry, entry_config, user_input
                )
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
                        CONF_LOCKS: selected,
                        CONF_MEMBERS: _declared_members(
                            self.hass, entry_config, user_input
                        ),
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
                    ): LOCK_ENTITY_SELECTOR,
                    vol.Required(
                        CONF_CODELESS_LOCKS,
                        default=user_input[CONF_CODELESS_LOCKS],
                    ): _codeless_lock_selector(
                        self.hass,
                        _declared_codeless(self.hass, entry_config, config_entry),
                    ),
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

        stored = get_entry_config(self.config_entry)
        if user_input:
            selected = _selected_locks(user_input)
            # Accumulated alongside the users validation rather than
            # short-circuiting it. The rule is the same everywhere here:
            # accumulate what can render together, guard what cannot. These
            # two can, because they are keyed to different fields; the
            # allocation refusals below share ``base`` with the registry
            # refusal, so they are guarded instead.
            lock_errors, lock_placeholders = _check_lock_selection(
                self.hass, user_input, stored, self.config_entry
            )
            errors.update(lock_errors)
            description_placeholders.update(lock_placeholders)

            # Everything that reads a lock reads it as this submission would
            # leave it, declarations included -- so a member moved out of the
            # codeless field is validated against the provider it becomes.
            pending = _pending_config(self.hass, stored, user_input)
            (
                users,
                validation_errors,
                validation_placeholders,
            ) = await _async_validate_users_yaml(
                self.hass,
                self.config_entry,
                user_input[CONF_USERS],
                selected,
                pending,
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
                    selected,
                    len(users),
                    pending,
                )
                if unavailable is None:
                    errors.update(allocation_errors)
                    description_placeholders.update(allocation_placeholders)
                else:
                    # Reconciled against what the entry already holds, so a
                    # user who was here before keeps their number and only
                    # newcomers are issued one. Moving somebody would rewrite
                    # their credential on every lock.
                    assignment = stored.assignment.reconcile(
                        users, start=1, unavailable=unavailable
                    )
                    await _async_discard_reclaimed_credentials(
                        self.hass, self.config_entry, stored, user_input
                    )
                    # Written through EntryConfig so whatever else the entry
                    # carries survives the edit. Building the dict by hand
                    # drops every key this form does not ask about.
                    return self.async_create_entry(
                        title="",
                        data=EntryConfig(
                            locks=tuple(selected),
                            members=_declared_members(self.hass, stored, user_input),
                            users=users,
                            assignment=assignment,
                            extra=stored.extra,
                        ).to_dict(),
                    )

        # Plain dict/list, because the form selectors cannot serialize the
        # deeply read-only mappings EntryConfig uses internally.
        defaults = {
            **_stored_selection(self.hass, stored),
            CONF_USERS: {name: dict(user) for name, user in stored.users.items()},
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
                        CONF_CODELESS_LOCKS,
                        default=user_input.get(
                            CONF_CODELESS_LOCKS, defaults[CONF_CODELESS_LOCKS]
                        ),
                    ): _codeless_lock_selector(
                        self.hass,
                        _declared_codeless(self.hass, stored, self.config_entry),
                    ),
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
