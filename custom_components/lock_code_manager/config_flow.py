"""Adds config flow for lock_code_manager."""

from __future__ import annotations

from collections.abc import Iterable
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


async def _async_get_unmanaged_codes(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    lock_entity_ids: list[str],
) -> tuple[dict[str, dict[int, str | SlotCode]], dict[str, Any]]:
    """Query locks for usercodes and return only unmanaged ones.

    Returns a tuple of:
    - Dict keyed by lock entity ID, each value being a dict of slot number to
      code value for slots not managed by any Lock Code Manager config entry.
      Empty slots (SlotCode.EMPTY) are excluded.
    - Dict keyed by lock entity ID to temporary lock provider instances, for
      reuse in clear/adopt steps.
    """
    result: dict[str, dict[int, str | SlotCode]] = {}
    lock_instances: dict[str, Any] = {}
    for lock_entity_id in lock_entity_ids:
        lock_entry = ent_reg.async_get(lock_entity_id)
        if not lock_entry:
            _LOGGER.warning(
                "Entity %s not found in registry; skipping usercode check",
                lock_entity_id,
            )
            continue
        if lock_entry.platform not in INTEGRATIONS_CLASS_MAP:
            _LOGGER.debug(
                "Lock %s uses unsupported platform %s; skipping usercode check",
                lock_entity_id,
                lock_entry.platform,
            )
            continue
        lock_config_entry = hass.config_entries.async_get_entry(
            lock_entry.config_entry_id
        )
        if lock_config_entry is None:
            _LOGGER.warning(
                "Config entry for lock %s not found; skipping usercode check",
                lock_entity_id,
            )
            continue
        lock_instance = INTEGRATIONS_CLASS_MAP[lock_entry.platform](
            hass, dev_reg, ent_reg, lock_config_entry, lock_entry
        )
        try:
            usercodes = await lock_instance.async_internal_get_usercodes()
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Failed to get usercodes from %s during lock reset check; "
                "this lock's codes will not be shown",
                lock_entity_id,
                exc_info=True,
            )
            continue
        # Note: some providers (Matter, Virtual) only return slots already
        # configured in a Lock Code Manager config entry, so unmanaged codes
        # on those providers will not be detected here. This reset step is
        # most useful for Z-Wave locks which return all occupied slots.
        managed_slots = {
            int(s)
            for entry in hass.config_entries.async_entries(DOMAIN)
            if lock_entity_id in get_entry_data(entry, CONF_LOCKS, [])
            for s in get_entry_data(entry, CONF_SLOTS, {})
        }
        unmanaged = {
            slot: code
            for slot, code in usercodes.items()
            if code is not SlotCode.EMPTY and slot not in managed_slots
        }
        if unmanaged:
            result[lock_entity_id] = unmanaged
            lock_instances[lock_entity_id] = lock_instance
    return result, lock_instances


class LockCodeManagerFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
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
        self._unmanaged_codes: dict[str, dict[int, str | SlotCode]] = {}
        self._lock_instances: dict[str, Any] = {}

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
            return await self.async_step_lock_reset()

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

    async def async_step_lock_reset(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Check for unmanaged codes on selected locks and show reset options."""
        if not self._unmanaged_codes:
            (
                self._unmanaged_codes,
                self._lock_instances,
            ) = await _async_get_unmanaged_codes(
                self.hass, self.dev_reg, self.ent_reg, self.data[CONF_LOCKS]
            )
        if not self._unmanaged_codes:
            return await self.async_step_choose_path()

        has_readable = any(
            code is not SlotCode.UNKNOWN
            for codes in self._unmanaged_codes.values()
            for code in codes.values()
        )

        menu_options = ["lock_reset_clear", "lock_reset_cancel"]
        if has_readable:
            menu_options = [
                "lock_reset_clear",
                "lock_reset_adopt",
                "lock_reset_cancel",
            ]

        slot_summary = ", ".join(
            f"{lock_id} (slots: {', '.join(str(s) for s in sorted(codes))})"
            for lock_id, codes in self._unmanaged_codes.items()
        )
        return self.async_show_menu(
            step_id="lock_reset",
            menu_options=menu_options,
            description_placeholders={"slot_summary": slot_summary},
        )

    async def async_step_lock_reset_clear(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Clear all unmanaged codes from the selected locks."""
        for lock_entity_id, codes in self._unmanaged_codes.items():
            lock_instance = self._lock_instances.get(lock_entity_id)
            if not lock_instance:
                _LOGGER.warning(
                    "No lock instance for %s; cannot clear unmanaged codes",
                    lock_entity_id,
                )
                continue
            for slot in codes:
                try:
                    await lock_instance.async_internal_clear_usercode(
                        slot, source="direct"
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to clear slot %s on %s",
                        slot,
                        lock_entity_id,
                        exc_info=True,
                    )
        self._unmanaged_codes = {}
        self._lock_instances = {}
        return await self.async_step_choose_path()

    async def async_step_lock_reset_adopt(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Adopt readable unmanaged codes into the Lock Code Manager configuration."""
        self.data.setdefault(CONF_SLOTS, {})
        for lock_entity_id, codes in self._unmanaged_codes.items():
            for slot, code in codes.items():
                if code is SlotCode.UNKNOWN:
                    continue
                if slot in self.data[CONF_SLOTS]:
                    existing_pin = self.data[CONF_SLOTS][slot].get(CONF_PIN)
                    if existing_pin != str(code):
                        _LOGGER.warning(
                            "Slot %s has conflicting PINs across locks "
                            "(keeping first seen PIN, skipping %s)",
                            slot,
                            lock_entity_id,
                        )
                        continue
                self.data[CONF_SLOTS][slot] = {
                    CONF_NAME: f"Slot {slot}",
                    CONF_PIN: str(code),
                    CONF_ENABLED: True,
                }

        # Clear masked codes that were not adopted
        for lock_entity_id, codes in self._unmanaged_codes.items():
            masked_slots = [s for s, c in codes.items() if c is SlotCode.UNKNOWN]
            if not masked_slots:
                continue
            lock_instance = self._lock_instances.get(lock_entity_id)
            if not lock_instance:
                _LOGGER.warning(
                    "No lock instance for %s; cannot clear masked codes",
                    lock_entity_id,
                )
                continue
            for slot in masked_slots:
                try:
                    await lock_instance.async_internal_clear_usercode(
                        slot, source="direct"
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to clear masked slot %s on %s",
                        slot,
                        lock_entity_id,
                        exc_info=True,
                    )

        self._unmanaged_codes = {}
        self._lock_instances = {}
        return await self.async_step_choose_path()

    async def async_step_lock_reset_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Cancel the config flow when user declines to handle unmanaged codes."""
        return self.async_abort(reason="lock_reset_cancelled")

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
        description_placeholders: dict[str, Any] = {
            "slot_num": self.slots_to_configure[0]
        }
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
                self.data[CONF_SLOTS][int(self.slots_to_configure.pop(0))] = (
                    CODE_SLOT_SCHEMA(user_input)
                )
                if not self.slots_to_configure:
                    return self.async_create_entry(title=self.title, data=self.data)
                description_placeholders["slot_num"] = self.slots_to_configure[0]

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
                    return self.async_create_entry(title=self.title, data=self.data)

        return self.async_show_form(
            step_id="yaml",
            data_schema=vol.Schema(
                {vol.Required(CONF_SLOTS, default=user_input): SLOTS_YAML_SELECTOR}
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


class LockCodeManagerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Lock Code Manager."""

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
                    return self.async_create_entry(title="", data=user_input)

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
