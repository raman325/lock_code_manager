"""Adversarial review round 1a of the core swap: HA-level failing tests."""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.lock_runtime import (
    async_give_up,
    async_write_once,
    lock_registry,
    port_of,
)

from .common import LOCK_1_ENTITY_ID, TWO_SLOTS_ON_LOCK_1, mock_lock


async def test_f3_an_unmanaged_clear_after_a_release_is_a_plain_clear(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A slot given up long ago, now holding someone else's code.

    ``releasing`` keeps the slot after its release is done, so the card's
    clear of the unmanaged code goes out as a release: a native-user
    provider (Matter) deletes only an LCM-tagged user and leaves the code,
    and on a masked lock the clear is then trusted (#1437) -- reported done.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="rt")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)

    async_give_up(hass, entry, LOCK_1_ENTITY_ID, 2)
    await hass.async_block_till_done()
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker is not None and 2 not in worker.slots
    assert 2 not in lock.codes

    # Someone programs a code at the keypad; the user clears it from the card.
    lock.codes[2] = "9999"
    releases: list[int] = []

    async def _release(self, slot: int) -> None:
        releases.append(slot)  # no LCM-tagged user here: nothing deleted

    with patch.object(type(lock), "async_release_managed_slot", _release):
        done = await async_write_once(hass, LOCK_1_ENTITY_ID, 2, None)
        await hass.async_block_till_done()

    assert releases == []
    assert 2 not in lock.codes
    assert done is True
    assert 2 not in port_of(worker).releasing
