"""Repairs for lock_code_manager."""

from __future__ import annotations

import json

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers.issue_registry import async_delete_issue

from .const import DOMAIN
from .domain.references import (
    async_find_referrers,
    format_labels,
    format_moved,
)


class AcknowledgeRepairFlow(RepairsFlow):
    """Simple repair flow that just acknowledges the issue."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the confirm step."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init")


class EntityIdsRenamedRepairFlow(RepairsFlow):
    """
    Name the automations and scripts still pointing at an ID that moved.

    Reports rather than repairs. Rewriting somebody's ``automations.yaml``
    was tried and abandoned -- see :mod:`.domain.references`. Looking the
    references up here rather than when the issue was raised is deliberate:
    the migration runs while a config entry is setting up, before the
    automation component necessarily has its entities.
    """

    def __init__(self, issue_id: str, moved: dict[str, str]) -> None:
        """Store what moved, so the flow can look up who still points at it."""
        self._issue_id = issue_id
        self._moved = moved

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Show what still points at the old IDs, then let the user dismiss."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})

        referrers = await async_find_referrers(self.hass, self._moved)
        if not referrers.total:
            # Nothing points at the old IDs, so there is nothing to tell.
            async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "renames": format_moved(self._moved),
                "referrers": format_labels(referrers.labels),
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str] | None
) -> RepairsFlow:
    """Create a fix flow for a repair issue."""
    if issue_id.startswith("entity_ids_renamed_"):
        return EntityIdsRenamedRepairFlow(
            issue_id, json.loads((data or {}).get("moved") or "{}")
        )
    if issue_id.startswith(
        (
            "number_of_uses_removed",
            "slot_disabled_",
            "pin_required_",
            "slot_suspended_",
        )
    ):
        return AcknowledgeRepairFlow()
    raise ValueError(f"Unknown issue: {issue_id}")
