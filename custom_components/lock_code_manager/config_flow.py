"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
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
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    DEFAULT_NUM_SLOTS,
    DEFAULT_START,
    DOMAIN,
    EXCLUDED_CONDITION_PLATFORMS,
)
from .data import EntryConfig, get_entry_config
from .exceptions import LockCodeManagerError, LockCodeManagerProviderError
from .models import SlotCode
from .providers import INTEGRATIONS_CLASS_MAP

_LOGGER = logging.getLogger(__name__)

CODE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PIN): cv.string,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_ENTITY_ID): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=CONDITION_ENTITY_DOMAINS)
        ),
    }
)


def enabled_requires_pin(data: dict[str, Any]) -> dict[str, Any]:
    """Validate that if enabled is True, pin is set."""
    if any(val.get(CONF_ENABLED) and not val.get(CONF_PIN) for val in data.values()):
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
                common_slots := sorted(set(config.slots) & {int(s) for s in slots_list})
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


class _LockQuerySkipped(LockCodeManagerError):
    """Raised when a lock should be skipped before any provider call."""


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
        raise _LockQuerySkipped(lock_entity_id)
    if lock_entry.platform not in INTEGRATIONS_CLASS_MAP:
        _LOGGER.debug(
            "Lock %s uses unsupported platform %s; skipping usercode check",
            lock_entity_id,
            lock_entry.platform,
        )
        raise _LockQuerySkipped(lock_entity_id)
    lock_config_entry = hass.config_entries.async_get_entry(lock_entry.config_entry_id)
    if lock_config_entry is None:
        _LOGGER.warning(
            "Config entry for lock %s not found; skipping usercode check",
            lock_entity_id,
        )
        raise _LockQuerySkipped(lock_entity_id)

    return INTEGRATIONS_CLASS_MAP[lock_entry.platform](
        hass, dev_reg, ent_reg, lock_config_entry, lock_entry
    )


async def _async_get_all_codes(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    lock_entity_ids: list[str],
) -> dict[str, dict[int, str | SlotCode]]:
    """
    Query locks for all usercodes.

    Returns ``codes_by_lock`` mapping each lock entity ID to its slot/code
    dict (``SlotCode.EMPTY`` for empty slots).  Locks that fail to query are
    skipped with logging.
    """
    result: dict[str, dict[int, str | SlotCode]] = {}
    # Query sequentially to avoid flooding networks (e.g. Z-Wave, Matter)
    # with simultaneous requests across multiple locks
    for lock_entity_id in lock_entity_ids:
        try:
            lock_instance = _async_build_lock_instance(
                hass, dev_reg, ent_reg, lock_entity_id
            )
            usercodes = await lock_instance.async_internal_get_usercodes()
        except _LockQuerySkipped:
            continue
        except LockCodeManagerProviderError as err:
            _LOGGER.warning(
                "Failed to get usercodes from %s: %s",
                lock_entity_id,
                err,
            )
            continue
        except LockCodeManagerError as err:
            _LOGGER.warning(
                "Failed to get usercodes from %s: %s",
                lock_entity_id,
                err,
            )
            continue
        except Exception:
            _LOGGER.warning(
                "Failed to get usercodes from %s; this lock's codes will not be shown",
                lock_entity_id,
                exc_info=True,
            )
            continue

        if usercodes:
            result[lock_entity_id] = usercodes
    return result


def _scope_codes_to_pairs(
    all_codes: dict[str, dict[int, str | SlotCode]],
    pairs: Iterable[tuple[str, int]],
) -> dict[str, dict[int, str | SlotCode]]:
    """Filter raw query results to only the ``(lock, slot)`` pairs given."""
    scoped_codes: dict[str, dict[int, str | SlotCode]] = {}
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

    _all_codes: dict[str, dict[int, str | SlotCode]]
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
            if codes.get(slot_num, SlotCode.EMPTY) != SlotCode.EMPTY
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

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize config flow."""
        self.data: dict[str, Any] = {}
        self.title: str = ""
        self.ent_reg: er.EntityRegistry = None
        self.dev_reg: dr.DeviceRegistry = None
        self.slots_to_configure: list[int] = []
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
            # Scan locks for existing codes once upfront
            self._all_codes = await _async_get_all_codes(
                self.hass, self.dev_reg, self.ent_reg, user_input[CONF_LOCKS]
            )
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
        """Handle a UI oriented flow."""
        errors = {}
        description_placeholders = {}
        if user_input is not None:
            start = user_input[CONF_START_SLOT]
            num_slots = user_input[CONF_NUM_SLOTS]
            additional_errors, additional_placeholders = _check_common_slots(
                self.hass, self.data[CONF_LOCKS], list(range(start, start + num_slots))
            )
            errors.update(additional_errors)
            description_placeholders.update(additional_placeholders)
            if not errors:
                self.slots_to_configure = list(range(start, start + num_slots))
                self._occupied_lock_slots = self._find_occupied_lock_slots(
                    self.slots_to_configure
                )
                if self._occupied_lock_slots:
                    self._next_step = self.async_step_code_slot
                    return await self.async_step_existing_codes_confirm()
                return await self.async_step_code_slot()

        return self.async_show_form(
            step_id="ui",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_START_SLOT, default=DEFAULT_START): POSITIVE_INT,
                    vol.Required(
                        CONF_NUM_SLOTS, default=DEFAULT_NUM_SLOTS
                    ): POSITIVE_INT,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=False,
        )

    async def async_step_code_slot(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Handle code slots step."""
        errors: dict[str, str] = {}
        current_slot = self.slots_to_configure[0]
        description_placeholders: dict[str, Any] = {"slot_num": current_slot}
        self.data.setdefault(CONF_SLOTS, {})

        if user_input is not None:
            if user_input.get(CONF_ENABLED) and not user_input.get(CONF_PIN):
                errors[CONF_PIN] = "missing_pin_if_enabled"

            # Check for excluded platforms with a single registry lookup
            # self.ent_reg is set in async_step_user which always runs first
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
                slot_num = int(self.slots_to_configure.pop(0))
                self.data[CONF_SLOTS][slot_num] = CODE_SLOT_SCHEMA(user_input)
                if not self.slots_to_configure:
                    return await self._create_entry(title=self.title, data=self.data)
                current_slot = self.slots_to_configure[0]
                description_placeholders["slot_num"] = current_slot

        return self.async_show_form(
            step_id="code_slot",
            data_schema=CODE_SLOT_SCHEMA,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=len(self.slots_to_configure) == 1,
        )

    async def async_step_yaml(self, user_input: dict[str, Any] | None = None):
        """Handle yaml flow step."""
        errors = {}
        description_placeholders = {}
        if not user_input:
            user_input = {}
        if user_input:
            try:
                slots = CODE_SLOTS_SCHEMA(user_input[CONF_SLOTS])
            except vol.Invalid as err:
                _LOGGER.error("Invalid YAML: %s", err)
                errors["base"] = "invalid_config"
            else:
                additional_errors, additional_placeholders = _check_common_slots(
                    self.hass, self.data[CONF_LOCKS], user_input[CONF_SLOTS]
                )
                errors.update(additional_errors)
                description_placeholders.update(additional_placeholders)

                if not errors:
                    self.data[CONF_SLOTS] = slots
                    self._occupied_lock_slots = self._find_occupied_lock_slots(
                        slots.keys()
                    )
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

    async def async_step_reauth(self, user_input: dict[str, Any]):
        """Handle import flow step."""
        config_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        errors = {}
        description_placeholders = {
            **self.context["title_placeholders"],
            "lock": self.context["lock_entity_id"],
        }

        if CONF_SLOTS not in user_input:
            assert config_entry
            additional_errors, additional_placeholders = _check_common_slots(
                self.hass,
                user_input[CONF_LOCKS],
                get_entry_config(config_entry).slots.keys(),
                config_entry,
            )
            errors.update(additional_errors)
            description_placeholders.update(additional_placeholders)
            if not errors:
                self.hass.config_entries.async_update_entry(
                    config_entry, data={**config_entry.data, **user_input}
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(config_entry.entry_id),
                    f"Reload config entry {config_entry.entry_id}",
                )
                return self.async_abort(reason="locks_updated")

        return self.async_show_form(
            step_id="reauth",
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
            try:
                user_input[CONF_SLOTS] = CODE_SLOTS_SCHEMA(user_input[CONF_SLOTS])
            except vol.Invalid as err:
                _LOGGER.error("Invalid YAML: %s", err)
                errors["base"] = "invalid_config"
            else:
                additional_errors, additional_placeholders = _check_common_slots(
                    self.hass,
                    user_input[CONF_LOCKS],
                    user_input[CONF_SLOTS],
                    self.config_entry,
                )
                errors.update(additional_errors)
                description_placeholders.update(additional_placeholders)

                if not errors:
                    return await self._maybe_confirm_then_persist(user_input)

        # Use to_dict() rather than .locks / .slots directly — to_dict
        # returns plain mutable dict/list, while EntryConfig.slots is a
        # deeply read-only MappingProxyType which the form selectors
        # can't JSON-serialize.
        defaults = get_entry_config(self.config_entry).to_dict()

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
        all_codes = await _async_get_all_codes(
            self.hass, dev_reg, ent_reg, locks_to_query
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
