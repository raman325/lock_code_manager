"""Full lifecycle tests for a lock the entry declares codeless."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import (
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_SOURCE,
    ATTR_TARGET,
    CONF_CODELESS,
    CONF_LOCKS,
    CONF_MEMBERS,
    CONF_NUM_USERS,
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
    LOCK_2_ENTITY_ID,
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


async def test_a_second_entry_cannot_end_up_on_a_different_provider(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A lock one entry declares is declared by the next one, through the flow.

    Whether a lock keeps codes of its own is a fact about the DEVICE, so two
    entries answering differently is a contradiction rather than a
    configuration -- and one with teeth: two providers over one entity are
    two credential stores, and a Personal Identification Number lands in
    whichever the caller happened to reach. The second entry is driven
    through the real config flow here, because the flow is what makes the
    contradiction unrepresentable, and the assertion is on the provider the
    entry ends up RUNNING rather than on what it stored.

    Written against a lock a provider does claim, which is the only shape
    that can go wrong silently: an unclaimed one is asked about anyway.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    first = await _entry_for(
        hass,
        unique_id="first_entry",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)

    started = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    flow_id = started["flow_id"]
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "second", CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )

    # Asked, rather than quietly handed to the provider that claims the
    # lock: agreeing has to be an answer the second entry can give.
    assert result["step_id"] == "codeless_reconsider"
    assert result["description_placeholders"] == {"lock": LOCK_1_ENTITY_ID}

    await hass.config_entries.flow.async_configure(
        flow_id, {"next_step_id": "codeless_confirm"}
    )
    await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "ui"})
    await hass.config_entries.flow.async_configure(flow_id, {CONF_NUM_USERS: 1})
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_NAME: "Bea", CONF_ENABLED: True, CONF_PIN: "8888"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    second = hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, "second")
    # One entity, one provider: the second entry did not merely record the
    # same answer, it is driving the very instance the first one holds, so
    # there is no second store for a code to go missing in.
    assert (
        second.runtime_data.locks[LOCK_1_ENTITY_ID]
        is first.runtime_data.locks[LOCK_1_ENTITY_ID]
    )
    assert isinstance(second.runtime_data.locks[LOCK_1_ENTITY_ID], CodelessLock)

    for entry in (first, second):
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_lock_leaving_one_entry_leaves_the_sibling_untouched(
    hass: HomeAssistant, codeless_lock_entity: er.RegistryEntry
) -> None:
    """
    Dropping a shared member from one entry neither kills nor empties it.

    Both entries declare the lock, so both drive one instance and one store.
    The entry giving it up tears down only what nothing else holds -- release
    is the inverse of the share, asked about the object rather than about the
    roster -- and it discards the store only when nothing else reads it. Get
    either wrong and the sibling is left with a dead provider, or with every
    code it held gone: for a codeless member that store IS the credential,
    and there is nowhere else those codes exist.
    """
    keeping = await _entry_for(
        hass,
        unique_id="keeping_entry",
        slot_num=2,
        pin="2222",
        holding=[codeless_lock_entity.entity_id],
        declaring=[codeless_lock_entity],
    )
    giving_up = await _entry_for(
        hass,
        unique_id="giving_up_entry",
        slot_num=1,
        pin="1111",
        holding=[codeless_lock_entity.entity_id],
        declaring=[codeless_lock_entity],
    )
    await _settle_sync(hass)
    shared = keeping.runtime_data.locks[codeless_lock_entity.entity_id]
    assert giving_up.runtime_data.locks[codeless_lock_entity.entity_id] is shared

    hass.config_entries.async_update_entry(
        giving_up, options={**giving_up.data, CONF_LOCKS: [], CONF_MEMBERS: {}}
    )
    await hass.async_block_till_done()

    assert keeping.runtime_data.locks[codeless_lock_entity.entity_id] is shared
    # A reload is what would expose a discarded store: the codes come back
    # from disk or they do not come back at all. Read before any tick, so
    # this is what survived rather than what a sync put back.
    assert await hass.config_entries.async_reload(keeping.entry_id)
    await hass.async_block_till_done()
    reloaded = keeping.runtime_data.locks[codeless_lock_entity.entity_id]
    assert (await reloaded.async_get_usercodes())[2].readable_pin == "2222"

    for entry in (keeping, giving_up):
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


async def test_handing_the_lock_back_discards_the_codes_it_was_holding(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_lock_config_entry,
) -> None:
    """
    The ``codeless_reconsider`` question promises this in so many words.

    "The codes Lock Code Manager was holding for it are discarded" is true
    only through the redeclaration teardown: the roster carries the same
    entity id on both sides of the edit, so the codes go only because
    ``locks_redeclared`` feeds ``locks_to_remove`` and that removal is a
    permanent one. Drop either coupling and the promise silently becomes a
    lie -- a file of cleartext Personal Identification Numbers left on disk
    for a lock nothing reads it for any more.

    Driven through the options flow rather than a config write, because the
    sentence is about what answering "no" does.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = await _entry_for(
        hass,
        unique_id="handing_back",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)
    # A reload is the cheapest way to reach the save unload performs, which
    # is what puts the credential on disk in the first place.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    store_key = f"codeless_{DOMAIN}_{LOCK_1_ENTITY_ID}"
    assert hass_storage[store_key]["data"]["1"]["code"] == "9999"

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]
    users = {f"{USER_NAME} 1": {CONF_PIN: "9999", CONF_ENABLED: True}}
    result = await hass.config_entries.options.async_configure(
        flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
    )
    assert result["step_id"] == "codeless_reconsider"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "codeless_decline"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert store_key not in hass_storage

    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_options_decline_discards_them_with_the_entry_unloaded(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_lock_config_entry,
) -> None:
    """
    The listener that kept that promise is not there while the entry is not.

    ``async_update_listener`` is registered during setup and released on
    unload, so an options save against an entry that is not loaded reaches
    storage with nothing to act on it: no ``locks_redeclared``, no permanent
    removal, and a file of cleartext Personal Identification Numbers left on
    disk for a member the entry has just stopped declaring -- unreadable by
    anything, and swept by nothing, because entry deletion collects only
    what the entry still declares.

    The options form is reachable in that state, and the entry that comes
    back later is a working one, so nothing on screen ever says the answer
    was only half taken.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = await _entry_for(
        hass,
        unique_id="handing_back_unloaded",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)
    # Unload is both what puts the credential on disk and what takes the
    # listener away, which is the whole of the state under test.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    store_key = f"codeless_{DOMAIN}_{LOCK_1_ENTITY_ID}"
    assert hass_storage[store_key]["data"]["1"]["code"] == "9999"

    started = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = started["flow_id"]
    users = {f"{USER_NAME} 1": {CONF_PIN: "9999", CONF_ENABLED: True}}
    result = await hass.config_entries.options.async_configure(
        flow_id, user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_USERS: users}
    )
    assert result["step_id"] == "codeless_reconsider"

    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "codeless_decline"}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    assert store_key not in hass_storage

    # Loaded again, so the member is the provider's now and the store stays
    # gone: nothing rebuilt it out of what the unloaded instance still held.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert not isinstance(entry.runtime_data.locks[LOCK_1_ENTITY_ID], CodelessLock)
    assert store_key not in hass_storage

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_reauth_decline_discards_them_the_same_way(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_lock_config_entry,
) -> None:
    """
    Reauth shows the same sentence, so it has to mean the same thing there.

    Reauth is the one save that does not go through the update listener: it
    writes the entry and reloads, and the entry it is repairing is failed, so
    nothing was holding the lock for ``locks_redeclared`` to find and
    ``remove_permanently`` to collect. The promise on screen was therefore
    true on one path and false on the other, and the false one left a file of
    cleartext Personal Identification Numbers on disk for a lock that had
    just been handed back to its own integration -- unreadable by anything,
    and collected by nothing, because the entry that would have swept it on
    deletion no longer declares the member.

    A second lock whose registry row is gone is what puts the entry in
    reauth, which is the only way to reach that step.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = await _entry_for(
        hass,
        unique_id="reauth_handing_back",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)
    # Unload is what puts the credential on disk; the entry then fails to
    # come back because one of its locks no longer exists.
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    store_key = f"codeless_{DOMAIN}_{LOCK_1_ENTITY_ID}"
    assert hass_storage[store_key]["data"]["1"]["code"] == "9999"

    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    assert result["step_id"] == "codeless_reconsider"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "codeless_decline"}
    )
    assert result["type"] == "abort"
    await hass.async_block_till_done()

    assert store_key not in hass_storage

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_reauth_confirmation_leaves_the_codes_exactly_where_they_are(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    mock_lock_config_entry,
) -> None:
    """
    The other answer to the same question must not cost a single code.

    Discarding on decline and discarding on confirm are one line apart, and
    the wrong one of the two is silent: the user is asked whether Lock Code
    Manager should keep holding the codes, says yes, and every one of them is
    deleted on the way to saving that yes. There is nowhere else a codeless
    member's codes exist, so it is unrecoverable, and the entry that comes
    back looks configured -- the slots are all there, holding nothing.
    """
    claimed = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    entry = await _entry_for(
        hass,
        unique_id="reauth_keeping_them",
        slot_num=1,
        pin="9999",
        holding=[LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
        declaring=[claimed],
    )
    await _settle_sync(hass)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    store_key = f"codeless_{DOMAIN}_{LOCK_1_ENTITY_ID}"
    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    assert result["step_id"] == "codeless_reconsider"

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "codeless_confirm"}
    )
    assert result["type"] == "abort"
    await hass.async_block_till_done()

    # Read back through the provider the repaired entry loaded, so this is
    # the code a use would be checked against rather than a file nothing
    # opened.
    held = entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert isinstance(held, CodelessLock)
    assert (await held.async_get_usercodes())[1].readable_pin == "9999"
    assert hass_storage[store_key]["data"]["1"]["code"] == "9999"

    await hass.config_entries.async_unload(entry.entry_id)
