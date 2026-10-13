"""
Config, reauth and options flows against an entry that really sets up.

``test_config_flow.py`` bypasses entry setup for every test it holds, so what
a flow does to an entry Home Assistant then loads -- or fails to -- is
tested here.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import CONF_LOCKS
from custom_components.lock_code_manager.domain.queries import get_entry_config

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    async_configure_flow,
    current_entry,
    in_sync_entity_id,
)


class TestFlowsOnAFailedEntry:
    """Reauth and options on an entry that really failed to set up (a lock gone)."""

    @staticmethod
    async def _async_failed_entry(hass: HomeAssistant) -> MockConfigEntry:
        """
        Set up an entry on both locks after lock 2's entity was removed.

        Setup finds the lock missing, starts a reauth naming it, and fails.
        """
        er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)
        entry = current_entry(
            "Failed Entry", {1: "1234"}, locks=(LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
        )
        entry.add_to_hass(hass)
        # The component's own setup succeeds, which is what this returns.
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        if entry.state is not ConfigEntryState.SETUP_ERROR:
            pytest.fail(f"setup: the entry is {entry.state}, not failed")
        return entry

    async def test_reauth_renders_then_saves_and_loads(
        self, hass: HomeAssistant, mock_lock_config_entry: MockConfigEntry
    ) -> None:
        """
        Regression for #1348 (PR #1349): the reauth a removed lock starts works.

        The frontend opens the form by calling the step with no input; that
        crashed. Submitted, the reauth saves the locks and the entry loads.
        """
        entry = await self._async_failed_entry(hass)
        [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})

        result = await async_configure_flow(hass, flow["flow_id"], None)
        assert result["step_id"] == "reauth_confirm"
        assert result["description_placeholders"]["lock"] == LOCK_2_ENTITY_ID

        result = await async_configure_flow(
            hass, flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
        )
        await hass.async_block_till_done()

        assert result["reason"] == "locks_updated"
        assert entry.state is ConfigEntryState.LOADED
        assert get_entry_config(entry).locks == (LOCK_1_ENTITY_ID,)
        assert hass.states.get(in_sync_entity_id(hass, entry, 1, LOCK_1_ENTITY_ID))
        assert await hass.config_entries.async_unload(entry.entry_id)

    @pytest.mark.parametrize("flow_kind", ["reauth", "options"])
    async def test_locks_saved_on_a_failed_entry_survive_a_restart(
        self,
        hass: HomeAssistant,
        mock_lock_config_entry: MockConfigEntry,
        flow_kind: str,
    ) -> None:
        """
        Regressions for PRs #1344 and #1346: a fix saved while failed is kept.

        With the entry failed, no update listener applies an options save,
        and a later setup's migration used to overwrite what reauth wrote with
        the options left behind. Either flow's lock list must be the one the
        entry loads with, and loads with again after a restart.
        """
        entry = await self._async_failed_entry(hass)
        if flow_kind == "reauth":
            [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
            flow_id = flow["flow_id"]
            configure = hass.config_entries.flow.async_configure
        else:
            result = await hass.config_entries.options.async_init(entry.entry_id)
            flow_id = result["flow_id"]
            configure = hass.config_entries.options.async_configure

        result = await configure(flow_id, {CONF_LOCKS: [LOCK_1_ENTITY_ID]})
        await hass.async_block_till_done()
        assert result["reason"] == "locks_updated"
        assert entry.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert get_entry_config(entry).locks == (LOCK_1_ENTITY_ID,)
        assert not entry.options
        # The entry works, so nothing is left asking to reauthenticate it.
        assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
        assert await hass.config_entries.async_unload(entry.entry_id)
