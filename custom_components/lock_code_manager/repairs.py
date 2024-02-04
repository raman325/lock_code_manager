"""Repairs for Lock Code Manager."""

from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, HACS_DOMAIN


class FoldEntityRowJSNotFoundFlow(RepairsFlow):
    """Handler for an issue fixing flow."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step of a fix flow."""
        return self.async_show_menu(menu_options=["fix", "ignore"])

    async def async_step_fix(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if HACS_DOMAIN not in self.hass.config.components or not (
            hacs := self.hass.data.get(HACS_DOMAIN)
        ):
            hacs
            return self.async_abort(reason="hacs_not_loaded")

        return self.async_abort(reason="hacs_not_loaded")
        return self.async_create_entry(title="", data={})

    async def async_step_ignore(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        ir.async_get(self.hass).async_ignore(DOMAIN, self.issue_id, True)
        return self.async_abort(reason="issue_ignored")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str] | None = None
) -> RepairsFlow:
    """Create flow."""

    if issue_id == "fold_entity_row_js_not_found":
        return FoldEntityRowJSNotFoundFlow()
    return ConfirmRepairFlow()
