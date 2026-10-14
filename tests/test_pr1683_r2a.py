"""Round 2a adversarial review of PR #1683: the integration's use of the core."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.lock_runtime import lock_registry

from .common import (
    LOCK_1_ENTITY_ID,
    async_advance_clock,
    current_entry,
    lcm_issue_ids,
    mock_lock,
    when_mock_lock_built,
)


async def test_a_user_removed_while_the_lock_is_dead_at_boot_still_leaves_it(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, mock_lock_config_entry
) -> None:
    """
    A restart with the lock unreachable defers its provider's setup.

    Removing a user then (revoking a dog walker's access) unclaimed the slot
    instead of clearing it: the worker never answered, the provider is not
    set up, so ``async_give_up`` took the lock for one never set up. When the
    lock came back the PIN stayed on it, with no repair -- and the
    unmanaged-code sweep only ever runs once.
    """
    entry = current_entry("r2a-dead-boot", {1: "1234", 2: "5678"})
    entry.add_to_hass(hass)

    def _dead(lock):
        # Its integration is up, the node is dead: setup is deferred.
        fault = lock.fail(None, error=LockDisconnected)
        original = lock.async_is_integration_connected

        async def _connected() -> bool:
            if fault in lock.faults:
                return False
            return await original()

        lock.async_is_integration_connected = _connected
        return fault

    with when_mock_lock_built(_dead) as faults:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        lock = mock_lock(entry)
        assert not lock.provider_setup_succeeded
        assert lock.codes[2] == "5678"

        subentry = next(s for s in entry.subentries.values() if s.title == "user2")
        hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        await hass.async_block_till_done()

        # The lock comes back.
        faults[0].clear()
        await async_advance_clock(hass, freezer, 60 * 60)

    assert lock.provider_setup_succeeded
    assert 2 not in lock.codes or any(
        "slot_clear_failed" in i or "slot_suspended" in i for i in lcm_issue_ids(hass)
    )


async def test_a_duplicate_refused_unsent_is_not_the_lock_answering(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The duplicate check refuses a PIN before anything is sent.

    It went to the worker as a refusal "sent", so it counted as the lock
    answering: during an outage it paid the tries owed by slots whose syncs
    got no answer (an outage charged to a normal slot), reset the lock's
    no-answers and marked a never-reached lock as answered.
    """
    entry = current_entry("r2a-dup-unsent", {1: "1234", 2: "5678"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    # Somebody's own code, in a slot nobody here manages.
    lock.codes[7] = "9999"
    await worker.read()
    answers = worker._answers

    lock.fail(None, error=LockDisconnected)
    # Slot 1's sync gets no answer (owed); slot 2 asks for that code's PIN.
    worker.want(1, "1111")
    worker.want(2, "9999")
    await hass.async_block_till_done()

    assert worker.slots[1].tries == 0
    assert worker._answers == answers
