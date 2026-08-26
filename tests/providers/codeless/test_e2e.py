"""Full lifecycle tests for a lock the entry declares codeless."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_SOURCE,
    ATTR_TARGET,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.domain.credentials import (
    MANAGED_CREDENTIAL_TYPES,
    CredentialType,
)
from custom_components.lock_code_manager.domain.sync import TICK_INTERVAL
from custom_components.lock_code_manager.providers.codeless import CodelessLock

from ...common import slot_entity_id
from .conftest import USER_NAME, USER_PIN

# The keypad the code was typed on: an entity Lock Code Manager knows
# nothing about, which is how a use reaches a lock it cannot watch.
KEYPAD_ENTITY_ID = "sensor.front_door_keypad"


async def _settle_sync(hass: HomeAssistant) -> None:
    """Run the two ticks a slot takes to load and then write."""
    for tick in range(2):
        async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * (tick + 1) * 2)
        await hass.async_block_till_done()


async def test_the_declaration_is_what_sets_the_lock_up(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    An entry loads a lock nothing claims, because it declared it.

    The same entry without the declaration cannot be set up at all -- that
    refusal is what this feature is an exception to -- so setup completing
    is the whole assertion.
    """
    lock = lcm_config_entry.runtime_data.locks[codeless_lock_entity.entity_id]

    assert isinstance(lock, CodelessLock)
    assert lock.lock.entity_id == codeless_lock_entity.entity_id


async def test_the_credential_is_held_by_lock_code_manager(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    codeless_lock: CodelessLock,
) -> None:
    """
    A user's PIN is synced into Lock Code Manager's own store.

    The store is the assertion because it is the only place the credential
    can be: there is nothing on the device to write to, and a sync that
    silently did nothing would leave the slot looking configured while no
    code anywhere would validate.
    """
    await _settle_sync(hass)

    assert codeless_lock._data["1"]["code"] == USER_PIN
    assert codeless_lock._data["1"]["name"] == USER_NAME


async def test_a_use_reported_for_the_lock_reaches_the_users_event_entity(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    A lock Lock Code Manager cannot talk to still shows up in the history.

    Nothing observes this lock, so a use arrives through ``use_credential``:
    the caller says what was typed and where, the entry checks it against
    its users, and the use is recorded against the user who owns the code
    like any other. That recording is the visible half of what declaring a
    lock codeless buys.
    """
    await _settle_sync(hass)
    event_entity_id = slot_entity_id(
        hass, "event", lcm_config_entry, 1, EVENT_CREDENTIAL_USED
    )
    before = hass.states.get(event_entity_id)
    assert before
    assert before.state != STATE_UNAVAILABLE
    assert before.attributes[ATTR_EVENT_TYPES] == sorted(MANAGED_CREDENTIAL_TYPES)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            "code": USER_PIN,
            ATTR_SOURCE: KEYPAD_ENTITY_ID,
            ATTR_TARGET: codeless_lock_entity.entity_id,
            "config_entry_id": lcm_config_entry.entry_id,
        },
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response["valid"] is True
    assert response["user"] == USER_NAME

    recorded = hass.states.get(event_entity_id)
    assert recorded.state != before.state
    assert recorded.attributes[ATTR_EVENT_TYPE] == CredentialType.PIN
    assert recorded.attributes[ATTR_TARGET] == codeless_lock_entity.entity_id


async def test_an_unknown_code_is_rejected_for_the_lock(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    Holding the credentials means answering for them, including no.

    A codeless lock never rejects anything itself, so this answer is the
    only thing standing between a wrong code and whatever the caller does
    with a valid one.
    """
    await _settle_sync(hass)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            "code": "9999",
            ATTR_SOURCE: KEYPAD_ENTITY_ID,
            ATTR_TARGET: codeless_lock_entity.entity_id,
            "config_entry_id": lcm_config_entry.entry_id,
        },
        blocking=True,
        return_response=True,
    )

    assert response["valid"] is False
    assert response["user"] is None


async def test_the_credentials_survive_a_reload(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    Lock Code Manager is the only copy, so a reload must not be a reset.

    Every other provider can re-read its lock; this one has nowhere to
    re-read from, which makes the store the credential rather than a cache
    of it.
    """
    await _settle_sync(hass)

    assert await hass.config_entries.async_reload(lcm_config_entry.entry_id)
    await hass.async_block_till_done()

    lock = lcm_config_entry.runtime_data.locks[codeless_lock_entity.entity_id]
    assert isinstance(lock, CodelessLock)
    assert (await lock.async_get_usercodes())[1].readable_pin == USER_PIN
