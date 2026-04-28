"""Adds config flow for lock_code_manager."""

from __future__ import annotations

import asyncio
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
    CONF_NUMBER_OF_USES,
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

UI_CODE_SLOT_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_PIN): cv.string,
        vol.Required(CONF_ENABLED, default=True): cv.boolean,
        vol.Optional(CONF_ENTITY_ID): sel.EntitySelector(
            sel.EntitySelectorConfig(domain=CONDITION_ENTITY_DOMAINS)
        ),
    }
)

# Validation schema accepts number_of_uses for backward compatibility with
# existing config entries, but the UI schema above does not show it for new
# entries. number_of_uses is deprecated — use the Slot Usage Limiter blueprint.
CODE_SLOT_SCHEMA = UI_CODE_SLOT_SCHEMA.extend(
    {vol.Optional(CONF_NUMBER_OF_USES): vol.Coerce(int)}
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
    """Build a temporary lock provider instance for ``lock_entity_id``.

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
) -> tuple[dict[str, dict[int, str | SlotCode]], dict[str, Any]]:
    """Query locks for all usercodes.

    Returns ``(codes_by_lock, lock_instances_by_lock)`` where ``codes_by_lock``
    maps each lock entity ID to its slot/code dict (``SlotCode.EMPTY`` for
    empty slots) and ``lock_instances_by_lock`` retains the provider
    instances so the caller can reuse them when clearing slots. Locks that
    fail to query are skipped with logging.
    """
    result: dict[str, dict[int, str | SlotCode]] = {}
    lock_instances: dict[str, Any] = {}
    # Query sequentially to avoid flooding networks (e.g. Z-Wave, Matter)
    # with simultaneous requests across multiple locks
    for lock_entity_id in lock_entity_ids:
        try:
            lock_instance = _async_build_lock_instance(
                hass, dev_reg, ent_reg, lock_entity_id
            )
            usercodes = await lock_instance.async_internal_get_usercodes()
        except _LockQuerySkipped:
            # Already logged by _async_build_lock_instance with the
            # appropriate level for the specific skip reason
            continue
        except LockCodeManagerProviderError as err:
            # Real provider failure (e.g. LockDisconnected) — surface it
            # so users can see why a lock's codes weren't checked
            _LOGGER.warning(
                "Failed to get usercodes from %s: %s",
                lock_entity_id,
                err,
            )
            continue
        except LockCodeManagerError as err:
            # Defensive fallback for third-party providers that raise the
            # bare base class instead of LockCodeManagerProviderError.
            _LOGGER.warning(
                "Failed to get usercodes from %s: %s",
                lock_entity_id,
                err,
            )
            continue
        except Exception:  # noqa: BLE001
            # Last-resort catch: this runs in the user-facing config flow.
            # Any provider exception (including programmer error in a
            # third-party provider) must degrade to "no codes shown" rather
            # than aborting the flow.
            _LOGGER.warning(
                "Failed to get usercodes from %s; this lock's codes will not be shown",
                lock_entity_id,
                exc_info=True,
            )
            continue

        if usercodes:
            result[lock_entity_id] = usercodes
            lock_instances[lock_entity_id] = lock_instance
    return result, lock_instances


def _scope_codes_to_pairs(
    all_codes: dict[str, dict[int, str | SlotCode]],
    lock_instances: dict[str, Any],
    pairs: Iterable[tuple[str, int]],
) -> tuple[dict[str, dict[int, str | SlotCode]], dict[str, Any]]:
    """Filter raw query results to only the ``(lock, slot)`` pairs given."""
    scoped_codes: dict[str, dict[int, str | SlotCode]] = {}
    for lock, slot in pairs:
        if (code := all_codes.get(lock, {}).get(slot)) is not None:
            scoped_codes.setdefault(lock, {})[slot] = code
    scoped_instances = {lock: lock_instances[lock] for lock in scoped_codes}
    return scoped_codes, scoped_instances


class _ExistingCodesFlowMixin:
    """Mixin providing existing-codes detection, confirm UI, and clearing for config/options flows."""

    _all_codes: dict[str, dict[int, str | SlotCode]]
    _lock_instances: dict[str, Any]
    _slots_to_clear: list[int]
    _next_step: Callable[[], Awaitable[dict[str, Any]]] | None
    _clear_task: asyncio.Task[None] | None

    def _init_existing_codes_state(self) -> None:
        """Initialize mixin state. Call from the inheriting flow's __init__."""
        self._all_codes = {}
        self._lock_instances = {}
        self._slots_to_clear = []
        self._next_step = None
        self._clear_task = None

    def _slots_with_existing_codes(self, slot_nums: Iterable[int]) -> list[int]:
        """Return sorted slot numbers that have a non-empty code on any lock."""
        return sorted(
            slot_num
            for slot_num in slot_nums
            if any(
                codes.get(slot_num, SlotCode.EMPTY) != SlotCode.EMPTY
                for codes in self._all_codes.values()
            )
        )

    async def _clear_existing_slot(self, slot_num: int) -> None:
        """Clear a slot on every lock that has a non-empty code in it."""
        for lock_entity_id, codes in self._all_codes.items():
            if codes.get(slot_num, SlotCode.EMPTY) == SlotCode.EMPTY:
                continue
            lock_instance = self._lock_instances.get(lock_entity_id)
            if not lock_instance:
                _LOGGER.warning(
                    "No lock instance for %s; cannot clear slot %s",
                    lock_entity_id,
                    slot_num,
                )
                continue
            try:
                await lock_instance.async_internal_clear_usercode(
                    slot_num, source="direct"
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Failed to clear slot %s on %s",
                    slot_num,
                    lock_entity_id,
                    exc_info=True,
                )

    async def _clear_all_pending_slots(self) -> None:
        """Clear every slot in ``_slots_to_clear`` and reset state."""
        for slot_num in self._slots_to_clear:
            await self._clear_existing_slot(slot_num)
        self._slots_to_clear = []
        self._all_codes = {}
        self._lock_instances = {}

    async def _clear_then_create_entry(
        self, *, title: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Create the entry after slots have been cleared by the progress step."""
        return self.async_create_entry(  # type: ignore[attr-defined]
            title=title, data=data
        )

    async def async_step_existing_codes_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Confirm clearing of existing codes before proceeding."""
        return self.async_show_menu(  # type: ignore[attr-defined]
            step_id="existing_codes_confirm",
            menu_options=["existing_codes_clear", "existing_codes_cancel"],
            description_placeholders={
                "slots": ", ".join(str(s) for s in self._slots_to_clear),
            },
        )

    async def async_step_existing_codes_clear(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """User confirmed clearing. Show progress while clearing codes."""
        if self._next_step is None:
            return self.async_abort(reason="unknown")  # type: ignore[attr-defined]

        if self._clear_task is None:
            self._clear_task = self.hass.async_create_task(  # type: ignore[attr-defined]
                self._clear_all_pending_slots(),
                "Lock Code Manager: clear existing codes",
            )

        if not self._clear_task.done():
            num_locks = sum(
                1
                for codes in self._all_codes.values()
                if any(
                    codes.get(s, SlotCode.EMPTY) != SlotCode.EMPTY
                    for s in self._slots_to_clear
                )
            )
            return self.async_show_progress(  # type: ignore[attr-defined]
                step_id="existing_codes_clear",
                progress_action="clearing_codes",
                description_placeholders={
                    "slots": str(len(self._slots_to_clear)),
                    "locks": str(num_locks),
                },
                progress_task=self._clear_task,
            )

        self._clear_task = None
        return self.async_show_progress_done(  # type: ignore[attr-defined]
            next_step_id="existing_codes_done",
        )

    async def async_step_existing_codes_done(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Proceed to the next step after clearing finishes."""
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
            (
                self._all_codes,
                self._lock_instances,
            ) = await _async_get_all_codes(
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
                self._slots_to_clear = self._slots_with_existing_codes(
                    self.slots_to_configure
                )
                if self._slots_to_clear:
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
                    return await self._clear_then_create_entry(
                        title=self.title, data=self.data
                    )
                current_slot = self.slots_to_configure[0]
                description_placeholders["slot_num"] = current_slot

        return self.async_show_form(
            step_id="code_slot",
            data_schema=UI_CODE_SLOT_SCHEMA,
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
                    self._slots_to_clear = self._slots_with_existing_codes(slots.keys())
                    if self._slots_to_clear:
                        self._next_step = partial(
                            self._clear_then_create_entry,
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
        """Scan added (lock, slot) pairs for codes; confirm clear if any.

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
        all_codes, lock_instances = await _async_get_all_codes(
            self.hass, dev_reg, ent_reg, locks_to_query
        )

        # Scope to ONLY the added pairs so the mixin's clearing logic
        # cannot touch already-managed (lock, slot) pairs
        self._all_codes, self._lock_instances = _scope_codes_to_pairs(
            all_codes, lock_instances, diff.pairs_added
        )

        added_slot_nums = {slot for _, slot in diff.pairs_added}
        self._slots_to_clear = self._slots_with_existing_codes(added_slot_nums)
        if not self._slots_to_clear:
            return self.async_create_entry(title="", data=user_input)

        self._next_step = partial(
            self._clear_then_create_entry, title="", data=user_input
        )
        return await self.async_step_existing_codes_confirm()
