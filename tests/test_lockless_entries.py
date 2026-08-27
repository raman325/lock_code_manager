"""Tests for entries that manage no locks."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_SOURCE,
    ATTR_TARGET,
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.domain.validation import validate_credential

KEYPAD_STATUS_ENTITY = "binary_sensor.front_keypad_status"
READER_ENTITY = "event.front_keypad_scanned"

SLOT_1_ACTIVE = "binary_sensor.lockless_test1_active"
SLOT_1_PIN = "text.lockless_test1_pin"


def _config(**overrides):
    """Return a lock-less entry configuration."""
    return {
        CONF_LOCKS: [],
        # Same input shape the rest of the suite uses, converted on the way
        # in, so this test is not asserting against a config shape it
        # invented for itself.
        CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True}},
        **overrides,
    }


async def _setup(hass: HomeAssistant, **overrides) -> MockConfigEntry:
    """Set up a lock-less entry through the ordinary setup path."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=_config(**overrides), unique_id="Lockless", title="Lockless"
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_lockless_entry_manages_credentials(hass: HomeAssistant) -> None:
    """
    An entry with no locks is a working configuration, not a broken one.

    This is the whole of what a keypad this integration cannot talk to needs:
    the credential is checked against the entry's own users, so an entry that
    never programmes a device still answers for it.
    """
    entry = await _setup(hass)

    assert entry.state is ConfigEntryState.LOADED

    # Live, not merely registered: an entity that exists but reads
    # `unavailable` answers nothing.
    assert (pin := hass.states.get(SLOT_1_PIN)) is not None
    assert pin.state == "1234"
    assert (active := hass.states.get(SLOT_1_ACTIVE)) is not None
    assert active.state == "on"

    result = validate_credential(entry, "1234")
    assert result.valid
    assert result.user == "test1"

    rejected = validate_credential(entry, "9999")
    assert not rejected.valid
    assert rejected.user is None


async def test_lockless_entry_answers_use_credential(hass: HomeAssistant) -> None:
    """
    The action reaches a lock-less entry by name.

    Naming the entry is the only way in now, which is the point: there is no
    member to name, and the keypad that would be named is not one.
    """
    await _setup(hass)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            "config_entry_title": "Lockless",
            ATTR_CODE: "1234",
            ATTR_SOURCE: READER_ENTITY,
            ATTR_TARGET: READER_ENTITY,
        },
        blocking=True,
        return_response=True,
    )

    assert response["valid"] is True
    assert response["user"] == "test1"


async def test_lockless_entry_stays_available(
    hass: HomeAssistant,
) -> None:
    """
    Nothing to follow is not the same as nothing being up.

    An entry with no locks has nothing that could go down, so its entities
    must not present as unavailable.
    """
    await _setup(hass)

    assert hass.states.get(SLOT_1_ACTIVE).state != STATE_UNAVAILABLE

    # A state change somewhere the entry never named must not reach it.
    hass.states.async_set(KEYPAD_STATUS_ENTITY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert hass.states.get(SLOT_1_ACTIVE).state != STATE_UNAVAILABLE
