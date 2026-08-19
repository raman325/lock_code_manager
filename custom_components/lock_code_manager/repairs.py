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
    async_repoint,
    format_entities,
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
    Offer to repoint automations and scripts at the entity IDs that moved.

    The references are looked up now rather than when the issue was raised:
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
        """Show what can and cannot be repointed, then do the former."""
        referrers = await async_find_referrers(self.hass, self._moved)
        if not referrers.total:
            # Nothing refers to the old IDs, so there is nothing to confirm.
            async_delete_issue(self.hass, DOMAIN, self._issue_id)
            return self.async_create_entry(title="", data={})

        if user_input is not None:
            repointed = await async_repoint(self.hass, self._moved, referrers)
            for domain in referrers.fixable:
                # A file can hold configs for a component that is not loaded,
                # and it will read the new ids when it does load.
                if self.hass.services.has_service(domain, "reload"):
                    await self.hass.services.async_call(domain, "reload", blocking=True)
            return self.async_create_entry(title="", data={"repointed": repointed})

        fixable = sorted(referrers.labels)
        return self.async_show_form(
            step_id="init",
            description_placeholders={
                "renames": format_moved(self._moved),
                "fixable": "\n".join(f"- {label}" for label in fixable) or "- (none)",
                "unfixable": (
                    format_entities(self.hass, referrers.unfixable) or "- (none)"
                ),
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
            "entity_ids_renamed_",
        )
    ):
        return AcknowledgeRepairFlow()
    raise ValueError(f"Unknown issue: {issue_id}")
