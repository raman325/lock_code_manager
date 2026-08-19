"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
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
    CONF_SLOTS,
    CONF_USERS,
    DEFAULT_NUM_USERS,
    DOMAIN,
    EXCLUDED_CONDITION_PLATFORMS,
)
from .domain.config import EntryConfig
from .domain.credentials import CredentialType
from .domain.exceptions import LockCodeManagerError
from .domain.models import SlotCredential
from .domain.names import name_error, normalize_name, validate_slot_names
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
        vol.Optional(CONF_ENTITY_ID): sel.EntitySelector(
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


CODE_SLOTS_SCHEMA = vol.All(
    vol.Schema({vol.Coerce(int): CODE_SLOT_SCHEMA}), enabled_requires_pin
)

LOCKS_FILTER_CONFIG = [
    sel.EntityFilterSelectorConfig(integration=platform, domain=LOCK_DOMAIN)
    for platform in INTEGRATIONS_CLASS_MAP
]
LOCK_ENTITY_SELECTOR = sel.EntitySelector(
    sel.EntitySelectorConfig(filter=LOCKS_FILTER_CONFIG, multiple=True)
)
SLOTS_YAML_SELECTOR = sel.ObjectSelector(sel.ObjectSelectorConfig())


POSITIVE_INT = vol.All(vol.Coerce(int), vol.Range(min=1))


# The per-slot wording for the errors ``validate_slot_names`` reports.
_SLOT_NAME_ERRORS = {
    "name_required": "slot_name_required",
    "name_not_unique": "slot_name_not_unique",
}


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

    Only locks whose credential index IS the slot number are checked (see
    ``BaseLock.credential_index_follows_slot``) -- Matter allocates its own
    index, so a slot number above its credential count is perfectly legal
    there. Catching this at configuration time is what issue #1398 asked
    for: the write would otherwise fail forever on the device with nothing
    but a connectivity warning to show for it.

    A lock that cannot be queried is skipped rather than blocking the flow.
    Capabilities need the lock awake, and a battery lock that happens to be
    asleep must not make the config flow unusable; the same check runs again
    at write time, where it can suspend the affected slot precisely.
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


async def _async_validate_slots_yaml(
    hass: HomeAssistant,
    raw_slots: dict[Any, Any],
    locks: Iterable[str],
    config_entry: ConfigEntry | None = None,
) -> tuple[dict | None, dict, dict]:
    """
    Validate a slots-YAML submission for the config and options flows.

    Runs ``CODE_SLOTS_SCHEMA`` over ``raw_slots`` and, when it parses cleanly,
    checks for slots already configured on the same locks by another entry and
    for slot numbers beyond what the locks can hold. Returns the parsed slots
    (or ``None`` if validation failed) along with the accumulated error and
    description-placeholder dicts.
    """
    try:
        parsed_slots = CODE_SLOTS_SCHEMA(raw_slots)
    except vol.Invalid as err:
        _LOGGER.error("Invalid YAML: %s", err)
        # A missing name is now the most likely reason a previously-valid
        # slots block fails, so name it rather than sending the user to the
        # logs for the one error we can predict.
        #
        # Match on the error PATH, not the rendered text. CONF_NAME is
        # "name", which is a substring of any key containing it
        # ("friendly_name", "username"), so a text match reports an
        # unrelated schema failure as a missing name -- sending the user to
        # exactly the logs this branch exists to avoid.
        if CONF_NAME in getattr(err, "path", []):
            return None, {"base": "name_required"}, {}
        return None, {"base": "invalid_config"}, {}

    # The single-slot flow checks one name at a time; these paths submit the
    # whole set, so the set has to be checked together.
    if problem := validate_slot_names(parsed_slots):
        slot_num, error = problem
        # This is the one path that knows which slot failed, so it uses the
        # messages that name one. Everywhere else the user is looking at a
        # single user and there is no slot number to give -- the shared
        # messages must not ask for one, or they render as a translation
        # error instead of an explanation.
        return None, {"base": _SLOT_NAME_ERRORS[error]}, {"slot_num": slot_num}

    errors, placeholders = _check_common_slots(hass, locks, parsed_slots, config_entry)
    if not errors:
        errors, placeholders = await _async_check_slot_capacity(
            hass, locks, parsed_slots
        )
    return parsed_slots, errors, placeholders


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


@dataclass(frozen=True, slots=True)
class _LockQuery:
    """What one lock answered when asked what codes it is holding."""

    lock_entity_id: str
    # ``None`` means the read FAILED. An empty mapping means the lock answered
    # and holds nothing. Both render as "nothing to confirm" here; the
    # difference matters to allocation, which reads occupancy itself rather
    # than from this.
    codes: dict[int, SlotCredential] | None


async def _async_query_locks(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    lock_entity_ids: Iterable[str],
) -> list[_LockQuery]:
    """
    Ask every lock what credentials it holds.

    Queried sequentially to avoid flooding a Z-Wave or Matter network with
    simultaneous requests.

    Feeds the confirmation that shows which of the slots a user asked for
    already hold codes. Allocation does not read this -- it asks the locks
    itself, over the range it is considering -- so a lock that could not be
    read simply contributes nothing to show.
    """
    results: list[_LockQuery] = []
    for lock_entity_id in lock_entity_ids:
        try:
            lock_instance = _async_build_lock_instance(
                hass, dev_reg, ent_reg, lock_entity_id
            )
        except _LockQuerySkipped:
            results.append(_LockQuery(lock_entity_id=lock_entity_id, codes=None))
            continue

        try:
            codes = await lock_instance.async_internal_get_usercodes()
        except LockCodeManagerError as err:
            _LOGGER.warning("Failed to get usercodes from %s: %s", lock_entity_id, err)
            codes = None
        except Exception:
            _LOGGER.warning(
                "Failed to get usercodes from %s; this lock's codes will not be shown",
                lock_entity_id,
                exc_info=True,
            )
            codes = None
        results.append(_LockQuery(lock_entity_id, codes))
    return results


def _codes_by_lock(
    queries: Iterable[_LockQuery],
) -> dict[str, dict[int, SlotCredential]]:
    """Project the query results back to the lock/slot view the gate renders."""
    return {q.lock_entity_id: q.codes for q in queries if q.codes}


def _scope_codes_to_pairs(
    all_codes: dict[str, dict[int, SlotCredential]],
    pairs: Iterable[tuple[str, int]],
) -> dict[str, dict[int, SlotCredential]]:
    """Filter raw query results to only the ``(lock, slot)`` pairs given."""
    scoped_codes: dict[str, dict[int, SlotCredential]] = {}
    for lock, slot in pairs:
        if (code := all_codes.get(lock, {}).get(slot)) is not None:
            scoped_codes.setdefault(lock, {})[slot] = code
    return scoped_codes


class _ExistingCodesFlowMixin:
    """
    Mixin providing existing-codes detection and confirmation for config/options flows.

    When slots already have codes on the lock, this mixin shows a confirmation
    dialog listing which locks/slots are affected.  Clearing is NOT done here —
    the sync manager handles reconciliation when the config entry loads.
    """

    _all_codes: dict[str, dict[int, SlotCredential]]
    _occupied_lock_slots: list[tuple[str, int]]
    _next_step: Callable[[], Awaitable[dict[str, Any]]] | None

    def _init_existing_codes_state(self) -> None:
        """Initialize mixin state. Call from the inheriting flow's __init__."""
        self._all_codes = {}
        self._occupied_lock_slots = []
        self._next_step = None

    def _find_occupied_lock_slots(
        self, slot_nums: Iterable[int]
    ) -> list[tuple[str, int]]:
        """Return (lock_entity_id, slot_num) pairs that have non-empty codes."""
        return sorted(
            (lock_entity_id, slot_num)
            for slot_num in slot_nums
            for lock_entity_id, codes in self._all_codes.items()
            if (credential := codes.get(slot_num)) is not None and credential.is_present
        )

    @staticmethod
    def _format_occupied_slots(
        occupied: list[tuple[str, int]],
    ) -> str:
        """Format occupied lock/slot pairs for display in the confirmation dialog."""
        return "\n".join(
            f"- {lock_entity_id}: slot {slot_num}"
            for lock_entity_id, slot_num in occupied
        )

    async def _create_entry(
        self, *, title: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create the config entry."""
        return self.async_create_entry(  # type: ignore[attr-defined]
            title=title, data=data
        )

    async def async_step_existing_codes_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Confirm that existing codes will be overwritten by the sync manager."""
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="existing_codes_confirm",
            menu_options=["existing_codes_continue", "existing_codes_cancel"],
            description_placeholders={
                "details": self._format_occupied_slots(self._occupied_lock_slots),
            },
        )

    async def async_step_existing_codes_continue(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """User acknowledged existing codes. Proceed to next step."""
        if self._next_step is None:
            return self.async_abort(reason="unknown")  # type: ignore[attr-defined]
        return await self._next_step()

    async def async_step_existing_codes_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """User cancelled. Abort the flow."""
        return self.async_abort(reason="existing_codes_cancelled")  # type: ignore[attr-defined]


class LockCodeManagerFlowHandler(
    _ExistingCodesFlowMixin, config_entries.ConfigFlow, domain=DOMAIN
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
        self._init_existing_codes_state()

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
        for lock_entity_id in self.data[CONF_LOCKS]:
            try:
                lock_instance = _async_build_lock_instance(
                    self.hass, dev_reg, ent_reg, lock_entity_id
                )
            except _LockQuerySkipped as skipped:
                locks.append(
                    LockOccupancy(
                        lock_entity_id=lock_entity_id,
                        # A lock this integration writes to constrains the
                        # numbering, and without a provider there is no way
                        # to ask whether it addresses credentials by slot
                        # number -- so assume it does, which is the
                        # assumption that refuses rather than guesses. A lock
                        # on an unsupported platform is written to by nobody
                        # and constrains nothing.
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
                for lock_entity_id in self.data[CONF_LOCKS]
                for slot in get_managed_slots(self.hass, lock_entity_id)
            ),
        )

    async def _async_allocate_for(
        self, num_users: int
    ) -> tuple[frozenset[int] | None, dict[str, str], dict[str, Any]]:
        """
        Find numbers for ``num_users``, reading only as far as it has to.

        Locks that answer one index per round trip make the width of this
        read the cost of it, so it starts at the number of users and widens
        only by what turned out to be in the way -- and each pass asks only
        about the numbers no earlier pass covered. Every index is read at
        most once, so placing a handful of users never costs more round trips
        than the highest number they land on.

        Widening stops when the numbers it would need are past what a lock
        can hold, which is the same refusal as asking for too many users --
        because on a full lock that is what it is.

        It also stops on its own. Each pass either finds enough free numbers
        or discovers strictly more occupied ones than the pass before -- a
        pass that discovered no more would have found the window big enough,
        since the window is exactly the count plus what was in the way -- and
        the locks hold finitely many credentials.

        The count itself is checked before the first read: one no lock could
        ever hold is refused without asking a lock about it, rather than
        after a round trip per user.

        Returns the numbers allocation must avoid, verified across a window
        wide enough to hold everyone. The users themselves are numbered from
        it later, once they have names.
        """
        errors, placeholders = await _async_check_slot_capacity(
            self.hass, self.data[CONF_LOCKS], [num_users]
        )
        if errors:
            return (
                None,
                {"base": "too_many_users"},
                {**placeholders, "num_users": str(num_users)},
            )

        unavailable: set[int] = set()
        read_up_to = 0
        window = num_users
        while True:
            # Only the part nobody has asked about yet. Re-reading from one
            # every pass would cost a nearly-full lock several times its own
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
                self.hass, self.data[CONF_LOCKS], [wider]
            )
            if errors:
                # A different statement from the count being too large: the
                # count fits the lock, and the numbers it would have to reach
                # around what is already there do not. Saying "N users will
                # not fit" of a count the lock could hold reads as a bug, and
                # sends the user to re-interview a lock whose interview is
                # fine.
                return (
                    None,
                    {"base": "numbers_needed_exceed_capacity"},
                    {
                        **placeholders,
                        "num_users": str(num_users),
                        "needed": str(wider),
                    },
                )
            window = wider

        # No capacity check here: every window this loop accepted was checked
        # before it was accepted -- the first as the bare count, each wider
        # one before widening to it -- and allocation only issues numbers
        # inside the window.
        return frozenset(unavailable), {}, {}

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
            if entity_id := user_input.get(CONF_ENTITY_ID):
                entity_entry = self.ent_reg.async_get(entity_id)
                if (
                    entity_entry
                    and entity_entry.platform in EXCLUDED_CONDITION_PLATFORMS
                ):
                    errors[CONF_ENTITY_ID] = "excluded_platform"
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
        """Handle yaml flow step."""
        errors = {}
        description_placeholders = {}
        if not user_input:
            user_input = {}
        if user_input:
            (
                slots,
                validation_errors,
                validation_placeholders,
            ) = await _async_validate_slots_yaml(
                self.hass, user_input[CONF_SLOTS], self.data[CONF_LOCKS]
            )
            errors.update(validation_errors)
            description_placeholders.update(validation_placeholders)

            if not errors:
                assert slots is not None
                self.data[CONF_SLOTS] = slots
                # Read here rather than on the way in: only this path shows
                # the user what its chosen numbers already hold. The path
                # that chooses numbers itself reads what it needs, when it
                # knows how much of the lock that is.
                self._all_codes = _codes_by_lock(
                    await _async_query_locks(
                        self.hass, self.dev_reg, self.ent_reg, self.data[CONF_LOCKS]
                    )
                )
                self._occupied_lock_slots = self._find_occupied_lock_slots(slots.keys())
                if self._occupied_lock_slots:
                    self._next_step = partial(
                        self._create_entry,
                        title=self.title,
                        data=self.data,
                    )
                    return await self.async_step_existing_codes_confirm()
                return self.async_create_entry(title=self.title, data=self.data)

        return self.async_show_form(
            step_id="yaml",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SLOTS, default=user_input.get(CONF_SLOTS, {})
                    ): SLOTS_YAML_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=True,
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


class LockCodeManagerOptionsFlow(_ExistingCodesFlowMixin, config_entries.OptionsFlow):
    """Options flow for Lock Code Manager."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._init_existing_codes_state()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle a flow initialized by the user."""
        errors = {}
        description_placeholders = {}
        if not user_input:
            user_input = {}

        if user_input:
            (
                parsed_slots,
                validation_errors,
                validation_placeholders,
            ) = await _async_validate_slots_yaml(
                self.hass,
                user_input[CONF_SLOTS],
                user_input[CONF_LOCKS],
                self.config_entry,
            )
            errors.update(validation_errors)
            description_placeholders.update(validation_placeholders)

            if not errors:
                assert parsed_slots is not None
                user_input[CONF_SLOTS] = parsed_slots
                return await self._maybe_confirm_then_persist(user_input)

        # Plain dict/list, because the form selectors cannot serialize the
        # deeply read-only mappings EntryConfig uses internally.
        #
        # Seeded from the SLOT-shaped view, because this editor still takes
        # slot-shaped YAML. That is the next thing to change, and when it does
        # this becomes `config.users` and the projection disappears with it.
        config = get_entry_config(self.config_entry)
        defaults = {
            CONF_LOCKS: list(config.locks),
            CONF_SLOTS: {num: dict(slot) for num, slot in config.slots.items()},
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
                        CONF_SLOTS,
                        default=user_input.get(CONF_SLOTS, defaults[CONF_SLOTS]),
                    ): SLOTS_YAML_SELECTOR,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=True,
        )

    async def _maybe_confirm_then_persist(
        self, user_input: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Scan added (lock, slot) pairs for codes; show confirmation if any exist.

        Compares the submitted (lock, slot) pairs against the entry's
        current configuration. If any newly-added pair has a non-empty
        code on its lock, show the confirmation step before persisting.
        """
        diff = get_entry_config(self.config_entry) - EntryConfig.from_mapping(
            user_input
        )
        if not diff.pairs_added:
            return self.async_create_entry(title="", data=user_input)

        # Query only the locks involved in newly-added pairs
        locks_to_query = sorted({lock for lock, _ in diff.pairs_added})
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        all_codes = _codes_by_lock(
            await _async_query_locks(self.hass, dev_reg, ent_reg, locks_to_query)
        )

        # Scope to ONLY the added pairs so the confirmation dialog only
        # shows newly-added lock/slot pairs, not already-managed ones
        self._all_codes = _scope_codes_to_pairs(all_codes, diff.pairs_added)

        added_slot_nums = {slot for _, slot in diff.pairs_added}
        self._occupied_lock_slots = self._find_occupied_lock_slots(added_slot_nums)
        if not self._occupied_lock_slots:
            return self.async_create_entry(title="", data=user_input)

        self._next_step = partial(self._create_entry, title="", data=user_input)
        return await self.async_step_existing_codes_confirm()
