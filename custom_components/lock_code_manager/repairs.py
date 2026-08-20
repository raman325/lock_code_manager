"""Repairs for lock_code_manager."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import async_delete_issue

from .const import DOMAIN
from .domain.exceptions import LockCodeManagerError
from .domain.locks import get_managed_lock
from .domain.references import (
    async_find_referrers,
    format_labels,
    format_moved,
)
from .domain.unmanaged import UNMANAGED_ISSUE_KEY, unmanaged_issue_id

_LOGGER = logging.getLogger(__name__)


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


class UnmanagedCodeRepairFlow(RepairsFlow):
    """
    Decide what happens to a code Lock Code Manager does not manage.

    Offers the two answers as a menu rather than a fix button and an
    ignore button, because neither is the "correct" one: the integration
    cannot tell a code somebody set at the keypad from one it stranded
    itself, and only the person reading the repair can.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        """Record which code this flow decides."""
        self._lock_entity_id: str = data["lock_entity_id"]
        self._slot: int = int(data["slot"])
        self.description_placeholders: dict[str, str] = {
            "lock": self._lock_entity_id,
            "slot": str(self._slot),
        }

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Offer the choice."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["clear", "keep"],
            description_placeholders=self.description_placeholders,
        )

    async def async_step_clear(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Clear the code off the lock."""
        try:
            lock = get_managed_lock(self.hass, self._lock_entity_id)
            await lock.async_internal_clear_usercode(self._slot)
        except LockCodeManagerError as err:
            _LOGGER.warning(
                "Could not clear slot %s on %s: %s",
                self._slot,
                self._lock_entity_id,
                err,
            )
            # Left standing deliberately: the code is still on the lock, so
            # resolving the issue would report a clear that did not happen.
            return self.async_abort(
                reason="unmanaged_code_clear_failed",
                description_placeholders={
                    **self.description_placeholders,
                    "error": str(err),
                },
            )
        return self.async_create_entry(title="", data={})

    async def async_step_keep(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Leave the code alone, and stop asking about it."""
        ir.async_get(self.hass).async_ignore(
            DOMAIN, unmanaged_issue_id(self._lock_entity_id, self._slot), True
        )
        return self.async_abort(
            reason="unmanaged_code_kept",
            description_placeholders=self.description_placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str] | None
) -> RepairsFlow:
    """Create a fix flow for a repair issue."""
    if issue_id.startswith(UNMANAGED_ISSUE_KEY):
        assert data is not None
        return UnmanagedCodeRepairFlow(data)
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
