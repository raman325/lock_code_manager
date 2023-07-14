"""Adds config flow for lock_code_manager."""
from __future__ import annotations

import logging
import pkgutil
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector as sel,
)

from . import providers
from .const import (
    CONF_LOCKS,
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    DEFAULT_NUM_SLOTS,
    DEFAULT_START,
    DOMAIN,
)
from .helpers import CODE_SLOT_SCHEMA, CODE_SLOTS_SCHEMA

_LOGGER = logging.getLogger(__name__)

FILTER_CONFIG = [
    sel.EntityFilterSelectorConfig(integration=integration, domain=LOCK_DOMAIN)
    for integration in [
        module.name
        for module in pkgutil.iter_modules(providers.__path__)
        if not module.ispkg and module.name not in ("_base", "const")
    ]
]

POSITIVE_INT = vol.All(vol.Coerce(int), vol.Range(min=1))


class LockCodeManagerFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Lock Code Manager."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize config flow."""
        self.data: dict[str, Any] = {}
        self.title: str = ""
        self.ent_reg: er.EntityRegistry = None
        self.dev_reg: dr.DeviceRegistry = None
        self.slots_to_configure: list[int] = []

    async def async_step_user(
        self, user_input: dict[str, Any] = None
    ) -> dict[str, Any]:
        """Handle a flow initialized by the user."""
        errors = {}
        placeholders = {}
        if not self.ent_reg:
            self.ent_reg = er.async_get(self.hass)
        if not self.dev_reg:
            self.dev_reg = dr.async_get(self.hass)

        if user_input is not None:
            if existing_locks := [
                lock
                for lock in user_input[CONF_LOCKS]
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if lock in entry.data[CONF_LOCKS]
            ]:
                errors[CONF_LOCKS] = "locks_already_configured"
                placeholders[CONF_LOCKS] = ", ".join(existing_locks)
            else:
                self.title = user_input.pop(CONF_NAME)
                self.data = user_input
                return await self.async_step_choose_path()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): cv.string,
                    vol.Required(CONF_LOCKS): sel.EntitySelector(
                        sel.EntitySelectorConfig(filter=FILTER_CONFIG, multiple=True)
                    ),
                }
            ),
            last_step=False,
        )

    async def async_step_choose_path(
        self, user_input: dict[str, Any] = None
    ) -> dict[str, Any]:
        """Allow user to choose a path for configuration."""
        return self.async_show_menu(step_id="choose_path", menu_options=["ui", "yaml"])

    async def async_step_ui(self, user_input: dict[str, Any] = None) -> dict[str, Any]:
        """Handle a UI oriented flow."""
        if user_input is not None:
            start = user_input[CONF_START_SLOT]
            num_slots = user_input[CONF_NUM_SLOTS]
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
            last_step=False,
        )

    async def async_step_code_slot(
        self, user_input: dict[str, Any] = None
    ) -> dict[str, Any]:
        """Handle code slots step."""
        self.data.setdefault(CONF_SLOTS, {})
        if user_input is not None:
            self.data[CONF_SLOTS][
                int(self.slots_to_configure.pop(0))
            ] = CODE_SLOT_SCHEMA(user_input)
            if not self.slots_to_configure:
                return self.async_create_entry(title=self.title, data=self.data)

        return self.async_show_form(
            step_id="code_slot",
            data_schema=CODE_SLOT_SCHEMA,
            description_placeholders={"slot_num": self.slots_to_configure[0]},
            last_step=len(self.slots_to_configure) == 1,
        )

    async def async_step_yaml(self, user_input: dict[str, Any] = None):
        """Handle yaml flow step."""
        errors = {}
        if user_input:
            try:
                slots = CODE_SLOTS_SCHEMA(user_input[CONF_SLOTS])
            except vol.Invalid as err:
                _LOGGER.error("Invalid YAML: %s", err)
                errors["base"] = "invalid_config"
            else:
                self.data[CONF_SLOTS] = slots
                return self.async_create_entry(title=self.title, data=self.data)

        return self.async_show_form(
            step_id="yaml",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SLOTS, default={}): sel.ObjectSelector(
                        # sel.ObjectSelectorConfig()
                    )
                }
            ),
            errors=errors,
            last_step=True,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return LockCodeManagerOptionsFlow(config_entry)


class LockCodeManagerOptionsFlow(config_entries.OptionsFlow):
    """Options flow for Lock Code Manager."""

    def __init__(self, config_entry: config_entries.ConfigEntry):
        """Initialize."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] = None
    ) -> dict[str, Any]:
        """Handle a flow initialized by the user."""
        errors = {}
        placeholders = {}
        if not user_input:
            user_input = {}

        if user_input:
            try:
                user_input[CONF_SLOTS] = CODE_SLOTS_SCHEMA(user_input[CONF_SLOTS])
            except vol.Invalid:
                errors["base"] = "invalid_config"
            else:
                if existing_locks := [
                    lock
                    for lock in user_input[CONF_LOCKS]
                    for entry in self.hass.config_entries.async_entries(DOMAIN)
                    if entry != self.config_entry and lock in entry.data[CONF_LOCKS]
                ]:
                    errors[CONF_LOCKS] = "locks_already_configured"
                    placeholders[CONF_LOCKS] = ", ".join(existing_locks)
                else:
                    return self.async_create_entry(title="", data=user_input)

        def _get_default(key: str, default: Any) -> Any:
            """Get default value."""
            return user_input.get(key, default)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LOCKS,
                        default=_get_default(
                            CONF_LOCKS,
                            self.config_entry.data[CONF_LOCKS],
                        ),
                    ): sel.EntitySelector(
                        sel.EntitySelectorConfig(filter=FILTER_CONFIG, multiple=True)
                    ),
                    vol.Required(
                        CONF_SLOTS,
                        default=_get_default(
                            CONF_SLOTS, self.config_entry.data[CONF_SLOTS]
                        ),
                    ): sel.ObjectSelector(sel.ObjectSelectorConfig()),
                }
            ),
            errors=errors,
            description_placeholders=placeholders,
            last_step=True,
        )
