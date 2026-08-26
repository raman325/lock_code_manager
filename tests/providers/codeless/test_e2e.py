"""Full lifecycle tests for a lock the entry declares codeless."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.const import CONF_ENABLED, CONF_PIN, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_SOURCE,
    ATTR_TARGET,
    CONF_CODELESS,
    CONF_LOCKS,
    CONF_MEMBERS,
    CONF_USERS,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.domain.credentials import (
    MANAGED_CREDENTIAL_TYPES,
    CredentialType,
)
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)
from custom_components.lock_code_manager.domain.sync import TICK_INTERVAL
from custom_components.lock_code_manager.providers.codeless import CodelessLock

from ...common import (
    LOCK_1_ENTITY_ID,
    MockLCMLock,
    register_codeless_lock,
    slot_entity_id,
)
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


async def _entry_for(
    hass: HomeAssistant,
    *,
    unique_id: str,
    slot_num: int,
    pin: str,
    holding: Sequence[str],
    declaring: Sequence[er.RegistryEntry] = (),
) -> MockConfigEntry:
    """
    Set up an entry holding one user, declaring the members it is told to.

    Added and set up in one step because Home Assistant loads every entry of
    a domain when the domain itself loads: entries staged in advance are
    already running by the time the first ``async_setup`` returns, and a test
    about load order has to choose it.
    """
    name = f"{USER_NAME} {slot_num}"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: list(holding),
            CONF_MEMBERS: {
                lock_entry.id: {CONF_CODELESS: True} for lock_entry in declaring
            },
            CONF_USERS: {name: {CONF_PIN: pin, CONF_ENABLED: True}},
            CONF_SLOT_ASSIGNMENT: {name.casefold(): slot_num},
        },
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_taking_the_declaration_back_hands_the_lock_to_its_provider(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Answering no moves the lock onto its provider without waiting for a reload.

    The declaration is what picks the provider, so changing it changes what
    the member IS -- but the roster the update listener diffs carries the
    same entity id on both sides, so nothing rebuilt the instance. The answer
    reached storage and the lock went on being held by Lock Code Manager,
    which is the whole of what the menu offers a way out of.

    Written against a lock a provider does claim, because that is the only
    shape this exit exists for: a declared member whose platform gained a
    provider, being handed to it.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = await _entry_for(
        hass,
        unique_id="declaration_taken_back",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)

    held = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert isinstance(held, CodelessLock)
    assert (await held.async_get_usercodes())[1].readable_pin == "9999"

    hass.config_entries.async_update_entry(
        entry, options={**entry.data, CONF_MEMBERS: {}}
    )
    await hass.async_block_till_done()
    await _settle_sync(hass)

    handed_to = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert isinstance(handed_to, MockLCMLock)
    # The visible half: the credential is on the lock now, which is what
    # handing the member to its provider means.
    assert handed_to.codes[1] == "9999"

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("declare_first", [True, False])
async def test_the_entry_that_declared_the_lock_does_not_share_a_provider(
    hass: HomeAssistant, mock_lock_config_entry, declare_first: bool
) -> None:
    """
    Two entries that disagree about a lock get the provider each asked for.

    A shared instance was keyed on the lock alone, so an entry that declared
    a member codeless and one that did not were handed whichever instance
    loaded first, and the declaration decided nothing beyond load order.
    Loaded the wrong way round it wrote a user's Personal Identification
    Number to a device somebody had said must never be written to.

    Both orders are run because load order is exactly what the sharing was
    sensitive to.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)

    async def _declaring() -> MockConfigEntry:
        return await _entry_for(
            hass,
            unique_id="declaring_entry",
            slot_num=1,
            pin="9999",
            holding=[LOCK_1_ENTITY_ID],
            declaring=[claimed],
        )

    async def _plain() -> MockConfigEntry:
        return await _entry_for(
            hass,
            unique_id="plain_entry",
            slot_num=2,
            pin="8888",
            holding=[LOCK_1_ENTITY_ID],
        )

    first, second = (_declaring, _plain) if declare_first else (_plain, _declaring)
    await first()
    await second()
    await _settle_sync(hass)

    declaring = hass.config_entries.async_entry_for_domain_unique_id(
        DOMAIN, "declaring_entry"
    )
    plain = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, "plain_entry")
    declared_lock = declaring.runtime_data.locks[LOCK_1_ENTITY_ID]
    provider_lock = plain.runtime_data.locks[LOCK_1_ENTITY_ID]

    assert isinstance(declared_lock, CodelessLock)
    assert isinstance(provider_lock, MockLCMLock)
    # The entry that declared the lock kept its credential off the device;
    # the entry that did not put its own on it.
    assert (await declared_lock.async_get_usercodes())[1].readable_pin == "9999"
    assert provider_lock.codes[2] == "8888"
    assert provider_lock.codes[1] != "9999"

    for entry in (declaring, plain):
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_declaring_entry_releases_the_instance_it_holds(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Unloading one of two entries tears down the instance THAT entry holds.

    Release inverts the share, so it asks after the object being given up
    rather than after the entity id. Two entries that resolve one lock to
    different providers hold two instances, and an entry that read a
    sibling's entry in the roster as ownership walked away from its own --
    leaving it running, and skipping the unload that writes the Lock Code
    Manager store back. For a codeless member that store IS the credential,
    so every code the entry held would be gone on the next load.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    await _entry_for(
        hass,
        unique_id="plain_entry",
        slot_num=2,
        pin="8888",
        holding=[LOCK_1_ENTITY_ID],
    )
    declaring = await _entry_for(
        hass,
        unique_id="declaring_entry",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)

    assert await hass.config_entries.async_reload(declaring.entry_id)
    await hass.async_block_till_done()

    reloaded = declaring.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert isinstance(reloaded, CodelessLock)
    # Read before any tick, so this is what unload wrote rather than what a
    # sync would put back.
    assert (await reloaded.async_get_usercodes())[1].readable_pin == "9999"

    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)


async def test_deleting_the_entry_takes_the_stored_credentials_with_it(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    A deleted entry does not leave its users' codes on disk.

    Lock Code Manager is the store for a codeless member, so unload writes it
    back rather than removing it -- a reload must not be a reset. Entry
    deletion unloads first, so it took that same branch and SAVED, leaving
    every Personal Identification Number the entry held in ``.storage`` in
    cleartext, with nothing left in Home Assistant that could ever read it.
    """
    entry = await _entry_for(
        hass,
        unique_id="deleted_entry",
        slot_num=1,
        pin=USER_PIN,
        holding=[codeless_lock_entity.entity_id],
        declaring=[codeless_lock_entity],
    )
    await _settle_sync(hass)

    # A reload is the cheapest way to reach the save unload performs, which
    # is what puts the credential on disk in the first place.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    store_key = f"codeless_{DOMAIN}_{codeless_lock_entity.entity_id}"
    assert hass_storage[store_key]["data"]["1"]["code"] == USER_PIN

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert store_key not in hass_storage


async def test_a_member_whose_entity_is_gone_does_not_strand_the_others(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    A registry row can go before the entry does, and the sweep carries on.

    The store is named after the entity id, so a member whose row is gone
    cannot be named and its credentials are unreachable. That is a leak this
    cannot close -- but stopping there would leak every OTHER member's codes
    too, which is the difference between one orphaned file and the whole
    entry's. It is listed first, which is where that difference shows.
    """
    second = register_codeless_lock(hass, "second_door")
    entry = await _entry_for(
        hass,
        unique_id="entity_removed",
        slot_num=1,
        pin=USER_PIN,
        holding=[second.entity_id, codeless_lock_entity.entity_id],
        declaring=[second, codeless_lock_entity],
    )
    await _settle_sync(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    kept = f"codeless_{DOMAIN}_{codeless_lock_entity.entity_id}"
    assert hass_storage[kept]["data"]["1"]["code"] == USER_PIN

    er.async_get(hass).async_remove(second.entity_id)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert kept not in hass_storage


async def test_a_member_no_provider_claims_does_not_strand_the_others(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    A member nothing resolves never wrote a store, and is stepped over.

    An entry can hold one: setup drops it with a repair rather than refusing
    the whole entry, so the entry runs for as long as the user leaves it
    there. Deleting it must still collect the credentials the members that
    DID load were holding, so it is listed first.
    """
    unclaimed = register_codeless_lock(hass, "undeclared_door")
    entry = await _entry_for(
        hass,
        unique_id="unclaimed_member",
        slot_num=1,
        pin=USER_PIN,
        holding=[unclaimed.entity_id, codeless_lock_entity.entity_id],
        declaring=[codeless_lock_entity],
    )
    assert unclaimed.entity_id not in entry.runtime_data.locks
    await _settle_sync(hass)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    kept = f"codeless_{DOMAIN}_{codeless_lock_entity.entity_id}"
    assert hass_storage[kept]["data"]["1"]["code"] == USER_PIN

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert kept not in hass_storage


async def test_deleting_one_entry_leaves_a_sibling_entrys_credentials_alone(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    codeless_lock_entity: er.RegistryEntry,
) -> None:
    """
    A store two entries share is collected by the last one out, not the first.

    The store is named after the lock, not the entry, so a member both
    entries declare has one file behind it. Deleting either without asking
    would take the other's users' codes with it, and for a codeless member
    there is nowhere else those codes exist.
    """
    shared = {
        "holding": [codeless_lock_entity.entity_id],
        "declaring": [codeless_lock_entity],
    }
    keeping = await _entry_for(
        hass, unique_id="keeping_entry", slot_num=1, pin="1111", **shared
    )
    await _settle_sync(hass)
    # Nothing reaches disk while the instance is still held, and the entry
    # that shares it is not the one that writes.
    await hass.config_entries.async_unload(keeping.entry_id)
    assert await hass.config_entries.async_setup(keeping.entry_id)
    await hass.async_block_till_done()

    going = await _entry_for(
        hass, unique_id="going_entry", slot_num=2, pin="2222", **shared
    )
    await hass.config_entries.async_remove(going.entry_id)
    await hass.async_block_till_done()

    store_key = f"codeless_{DOMAIN}_{codeless_lock_entity.entity_id}"
    assert hass_storage[store_key]["data"]["1"]["code"] == "1111"

    await hass.config_entries.async_unload(keeping.entry_id)


async def test_a_sibling_on_a_different_provider_does_not_keep_the_store(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_lock_config_entry,
) -> None:
    """
    Only an entry that would READ this store keeps it from being collected.

    Two entries can disagree about one lock: one declares it codeless and
    keeps its codes in Lock Code Manager, the other declares nothing and
    writes them to the device. Gating collection on presence -- any entry
    still listing the lock -- meant the sibling blocked it forever, even
    though it never opens this file. The cleartext Personal Identification
    Numbers the sweep exists to remove survived the deletion, with nothing
    left that could ever read them back.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    declaring = await _entry_for(
        hass,
        unique_id="declaring_entry",
        slot_num=1,
        pin=USER_PIN,
        holding=[LOCK_1_ENTITY_ID],
        declaring=[claimed],
    )
    sibling = await _entry_for(
        hass,
        unique_id="sibling_entry",
        slot_num=2,
        pin="2222",
        holding=[LOCK_1_ENTITY_ID],
    )
    assert isinstance(sibling.runtime_data.locks[LOCK_1_ENTITY_ID], MockLCMLock)
    await _settle_sync(hass)

    # A reload is the cheapest way to reach the save unload performs, which
    # is what puts the credential on disk in the first place.
    assert await hass.config_entries.async_reload(declaring.entry_id)
    await hass.async_block_till_done()
    store_key = f"codeless_{DOMAIN}_{LOCK_1_ENTITY_ID}"
    assert hass_storage[store_key]["data"]["1"]["code"] == USER_PIN

    await hass.config_entries.async_remove(declaring.entry_id)
    await hass.async_block_till_done()

    assert store_key not in hass_storage

    await hass.config_entries.async_unload(sibling.entry_id)
