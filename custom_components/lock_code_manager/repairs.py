"""Repairs for lock_code_manager."""

from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


class NumberOfUsesRemovedFlow(RepairsFlow):
    """Handler for the number_of_uses removal repair."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the init step — show the info and confirm button."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str] | None
) -> RepairsFlow:
    """Create a fix flow for a repair issue."""
    if issue_id == "number_of_uses_removed":
        return NumberOfUsesRemovedFlow()
    raise ValueError(f"Unknown issue: {issue_id}")
