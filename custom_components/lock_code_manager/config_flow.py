"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
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
    MAX_SEARCHED_SLOT,
)
from .domain.config import EntryConfig
from .domain.credentials import CredentialType
from .domain.exceptions import LockCodeManagerError
from .domain.names import name_error, normalize_name, validate_user_names
from .domain.occupancy import LockOccupancy, Occupancy
from .domain.queries import get_entry_config, get_managed_slots
from .domain.slot_assignment import CONF_SLOT_ASSIGNMENT, SlotAssignment
from .providers import INTEGRATIONS_CLASS_MAP

_LOGGER = logging.getLogger(__name__)

CODE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_PIN): cv.string,
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
        vol.Optional(CONF_PIN): cv.string,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_CONDITION): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=CONDITION_ENTITY_DOMAINS)
        ),
    }
)

USERS_SCHEMA = vol.All(vol.Schema({cv.string: USER_SCHEMA}), enabled_requires_pin)

LOCKS_FILTER_CONFIG = [
    sel.EntityFilterSelectorConfig(integration=platform, domain=LOCK_DOMAIN)
    for platform in INTEGRATIONS_CLASS_MAP
]
LOCK_ENTITY_SELECTOR = sel.EntitySelector(
    sel.EntitySelectorConfig(filter=LOCKS_FILTER_CONFIG, multiple=True)
)
SLOTS_YAML_SELECTOR = sel.ObjectSelector(sel.ObjectSelectorConfig())


POSITIVE_INT = vol.All(vol.Coerce(int), vol.Range(min=1))


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


async def _async_check_slot_capacity(
    hass: HomeAssistant,
    locks: Iterable[str],
    slots_list: Iterable[int | str],
) -> tuple[dict, dict]:
    """
    Reject slot numbers no lock in the set could ever hold.

    Only locks whose credential index IS the slot number are checked
    (``BaseLock.credential_index_follows_slot``) -- Matter allocates its own
    index, so a slot number above its credential count is legal there.
    Catching it here spares the user a write that fails forever on the device
    with nothing but a connectivity warning to show for it.

    A lock that cannot be queried is skipped rather than blocking the flow:
    capabilities need the lock awake, and a sleeping battery lock must not
    make the config flow unusable. The same check runs at write time, where
    it can suspend the affected slot precisely.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    slots = sorted({int(slot) for slot in slots_list})
    for lock_entity_id in locks:
        try:
            lock_instance = _async_build_lock_instance(
                hass, dev_reg, ent_reg, lock_entity_id
            )
            if not lock_instance.credential_index_follows_slot:
                continue
            capabilities = await lock_instance.async_get_capabilities()
        except _LockQuerySkipped:
            continue
        except LockCodeManagerError as err:
            _LOGGER.debug(
                "Skipping slot capacity check for %s: %s", lock_entity_id, err
            )
            continue
        except Exception:
            _LOGGER.warning(
                "Skipping slot capacity check for %s; its slot count could not "
                "be determined",
                lock_entity_id,
                exc_info=True,
            )
            continue

        num_slots = capabilities.bounded_slot_count(CredentialType.PIN)
        if num_slots is None:
            continue
        if out_of_range := [slot for slot in slots if not 1 <= slot <= num_slots]:
            return {"base": "slot_out_of_range"}, {
                "lock": lock_entity_id,
                "num_slots": str(num_slots),
                "out_of_range_slots": ", ".join(str(slot) for slot in out_of_range),
            }
    return {}, {}


async def _async_validate_users_yaml(
    hass: HomeAssistant,
    raw_users: dict[Any, Any],
    locks: Iterable[str],
) -> tuple[dict | None, dict, dict]:
    """
    Validate a users submission for the editor and the yaml setup path.

    Slot numbers are not part of this shape at all: the editor names users,
    and the numbers are allocated afterwards from whatever the locks leave
    free. What can still fail is a name that is empty or that means the same
    person as another, and a count of users no lock could hold.

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

    # The count is what a lock has to hold, now that nobody picks a number.
    errors, placeholders = await _async_check_slot_capacity(
        hass, locks, [len(parsed_users)]
    )
    if errors:
        return (
            None,
            {"base": "too_many_users"},
            {**placeholders, "num_users": str(len(parsed_users))},
        )
    return parsed_users, {}, {}


class _LockQuerySkipped(LockCodeManagerError):
    """
    Raised when no provider could be built for a lock.

    ``managed`` says whether Lock Code Manager will write credentials to the
    lock anyway. Only an unsupported platform means it will not. A lock
    missing from the entity registry, or whose integration's entry has gone,
    is one this entry still owns and merely could not reach -- reporting it
    as unmanaged would drop it from the occupancy check entirely and let
    allocation issue numbers against a lock it never read.
    """

    def __init__(self, lock_entity_id: str, *, managed: bool) -> None:
        """Record whether the lock is one credentials still get written to."""
        super().__init__(lock_entity_id)
        self.managed = managed


def _async_build_lock_instance(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    lock_entity_id: str,
) -> Any:
    """
    Build a temporary lock provider instance for ``lock_entity_id``.

    Performs setup-time checks (entity in registry, supported platform,
    parent config entry exists) and instantiates the provider class.
    Raises ``_LockQuerySkipped`` if any setup-time check fails.
    """
    lock_entry = ent_reg.async_get(lock_entity_id)
    if not lock_entry:
        _LOGGER.warning(
            "Entity %s not found in registry; skipping usercode check",
            lock_entity_id,
        )
        raise _LockQuerySkipped(lock_entity_id, managed=True)
    if lock_entry.platform not in INTEGRATIONS_CLASS_MAP:
        _LOGGER.debug(
            "Lock %s uses unsupported platform %s; skipping usercode check",
            lock_entity_id,
            lock_entry.platform,
        )
        raise _LockQuerySkipped(lock_entity_id, managed=False)
    lock_config_entry = hass.config_entries.async_get_entry(lock_entry.config_entry_id)
    if lock_config_entry is None:
        _LOGGER.warning(
            "Config entry for lock %s not found; skipping usercode check",
            lock_entity_id,
        )
        raise _LockQuerySkipped(lock_entity_id, managed=True)

    return INTEGRATIONS_CLASS_MAP[lock_entry.platform](
        hass, dev_reg, ent_reg, lock_config_entry, lock_entry
    )


class _AllocatesSlotsMixin:
    """
    Finding numbers for users, shared by the setup and editing flows.

    Neither flow asks anybody for a slot number: both name users and then
    allocate around whatever the locks already hold. Keeping that in one
    place is what stops the two from disagreeing about which numbers are
    free -- and only one of them reading the locks would be the more
    dangerous disagreement.

    ``_allocation_locks`` is set by the flow before allocating, because
    setup has the locks in its collected data and editing has them in the
    submission being validated.
    """

    # Supplied by whichever flow mixes this in; both have one.
    hass: HomeAssistant

    _allocation_locks: Sequence[str] = ()

    # The entry this flow is editing, if any. Setup is not editing one.
    _entry_being_edited: ConfigEntry | None = None

    async def _async_read_occupancy(self, indices: Collection[int]) -> Occupancy:
        """
        Ask every configured lock which of ``indices`` it holds.

        A lock that cannot answer reports ``None``, never an empty set: that
        difference is what makes allocation refuse instead of issuing a
        number over a credential it never checked for. Every way of failing
        to answer has to arrive that way, including the ones no provider
        promised.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        locks: list[LockOccupancy] = []
        for lock_entity_id in self._allocation_locks:
            try:
                lock_instance = _async_build_lock_instance(
                    self.hass, dev_reg, ent_reg, lock_entity_id
                )
            except _LockQuerySkipped as skipped:
                locks.append(
                    LockOccupancy(
                        lock_entity_id=lock_entity_id,
                        # With no provider there is no way to ask whether this
                        # lock addresses credentials by slot number, so assume
                        # it does -- the assumption that refuses rather than
                        # guesses. An unmanaged lock constrains nothing either
                        # way.
                        credential_index_follows_slot=skipped.managed,
                        managed=skipped.managed,
                        occupied=None,
                    )
                )
                continue
            except Exception:
                _LOGGER.warning(
                    "Could not build a provider for %s; its contents are unknown",
                    lock_entity_id,
                    exc_info=True,
                )
                locks.append(
                    LockOccupancy(
                        lock_entity_id=lock_entity_id,
                        credential_index_follows_slot=True,
                        managed=True,
                        occupied=None,
                    )
                )
                continue

            follows_slot = lock_instance.credential_index_follows_slot
            occupied: frozenset[int] | None = None
            if follows_slot:
                # A lock that allocates its own credential index cannot
                # constrain the numbering, so asking it spends a round trip
                # -- one per index on some providers -- on an answer that
                # nothing reads.
                try:
                    occupied = await lock_instance.async_internal_get_occupied_indices(
                        indices
                    )
                except Exception:
                    _LOGGER.warning(
                        "Failed to read the contents of %s; treating them as unknown",
                        lock_entity_id,
                        exc_info=True,
                    )
            locks.append(
                LockOccupancy(
                    lock_entity_id=lock_entity_id,
                    credential_index_follows_slot=follows_slot,
                    managed=True,
                    occupied=occupied,
                )
            )
        return Occupancy(
            locks=tuple(locks),
            claimed_by_other_entries=frozenset(
                slot
                for lock_entity_id in self._allocation_locks
                for slot in get_managed_slots(
                    self.hass, lock_entity_id, excluding=self._entry_being_edited
                )
            ),
        )

    def _too_far(
        self,
        num_users: int,
        max_slot: int,
        limiting_lock: str | None,
        needed: int | None = None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """
        Explain that the numbers needed run past where the search may go.

        Two things decide the wording. Whose limit it is: a lock that
        reported its own range can be named and described as the lock's
        capacity, while a limit nothing reported must not be, or the user is
        sent to re-interview a lock over a number it never gave.

        And whether the count itself is too large, or only the numbers it
        would have to reach. ``needed`` names the number the last user would
        land on when existing codes have pushed them past the range -- a
        count that fits the lock told "N users will not fit" reads as a bug.
        """
        if limiting_lock is None:
            return {"base": "search_limit_reached"}, {
                "num_users": str(num_users),
                "max_slot": str(max_slot),
            }
        if needed is not None:
            return {"base": "numbers_needed_exceed_capacity"}, {
                "num_users": str(num_users),
                "num_slots": str(max_slot),
                "needed": str(needed),
                "lock": limiting_lock,
            }
        return {"base": "too_many_users"}, {
            "num_users": str(num_users),
            "num_slots": str(max_slot),
            "lock": limiting_lock,
        }

    async def _async_max_slot(self) -> tuple[int, str | None]:
        """
        Return how far a search for free numbers may go across these locks.

        The smallest answer wins, and comes back with the lock that gave it
        so a refusal can name the right one: a number past any single lock's
        range is a number that lock cannot hold, and every lock in an entry
        gets the same numbers. Locks that allocate their own credential index
        are not asked, because their contents never constrain the numbering.

        A lock of ``None`` means nothing here could say and the limit is this
        integration's own -- which a message must not describe as a capacity
        some lock reported.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        limits: dict[str, int] = {}
        for lock_entity_id in self._allocation_locks:
            try:
                lock_instance = _async_build_lock_instance(
                    self.hass, dev_reg, ent_reg, lock_entity_id
                )
                if not lock_instance.credential_index_follows_slot:
                    continue
                # None is a lock with no opinion, not a lock with no slots.
                # Only a real answer earns a name, because the name is what
                # the refusal blames.
                if (bound := await lock_instance.async_get_max_slot()) is not None:
                    limits[lock_entity_id] = bound
            except Exception:
                _LOGGER.warning(
                    "Could not ask %s how far its slot numbers go; "
                    "searching only as far as this integration does",
                    lock_entity_id,
                    exc_info=True,
                )
        if not limits:
            return MAX_SEARCHED_SLOT, None
        # Ties are ordinary -- two locks of a kind answer alike -- so the
        # entity id breaks them, and the same lock is named every time.
        limiting = min(limits, key=lambda lock: (limits[lock], lock))
        return limits[limiting], limiting

    async def _async_allocate_for(
        self, num_users: int
    ) -> tuple[frozenset[int] | None, dict[str, str], dict[str, Any]]:
        """
        Find numbers for ``num_users``, reading only as far as it has to.

        Locks that answer one index per round trip make the width of this
        read its cost, so the window starts at the number of users and widens
        only by what turned out to be in the way, each pass asking only about
        numbers no earlier pass covered. Every index is read at most once.

        Terminating is not an accident: each pass either finds enough free
        numbers or discovers strictly MORE occupied ones than the pass
        before -- a pass discovering no more would have found the window big
        enough, the window being exactly the count plus what was in the way --
        and the locks hold finitely many credentials.

        Returns the numbers allocation must avoid, verified across a window
        wide enough to hold everyone. The users are numbered from it later,
        once they have names.
        """
        errors, placeholders = await _async_check_slot_capacity(
            self.hass, self._allocation_locks, [num_users]
        )
        if errors:
            return (
                None,
                {"base": "too_many_users"},
                {**placeholders, "num_users": str(num_users)},
            )

        max_slot, limiting_lock = await self._async_max_slot()
        if num_users > max_slot:
            # Before the first read, not just before each widening: a count
            # past the range walks off the end of the lock on the way in, and
            # a lock reading past-end as free would hand back all of it.
            return None, *self._too_far(num_users, max_slot, limiting_lock)

        unavailable: set[int] = set()
        read_up_to = 0
        window = num_users
        while True:
            # Only the part nobody has asked about yet; re-reading from one
            # each pass would cost a nearly-full lock several times its own
            # capacity to place a couple of users.
            occupancy = await self._async_read_occupancy(
                range(read_up_to + 1, window + 1)
            )
            if not occupancy.is_known:
                # Unreadable is not free: issuing a number could overwrite a
                # credential programmed by hand on a lock that did not answer.
                return (
                    None,
                    {"base": "occupancy_unknown"},
                    {"locks": ", ".join(occupancy.unreadable)},
                )
            unavailable |= occupancy.unavailable
            read_up_to = window

            taken_in_window = sum(1 for slot in unavailable if slot <= window)
            if window - taken_in_window >= num_users:
                break

            # Every number in the way pushes the last user one further out.
            wider = num_users + taken_in_window
            errors, placeholders = await _async_check_slot_capacity(
                self.hass, self._allocation_locks, [wider]
            )
            if errors:
                # Distinct from the count being too large: the count fits,
                # and the numbers needed to reach around what is already
                # there do not.
                return (
                    None,
                    {"base": "numbers_needed_exceed_capacity"},
                    {
                        **placeholders,
                        "num_users": str(num_users),
                        "needed": str(wider),
                    },
                )
            if wider > max_slot:
                # Past the last number any of these locks holds. Searching on
                # would only read indices no lock has, and a lock cannot hand
                # back a slot it does not have -- every one of them would
                # come back occupied, forever.
                return None, *self._too_far(
                    num_users, max_slot, limiting_lock, needed=wider
                )
            window = wider

        # No capacity check here: every window this loop accepted was checked
        # before it was accepted -- the first as the bare count, each wider
        # one before widening to it -- and allocation only issues numbers
        # inside the window.
        return frozenset(unavailable), {}, {}

    async def _create_entry(
        self, *, title: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create the config entry."""
        return self.async_create_entry(  # type: ignore[attr-defined]
            title=title, data=data
        )


class LockCodeManagerFlowHandler(
    _AllocatesSlotsMixin, config_entries.ConfigFlow, domain=DOMAIN
):
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

        if user_input is not None:
            self.title = user_input.pop(CONF_NAME)
            await self.async_set_unique_id(slugify(self.title))
            self._abort_if_unique_id_configured()
            self.data = user_input
            self._allocation_locks = user_input[CONF_LOCKS]
            return await self.async_step_choose_path()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): cv.string,
                    vol.Required(CONF_LOCKS): LOCK_ENTITY_SELECTOR,
                }
            ),
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
            # is one the user can still act on.
            (
                unavailable,
                errors,
                description_placeholders,
            ) = await self._async_allocate_for(num_users)
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
        return await self._create_entry(title=self.title, data=self.data)

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
                self.hass, user_input[CONF_USERS], self.data[CONF_LOCKS]
            )
            errors.update(validation_errors)
            description_placeholders.update(validation_placeholders)

            if not errors:
                assert users is not None
                # Same allocation the guided path uses, against the same
                # occupancy: nobody picks a number on either route, so
                # neither can land on one a lock already holds.
                (
                    unavailable,
                    allocation_errors,
                    allocation_placeholders,
                ) = await self._async_allocate_for(len(users))
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
                return await self._create_entry(title=self.title, data=self.data)

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
            existing_slots = get_entry_config(config_entry).slot_numbers
            additional_errors, additional_placeholders = _check_common_slots(
                self.hass,
                user_input[CONF_LOCKS],
                existing_slots,
                config_entry,
            )
            if not additional_errors:
                # Reauth is where a lock gets swapped, so it is also where an
                # already-valid slot set can become too large for the new lock.
                (
                    additional_errors,
                    additional_placeholders,
                ) = await _async_check_slot_capacity(
                    self.hass, user_input[CONF_LOCKS], existing_slots
                )
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


class LockCodeManagerOptionsFlow(_AllocatesSlotsMixin, config_entries.OptionsFlow):
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
            self._allocation_locks = user_input[CONF_LOCKS]
            # Its own numbers do not constrain it: kept ones are held by
            # tenure, released ones are free for whoever comes next.
            self._entry_being_edited = self.config_entry
            (
                users,
                validation_errors,
                validation_placeholders,
            ) = await _async_validate_users_yaml(
                self.hass, user_input[CONF_USERS], user_input[CONF_LOCKS]
            )
            errors.update(validation_errors)
            description_placeholders.update(validation_placeholders)

            if not errors:
                assert users is not None
                (
                    unavailable,
                    allocation_errors,
                    allocation_placeholders,
                ) = await self._async_allocate_for(len(users))
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
