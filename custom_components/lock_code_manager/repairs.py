"""Repairs for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .domain.exceptions import LockCodeManagerError
from .domain.locks import get_managed_lock
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
        lock = get_managed_lock(self.hass, self._lock_entity_id)
        try:
            await lock.async_internal_clear_usercode(self._slot)
        except LockCodeManagerError as err:
            _LOGGER.warning(
                "Could not clear slot %s on %s: %s",
                self._slot,
                self._lock_entity_id,
                err,
            )
            # The error alone cannot distinguish a lock that rejected the
            # clear from a link that dropped the reply. Whatever the
            # provider can measure about its transport goes in verbatim so
            # the reader can settle it at a glance (issue #1397, resurfaced
            # by #1307's failed clear).
            link_health = lock.describe_link_health()
            # Left standing deliberately: the code is still on the lock, so
            # resolving the issue would report a clear that did not happen.
            return self.async_abort(
                reason="unmanaged_code_clear_failed",
                description_placeholders={
                    **self.description_placeholders,
                    "error": str(err),
                    "link_health": f"\n\n{link_health}" if link_health else "",
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
