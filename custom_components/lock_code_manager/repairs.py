"""Repairs for lock_code_manager."""

from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import CONF_NUMBER_OF_USES, CONF_SLOTS, DOMAIN


class AcknowledgeRepairFlow(RepairsFlow):
    """Simple repair flow that just acknowledges the issue."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the confirm step."""
        if user_input is not None:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init")


class NumberOfUsesDeprecatedFlow(RepairsFlow):
    """
    Handler for the number_of_uses deprecation repair.

    When the user confirms, strips number_of_uses from all slot configs
    in the config entry.
    """

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the confirm step."""
        if user_input is not None:
            # Strip number_of_uses from all config entries
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                new_data = {**entry.data}
                new_options = {**entry.options}
                changed = False

                for data_dict in (new_data, new_options):
                    if CONF_SLOTS in data_dict:
                        new_slots = {}
                        for slot_num, slot_config in data_dict[CONF_SLOTS].items():
                            new_slot = {**slot_config}
                            if CONF_NUMBER_OF_USES in new_slot:
                                new_slot.pop(CONF_NUMBER_OF_USES)
                                changed = True
                            new_slots[slot_num] = new_slot
                        data_dict[CONF_SLOTS] = new_slots

                if changed:
                    self.hass.config_entries.async_update_entry(
                        entry, data=new_data, options=new_options
                    )

            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="init")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str] | None
) -> RepairsFlow:
    """Create a fix flow for a repair issue."""
    if issue_id == "number_of_uses_deprecated":
        return NumberOfUsesDeprecatedFlow()
    if issue_id.startswith(("slot_disabled_", "pin_required_", "slot_suspended_")):
        return AcknowledgeRepairFlow()
    raise ValueError(f"Unknown issue: {issue_id}")
