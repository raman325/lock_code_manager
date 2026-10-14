"""Guards for behaviour the retired coordinator's tests (part 2) pinned."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, STATE_ON
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN

from .common import LOCK_1_ENTITY_ID, in_sync_entity_id, mock_lock


async def test_what_a_slot_wants_on_the_lock_follows_its_switch_and_pin(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    An enabled slot with a PIN puts it on the lock; anything else clears it.

    The lock starts holding codes in slots 1 and 2. Slot 1 is enabled with
    no PIN and slot 2 is disabled with one: both are cleared, never set to an
    empty PIN. Slot 3 is enabled with a PIN, which is written.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                1: {CONF_NAME: "blank", CONF_PIN: "", CONF_ENABLED: True},
                2: {CONF_NAME: "off", CONF_PIN: "5678", CONF_ENABLED: False},
                3: {CONF_NAME: "on", CONF_PIN: "4242", CONF_ENABLED: True},
            },
        },
        unique_id="Desired Codes",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    lock = mock_lock(entry)

    assert lock.codes == {3: "4242"}
    assert [call[0] for call in lock.service_calls["set_usercode"]] == [3]
    for slot_num in (1, 2, 3):
        assert hass.states.get(in_sync_entity_id(hass, entry, slot_num)).state == (
            STATE_ON
        )

    assert await hass.config_entries.async_unload(entry.entry_id)
