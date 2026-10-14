"""
Guards for behaviour the retired slot-coordinator lifecycle tests held.

The old slot coordinator ran one sync manager per lock and fanned changes
out to them; the lock workers and ``async_want`` do that now. These tests
hold the parts nothing else did.
"""

from __future__ import annotations

from functools import partial

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.domain.lock_runtime import (
    signal_lock_changed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    async_set_slot_pin,
    mock_lock,
)


async def _entry(hass: HomeAssistant, data: dict, unique_id: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_a_change_on_one_lock_reaches_only_that_locks_views(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A code changed on one lock is the business of its views, not the other lock's."""
    heard: list[str] = []
    unsubs = [
        async_dispatcher_connect(
            hass,
            signal_lock_changed(lock_entity_id),
            partial(heard.append, lock_entity_id),
        )
        for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    ]
    try:
        lock = mock_lock(lock_code_manager_config_entry, LOCK_1_ENTITY_ID)
        lock.codes[1] = "0000"
        lock._push_credential_update(1, SlotCredential.known("0000"))
        await hass.async_block_till_done()
    finally:
        for unsub in unsubs:
            unsub()

    assert heard
    assert set(heard) == {LOCK_1_ENTITY_ID}


async def test_a_slot_taken_on_one_lock_still_reaches_the_entrys_other_lock(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A PIN change goes to every lock the entry holds: one where another entry
    owns the slot refuses it, and the others still get it.
    """
    first = await _entry(hass, TWO_SLOTS_ON_LOCK_1, "first")
    second = await _entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
            CONF_SLOTS: {
                2: {CONF_NAME: "other2", CONF_PIN: "2222", CONF_ENABLED: True},
            },
        },
        "second",
    )
    assert list(second.runtime_data.locks) == [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]

    await async_set_slot_pin(hass, second, 2, "3333")
    await hass.async_block_till_done()

    assert mock_lock(second, LOCK_2_ENTITY_ID).codes[2] == "3333"
    assert mock_lock(first, LOCK_1_ENTITY_ID).codes[2] == "5678"
