"""
Guards for behaviour the retired lock coordinator's tests covered (part 1).

The lock-offline repair, end to end: what the user sees as a lock comes up,
is reached, is lost and comes back.
"""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.core.worker import BACKOFF_MAX, OFFLINE_AFTER
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected

from .common import (
    async_advance_clock,
    code_entity_id,
    current_entry,
    lcm_issue_ids,
    mock_lock,
    when_mock_lock_built,
)

# Long enough for any number of unanswered reads: the backoff is capped.
HOURS = 6 * 60 * 60


async def test_a_lock_is_reported_offline_once_reached_and_lost_until_it_is_back(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
) -> None:
    """
    A lock not reached since startup is never reported offline (#1257).

    Once reached and then lost it is, after it has gone unanswered long
    enough; the repair goes as soon as it answers again.
    """
    entry = current_entry("Offline Cycle", {1: "1234"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(
        lambda lock: lock.fail("get", error=LockDisconnected)
    ) as built:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    [starting] = built
    code = code_entity_id(hass, entry, 1)

    await async_advance_clock(hass, freezer, HOURS)
    assert starting.hits > OFFLINE_AFTER
    assert not lcm_issue_ids(hass, "lock_offline")
    assert hass.states.get(code).state == STATE_UNAVAILABLE

    starting.clear()
    await async_advance_clock(hass, freezer, BACKOFF_MAX + 60)
    assert hass.states.get(code).state == "1234"

    lost = mock_lock(entry).fail("get", error=LockDisconnected)
    await async_advance_clock(hass, freezer, HOURS)
    assert lcm_issue_ids(hass, "lock_offline")

    lost.clear()
    await async_advance_clock(hass, freezer, BACKOFF_MAX + 60)
    assert not lcm_issue_ids(hass, "lock_offline")
    assert await hass.config_entries.async_unload(entry.entry_id)
