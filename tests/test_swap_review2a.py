"""Adversarial review (round 2a) of the core swap: the integration's use of the core."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.lock_runtime import (
    GIVEN_UP_KEY,
    ISSUES_KEY,
)
from custom_components.lock_code_manager.repairs import async_create_fix_flow

from .common import LOCK_1_ENTITY_ID, lcm_issue_ids, mock_lock
from .test_swap_review1b import _entry, _remove_user_in_slot


async def test_a_held_release_acknowledged_after_a_restart_is_cleared(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Decision F: acknowledging slot_clear_failed clears the slot again.

    The issue is persistent, so it outlives a restart; what the integration
    remembers about slots given up does not. Acknowledged after a restart,
    the flow found nothing to resume, did nothing, and the repair went away
    with the code still on the lock.
    """
    entry = await _entry(hass)
    lock = mock_lock(entry)
    refusal = lock.refuse("clear", 1)
    await _remove_user_in_slot(hass, entry, 1)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    assert issue_id in lcm_issue_ids(hass, "slot_suspended")

    # A restart: the entry goes, and what was kept in memory with it.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.data[DOMAIN][GIVEN_UP_KEY].clear()
    hass.data[DOMAIN][ISSUES_KEY].clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    assert lock.codes.get(1) == "1234"
    assert issue_id in lcm_issue_ids(hass, "slot_suspended")
    refusal.clear()

    # The flow gets what the repair kept, as Home Assistant hands it over.
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    flow = await async_create_fix_flow(hass, issue_id, dict(issue.data or {}))
    flow.hass = hass
    await flow.async_step_init({})
    await hass.async_block_till_done()

    assert 1 not in lock.codes
