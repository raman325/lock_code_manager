"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
import logging
import pkgutil
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

from . import providers
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
from .data import get_entry_data
from .exceptions import LockCodeManagerError
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
    sel.EntityFilterSelectorConfig(integration=integration, domain=LOCK_DOMAIN)
    for integration in [
        module.name
        for module in pkgutil.iter_modules(providers.__path__)
        if not module.ispkg and module.name not in ("_base", "const")
    ]
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
            if lock in get_entry_data(entry, CONF_LOCKS, {})
            and (
                common_slots := sorted(
                    set(get_entry_data(entry, CONF_SLOTS, {})) & set(slots_list)
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


class _LockQuerySkipped(Exception):
    """Raised when a lock should be skipped before any provider call.

    Used internally by ``_async_build_lock_instance`` to signal one of the
    three expected setup-time skip conditions (missing entity, unsupported
    platform, missing config entry). This is distinct from
    ``LockCodeManagerError`` (raised by providers for real failures like
    ``LockDisconnected``) so the caller can log skips at DEBUG and real
    failures at WARNING.
    """


def _async_build_lock_instance(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    lock_entity_id: str,
) -> Any:
    """Build a temporary lock provider instance for ``lock_entity_id``.

    Performs setup-time checks (entity in registry, supported platform,
    parent config entry exists) and instantiates the provider class.

    Raises ``_LockQuerySkipped`` if any setup-time check fails. The provider
    constructor itself is not wrapped — failures there propagate as
    unexpected exceptions and are caught one level up.
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

    Returns a tuple of:
    - Dict keyed by lock entity ID, each value being a dict of slot number to
      code value (including SlotCode.EMPTY for empty slots). Callers must
      filter as needed.
    - Dict keyed by lock entity ID to temporary lock provider instances, for
      reuse in clearing slots.

    Locks are skipped (with logging) for three failure modes:
    - Setup-time skip (entity missing, unsupported platform, etc.) → DEBUG
    - Provider failure (e.g. ``LockDisconnected``) → WARNING with details
    - Unexpected exception → WARNING with traceback
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
        except _LockQuerySkipped:
            # Already logged by _async_build_lock_instance with the
            # appropriate level for the specific skip reason
            continue

        try:
            usercodes = await lock_instance.async_internal_get_usercodes()
        except LockCodeManagerError as err:
            # Real provider failure (e.g. LockDisconnected) — surface it
            # so users can see why a lock's codes weren't checked
            _LOGGER.warning(
                "Failed to get usercodes from %s: %s",
                lock_entity_id,
                err,
            )
            continue
        except Exception:  # noqa: BLE001
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


class _ExistingCodesFlowMixin:
    """Mixin providing existing-codes detection, confirm UI, and clearing.

    Inheriting flow must call ``_init_existing_codes_state()`` from its
    ``__init__``. Before showing the confirm step, populate ``_all_codes``
    and ``_lock_instances`` (typically from ``_async_get_all_codes``),
    set ``_slots_to_clear`` (typically via ``_slots_with_existing_codes``),
    and assign ``_next_step`` to the coroutine to run after the user
    confirms clearing.
    """

    _all_codes: dict[str, dict[int, str | SlotCode]]
    _lock_instances: dict[str, Any]
    _slots_to_clear: list[int]
    _next_step: Callable[[], Awaitable[dict[str, Any]]] | None

    def _init_existing_codes_state(self) -> None:
        """Initialize mixin state. Call from the inheriting flow's __init__."""
        self._all_codes = {}
        self._lock_instances = {}
        self._slots_to_clear = []
        self._next_step = None

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
        """User confirmed clearing. Run the next step."""
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

    async def _create_entry_and_clear_slots(self) -> dict[str, Any]:
        """Clear existing codes (already user-authorized), then create entry.

        The user explicitly confirmed clearing in the existing_codes_confirm
        step, so we do it before creating the entry. async_create_entry()
        only builds a FlowResult dict — the entry isn't persisted until
        after this step returns.
        """
        await self._clear_all_pending_slots()
        return self.async_create_entry(title=self.title, data=self.data)

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
                    return await self._create_entry_and_clear_slots()
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
                        self._next_step = self._create_entry_and_clear_slots
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
                get_entry_data(config_entry, CONF_SLOTS, {}).keys(),
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
        self._pending_options: dict[str, Any] = {}

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

        def _get_default(key: str) -> Any:
            """Get default value."""
            return user_input.get(key, get_entry_data(self.config_entry, key, {}))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOCKS, default=_get_default(CONF_LOCKS)
                    ): LOCK_ENTITY_SELECTOR,
                    vol.Required(
                        CONF_SLOTS, default=_get_default(CONF_SLOTS)
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
        old_locks: Iterable[str] = get_entry_data(self.config_entry, CONF_LOCKS, [])
        old_slots: Iterable[int] = get_entry_data(self.config_entry, CONF_SLOTS, {})
        old_pairs = {(lock, int(slot)) for lock in old_locks for slot in old_slots}

        new_locks = user_input[CONF_LOCKS]
        new_slots = user_input[CONF_SLOTS]
        new_pairs = {(lock, int(slot)) for lock in new_locks for slot in new_slots}

        added_pairs = new_pairs - old_pairs
        if not added_pairs:
            return self.async_create_entry(title="", data=user_input)

        # Query only the locks involved in newly-added pairs
        locks_to_query = sorted({lock for lock, _ in added_pairs})
        ent_reg = er.async_get(self.hass)
        dev_reg = dr.async_get(self.hass)
        all_codes, lock_instances = await _async_get_all_codes(
            self.hass, dev_reg, ent_reg, locks_to_query
        )

        # Scope _all_codes to ONLY the added pairs so the mixin's
        # clearing logic doesn't touch already-managed (lock, slot) pairs
        scoped_codes: dict[str, dict[int, str | SlotCode]] = {}
        for lock, slot in added_pairs:
            if (code := all_codes.get(lock, {}).get(slot)) is not None:
                scoped_codes.setdefault(lock, {})[slot] = code
        self._all_codes = scoped_codes
        self._lock_instances = {lock: lock_instances[lock] for lock in scoped_codes}

        added_slot_nums = {slot for _, slot in added_pairs}
        self._slots_to_clear = self._slots_with_existing_codes(added_slot_nums)
        if not self._slots_to_clear:
            return self.async_create_entry(title="", data=user_input)

        self._pending_options = user_input
        self._next_step = self._persist_options_and_clear_slots
        return await self.async_step_existing_codes_confirm()

    async def _persist_options_and_clear_slots(self) -> dict[str, Any]:
        """Clear confirmed slots, then persist the pending options."""
        await self._clear_all_pending_slots()
        return self.async_create_entry(title="", data=self._pending_options)
