"""Repairs for lock_code_manager."""

from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant


class AcknowledgeRepairFlow(RepairsFlow):
    """Simple repair flow that just acknowledges the issue."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the confirm step."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str] | None
) -> RepairsFlow:
    """Create a fix flow for a repair issue."""
    if issue_id.startswith(
        ("number_of_uses_removed", "slot_disabled_", "pin_required_", "slot_suspended_")
    ):
        return AcknowledgeRepairFlow()
    raise ValueError(f"Unknown issue: {issue_id}")
