"""Round 1a adversarial review of PR #1683: the integration's use of the core."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from .common import async_advance_clock, current_entry, lcm_issue_ids, mock_lock


async def test_a_slot_removed_while_setup_revalidation_failed_is_not_silently_kept(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_lock_config_entry
) -> None:
    """
    A slot removed while the provider's setup flag is down still leaves the lock.

    ``provider_setup_succeeded`` drops back to False when a reconnect's
    revalidation fails, on a lock whose codes were written. Removing a slot
    then must not leave its PIN on the lock with nothing to show for it: it
    is cleared once setup recovers, or a repair says the code may remain.
    """
    entry = current_entry("r1a-setup", {1: "1111", 2: "2222"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    assert lock.codes[2] == "2222"

    # A reconnect's revalidation failed: the flag is down.
    lock._setup_succeeded = False

    subentry = next(s for s in entry.subentries.values() if s.title == "user2")
    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()

    # Revalidation recovers.
    lock._setup_succeeded = True
    # The backoff's probe finds it set up again.
    await async_advance_clock(hass, freezer, 300)

    assert 2 not in lock.codes or any(
        "slot_clear_failed" in i or "slot_suspended" in i for i in lcm_issue_ids(hass)
    )
