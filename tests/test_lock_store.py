"""What a lock's worker remembers across a restart: the core's store."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.ha.allocation import async_read_occupancy
from custom_components.lock_code_manager.ha.lock_store import (
    STORE_KEY as SAVED_KEY,
    STORE_VERSION,
    LockStore,
)
from custom_components.lock_code_manager.ha.runtime import (
    REPORTS_KEY,
    STORE_KEY,
    async_want,
    lock_registry,
)
from custom_components.lock_code_manager.model.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.model.exceptions import LockDisconnected
from custom_components.lock_code_manager.model.locks import per_lock_issue_id
from custom_components.lock_code_manager.model.queries import subentry_id_for_slot
from custom_components.lock_code_manager.sync.slot import CLEARED, Slot, fingerprint
from custom_components.lock_code_manager.sync.worker import LockWorker

from .common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    NO_PIN_CAPABILITIES,
    TWO_SLOTS_ON_LOCK_1,
    async_advance_clock,
    async_restart_lcm,
    async_set_slot_pin,
    async_setup_lcm_entry,
    lcm_issue_ids,
    mock_lock,
    when_mock_lock_built,
)

TWO_LOCKS = {**TWO_SLOTS_ON_LOCK_1, CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}


def _masked(lock) -> None:
    """A lock that keeps slots 1 and 2's codes and never shows them."""
    lock.codes = {1: "1234", 2: "5678"}
    lock.write_only = {1, 2}


def _writes(lock) -> list:
    return lock.service_calls["set_usercode"] + lock.service_calls["noop_set_usercode"]


def _record(hass: HomeAssistant, lock: str = LOCK_1_ENTITY_ID) -> dict | None:
    """Return what the store keeps for ``lock`` now."""
    store: LockStore = hass.data[DOMAIN][STORE_KEY]
    entity = er.async_get(hass).async_get(lock)
    return None if entity is None else store._locks.get(entity.id)


async def _remove_user(hass: HomeAssistant, entry: MockConfigEntry, slot: int) -> None:
    hass.config_entries.async_remove_subentry(entry, subentry_id_for_slot(entry, slot))
    await hass.async_block_till_done()


async def _flush(hass: HomeAssistant, freezer) -> None:
    """Let the store's delayed save happen."""
    await async_advance_clock(hass, freezer, 5)


# Restarts ----------------------------------------------------------------------


async def test_a_masked_lock_is_not_written_again_after_a_restart(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """#405: what was written is remembered, so a restart rewrites nothing."""
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
        assert len(_writes(mock_lock(entry))) == 2

        await async_restart_lcm(hass, entry)

    assert _writes(mock_lock(entry)) == []


async def test_a_pin_changed_before_a_restart_is_written_once(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """What is remembered is the PIN written, not that the slot was written."""
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
        await async_set_slot_pin(hass, entry, 1, "4321")
        await hass.async_block_till_done()

        await async_restart_lcm(hass, entry)

    assert _writes(mock_lock(entry)) == []
    assert _record(hass)["slots"]["1"]["wrote"] == fingerprint(
        "4321", hass.data[DOMAIN]["instance_id"].encode()
    )


async def test_a_write_cut_off_by_a_restart_is_not_trusted_after_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A write that was out when Home Assistant stopped may have landed.

    Slot 1 held 1234, written and remembered; 4321 was being written when the
    stop cut it off, and the slot is back to 1234 by the next start. Trusting
    what was written before would leave 4321 on a lock that will not show it.
    """
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
        held = mock_lock(entry).hang("set", 1)
        await async_set_slot_pin(hass, entry, 1, "4321")
        await held.async_wait_held()
        lock_registry(hass)._grace = 0
        assert await hass.config_entries.async_unload(entry.entry_id)
        subentry = entry.subentries[subentry_id_for_slot(entry, 1)]
        hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, CONF_PIN: "1234"}
        )
        held.clear()

        await async_restart_lcm(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert [call[:2] for call in _writes(mock_lock(entry))] == [(1, "1234")]


async def test_a_lock_changed_while_stopped_is_put_back(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """What the lock says comes first: a remembered write never outvotes a read."""
    entry = await async_setup_lcm_entry(hass)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()

    with when_mock_lock_built(lambda lock: lock.codes.update({1: "9999"})):
        await async_restart_lcm(hass, entry)

    lock = mock_lock(entry)
    assert lock.service_calls["set_usercode"] == [(1, "4321", "test1")]
    assert lock.codes[1] == "4321"


async def test_a_clear_owed_at_a_restart_is_sent_after_it(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A user removed while the lock was away leaves it once it answers, restart or not."""
    entry = await async_setup_lcm_entry(hass)
    mock_lock(entry).fail(None, error=LockDisconnected)
    await _remove_user(hass, entry, 2)
    assert _record(hass)["slots"]["2"]["release"] == entry.entry_id

    await async_restart_lcm(hass, entry)

    assert 2 not in mock_lock(entry).codes
    assert _record(hass)["slots"].get("2") is None


async def test_a_clear_owed_by_a_deleted_entry_is_sent_through_another(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The entry that removed the user is gone; another that lists the lock serves."""
    with when_mock_lock_built(_masked):
        giving = await async_setup_lcm_entry(hass)
        other = await async_setup_lcm_entry(
            hass,
            {CONF_LOCKS: [LOCK_1_ENTITY_ID], CONF_SLOTS: {}},
            unique_id="other",
        )
        mock_lock(giving).fail(None, error=LockDisconnected)
        await _remove_user(hass, giving, 2)
        assert await hass.config_entries.async_remove(giving.entry_id)
        await hass.async_block_till_done()
        assert _record(hass)["slots"]["2"]["release"] == giving.entry_id
        assert "1" not in _record(hass)["slots"]

        await async_restart_lcm(hass, other)

    assert 2 not in mock_lock(other).codes


async def test_a_clear_owed_on_a_lock_no_entry_lists_is_dropped(
    hass: HomeAssistant, mock_lock_config_entry, caplog: pytest.LogCaptureFixture
) -> None:
    """With no entry to set its provider up, the clear cannot be sent: it is said so."""
    entry = await async_setup_lcm_entry(hass)
    mock_lock(entry).fail(None, error=LockDisconnected)
    await _remove_user(hass, entry, 2)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    await async_restart_lcm(hass)

    assert _record(hass) is None
    assert "Slot 2 on lock.test_1 was still being cleared" in caplog.text
    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is None


async def test_a_lock_dead_at_a_restart_raises_its_offline_repair(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """It answered before the restart, so silence after it is the lock gone."""
    entry = await async_setup_lcm_entry(hass)

    with when_mock_lock_built(lambda lock: lock.fail(None, error=LockDisconnected)):
        await async_restart_lcm(hass, entry)
        await async_advance_clock(hass, freezer, 6 * 60 * 60)

    assert lcm_issue_ids(hass, "lock_offline")


async def test_the_store_never_holds_a_pin(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage, freezer
) -> None:
    """Only fingerprints and ``cleared`` are kept of what was written."""
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
        await async_set_slot_pin(hass, entry, 1, "4321")
        await _remove_user(hass, entry, 2)
        await _flush(hass, freezer)

    def leaves(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from leaves(item)
        elif isinstance(value, list):
            for item in value:
                yield from leaves(item)
        else:
            yield value

    saved = hass_storage[SAVED_KEY]["data"]
    salt = hass.data[DOMAIN]["instance_id"].encode()
    [record] = saved["locks"].values()
    pin = fingerprint("4321", salt)
    assert record["slots"]["1"] == {"wrote": pin, "fingerprints": {"pin": pin}}
    assert record["slots"].get("2", {}).get("wrote") in (None, CLEARED)
    assert not {"1234", "4321", "5678"} & set(leaves(saved))


# Cleanup -----------------------------------------------------------------------


@pytest.mark.parametrize("how", ["reload", "disable"])
async def test_an_unload_forgets_nothing(
    hass: HomeAssistant, mock_lock_config_entry, how: str
) -> None:
    """#1243: an unload is not a lock leaving; what was written stays remembered."""
    entry = await async_setup_lcm_entry(hass)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()
    before = _record(hass)

    if how == "reload":
        assert await hass.config_entries.async_reload(entry.entry_id)
    else:
        await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        await hass.async_block_till_done()
        await hass.config_entries.async_set_disabled_by(entry.entry_id, None)
    await hass.async_block_till_done()

    assert _record(hass) == before


async def _offline_lock_2(hass: HomeAssistant, freezer) -> MockConfigEntry:
    """Set up an entry on both locks, and let lock 2 go dead and be reported so."""
    entry = await async_setup_lcm_entry(hass, TWO_LOCKS)
    mock_lock(entry, LOCK_2_ENTITY_ID).fail(None, error=LockDisconnected)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, 6 * 60 * 60)
    assert _OFFLINE_2 in lcm_issue_ids(hass, "lock_offline")
    assert _record(hass, LOCK_2_ENTITY_ID) is not None
    return entry


_OFFLINE_2 = per_lock_issue_id("lock_offline", LOCK_2_ENTITY_ID)


async def test_a_dead_lock_handed_off_is_forgotten_with_its_offline_repair(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """
    A dead lock taken out of its only entry never answers to clear its repair.

    Its users' codes are handed off, left on the lock: nothing is owed, so
    its worker stops, and with it anything that could have cleared it.
    """
    entry = await _offline_lock_2(hass, freezer)
    assert _record(hass, LOCK_2_ENTITY_ID)["reports"] is True
    lock_2 = er.async_get(hass).async_get(LOCK_2_ENTITY_ID)
    assert hass.data[DOMAIN][REPORTS_KEY][lock_2.id] is True
    entry.runtime_data.retained_pairs = {(LOCK_2_ENTITY_ID, 1), (LOCK_2_ENTITY_ID, 2)}

    hass.config_entries.async_update_entry(
        entry, options={**TWO_LOCKS, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    # Its worker stops once the job it is in has waited out its gap.
    await async_advance_clock(hass, freezer, 60)

    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None
    assert _OFFLINE_2 not in lcm_issue_ids(hass, "lock_offline")
    assert _record(hass, LOCK_2_ENTITY_ID) is None
    # Nor is it held in memory: back in an entry, its next read that
    # describes a slot is remembered again.
    assert lock_2.id not in hass.data[DOMAIN][REPORTS_KEY]
    assert _record(hass, LOCK_1_ENTITY_ID) is not None


async def test_a_dead_lock_taken_out_is_forgotten_at_the_next_start(
    hass: HomeAssistant,
    mock_lock_config_entry,
    freezer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Its clears are owed while Home Assistant runs, and the repair rightly stays.

    At the next start no entry lists the lock: the clears cannot be sent, and
    the lock, its repair and what was kept of it go.
    """
    entry = await _offline_lock_2(hass, freezer)
    hass.config_entries.async_update_entry(
        entry, options={**TWO_LOCKS, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await async_advance_clock(hass, freezer, 60)
    assert _OFFLINE_2 in lcm_issue_ids(hass, "lock_offline")
    assert "release" in _record(hass, LOCK_2_ENTITY_ID)["slots"]["1"]

    await async_restart_lcm(hass, entry)

    assert _OFFLINE_2 not in lcm_issue_ids(hass, "lock_offline")
    assert _record(hass, LOCK_2_ENTITY_ID) is None
    assert "Slot 1 on lock.test_2 was still being cleared" in caplog.text


async def test_a_lock_a_disabled_entry_lists_has_not_left(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Dropped by one entry, still listed by a disabled one: what was written stays."""
    entry = await async_setup_lcm_entry(hass, TWO_LOCKS)
    disabled = await async_setup_lcm_entry(
        hass,
        {CONF_LOCKS: [LOCK_2_ENTITY_ID], CONF_SLOTS: {}},
        unique_id="disabled",
    )
    await hass.config_entries.async_set_disabled_by(
        disabled.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    before = _record(hass, LOCK_2_ENTITY_ID)
    assert before is not None

    hass.config_entries.async_update_entry(
        entry, options={**TWO_LOCKS, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()

    assert _record(hass, LOCK_2_ENTITY_ID) == before


async def test_a_deleted_entry_s_slots_are_forgotten(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Deleting an entry forgets what was written to its slots, and only those."""
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
    await async_setup_lcm_entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {3: {CONF_NAME: "c", CONF_PIN: "3333", CONF_ENABLED: True}},
        },
        unique_id="other",
    )
    assert set(_record(hass)["slots"]) == {"1", "2", "3"}

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert set(_record(hass)["slots"]) == {"3"}


async def test_a_lock_no_entry_lists_is_forgotten_at_the_next_start(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage, freezer
) -> None:
    """What the store kept for a lock that is no longer configured goes at load."""
    entry = await async_setup_lcm_entry(hass)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await _flush(hass, freezer)
    hass_storage[SAVED_KEY]["data"]["locks"]["gone"] = {
        "entity_id": "lock.gone",
        "answered": True,
        "slots": {"4": {"wrote": CLEARED}},
    }
    offline = per_lock_issue_id("lock_offline", "lock.gone")
    ir.async_create_issue(
        hass,
        DOMAIN,
        offline,
        is_fixable=False,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="lock_offline",
    )

    await async_restart_lcm(hass, entry)

    assert _record(hass, "lock.gone") is None
    assert offline not in lcm_issue_ids(hass, "lock_offline")
    assert _record(hass) is not None


# The store's own ------------------------------------------------------------------


async def test_a_renamed_lock_keeps_its_record(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage, freezer
) -> None:
    """Kept by registry id: renamed while stopped, the lock still finds its record."""
    entry = await async_setup_lcm_entry(hass)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await _flush(hass, freezer)
    registry = er.async_get(hass)
    entity = registry.async_get(LOCK_1_ENTITY_ID)
    assert entity.id in hass_storage[SAVED_KEY]["data"]["locks"]
    registry.async_update_entity(LOCK_1_ENTITY_ID, new_entity_id="lock.renamed")

    store = LockStore(hass)
    await store.async_load()

    salt = hass.data[DOMAIN]["instance_id"].encode()
    assert store.wrote(entity.id, pin_address(1)) == fingerprint("4321", salt)
    assert store.prune({"lock.renamed"}) == []


async def test_a_slot_handed_off_is_forgotten(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A user removed with their code left on the lock is no longer this slot's."""
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
    entry.runtime_data.retained_pairs = {(LOCK_1_ENTITY_ID, 2)}

    await _remove_user(hass, entry, 2)

    assert mock_lock(entry).codes[2] == "5678"
    assert set(_record(hass)["slots"]) == {"1"}


async def test_a_held_clear_is_not_tried_again_by_a_restart(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Its repair is what tries it again, once acknowledged: a restart does not."""
    entry = await async_setup_lcm_entry(hass)
    mock_lock(entry).refuse("clear", 2)
    await _remove_user(hass, entry, 2)
    assert lcm_issue_ids(hass, "slot_suspended")
    assert "release" not in _record(hass)["slots"].get("2", {})

    await async_restart_lcm(hass, entry)

    assert mock_lock(entry).service_calls["clear_usercode"] == []


async def test_a_lock_that_answered_before_still_owes_a_clear_without_pins(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Reporting no room for PINs after a restart does not wipe what it held.

    It answered before, with codes written: a re-interview that reads no
    slots does not take them off, and the clear waits for it to recover.
    """
    entry = await async_setup_lcm_entry(hass)

    with when_mock_lock_built(
        lambda lock: setattr(lock, "capabilities", NO_PIN_CAPABILITIES)
    ):
        await async_restart_lcm(hass, entry)
        await _remove_user(hass, entry, 2)

    assert _record(hass)["slots"]["2"]["release"] == entry.entry_id


async def test_polls_that_change_nothing_save_nothing(
    hass: HomeAssistant, mock_lock_config_entry, freezer
) -> None:
    """Every job reports in, but only a change is written: a polled lock spares the disk."""
    entry = await async_setup_lcm_entry(hass)
    await _flush(hass, freezer)
    store: LockStore = hass.data[DOMAIN][STORE_KEY]
    saves = []
    store._save = lambda: saves.append(None)

    await async_advance_clock(hass, freezer, 60 * 60)

    assert mock_lock(entry).service_calls["get_usercodes"]
    assert saves == []


async def test_a_lock_replaced_under_the_same_entity_id_starts_over(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage
) -> None:
    """
    A record whose registry id is gone was for a lock that is gone.

    A new lock given its entity id holds none of what was written to the
    old one: trusting that would leave a lock that will not show its codes
    without them.
    """
    hass_storage[SAVED_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": SAVED_KEY,
        "data": {
            "locks": {
                "gone": {
                    "entity_id": LOCK_1_ENTITY_ID,
                    "answered": True,
                    "reports": True,
                    "slots": {"1": {"wrote": CLEARED}},
                }
            }
        },
    }
    store = LockStore(hass)
    await store.async_load()

    key = er.async_get(hass).async_get(LOCK_1_ENTITY_ID).id
    assert store.wrote(key, pin_address(1)) is None
    assert not store.answered(key)
    assert not store.reports(key)
    assert [r["entity_id"] for r in store.prune({LOCK_1_ENTITY_ID})] == [
        LOCK_1_ENTITY_ID
    ]


async def test_a_lock_registry_started_before_set_up_is_kept(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A config flow's read starts the lock registry before Lock Code Manager sets up.

    Setting up keeps it, rather than leaving its workers running beside a
    second registry's, and what it writes from then on is remembered.
    """
    registry = lock_registry(hass)

    await async_setup_lcm_entry(hass)

    assert lock_registry(hass) is registry
    assert _record(hass) is not None


async def test_a_lock_gone_from_the_entity_registry_is_not_kept_twice(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Every lock Lock Code Manager drives has a registry entry: it is kept by that id.

    One whose entry goes while its worker runs is not kept again by entity
    id beside it: that record would outlive the next start's prune, which
    drops the one kept by the registry id that is gone.
    """
    entry = await async_setup_lcm_entry(hass, TWO_LOCKS)
    er.async_get(hass).async_remove(LOCK_2_ENTITY_ID)

    await async_set_slot_pin(hass, entry, 1, "4321")
    await hass.async_block_till_done()

    store: LockStore = hass.data[DOMAIN][STORE_KEY]
    assert [r["entity_id"] for r in store._locks.values()].count(LOCK_2_ENTITY_ID) == 1


async def test_a_read_before_set_up_that_described_a_slot_is_remembered(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The first entry's config flow reads its lock before there is a store (#1581).

    That the lock described a slot then is kept once there is one: the
    entry's own reads, of slots it wrote, may never show it again.
    """
    await async_read_occupancy(hass, None, [LOCK_1_ENTITY_ID], [1])

    await async_setup_lcm_entry(hass)

    assert _record(hass)["reports"] is True


async def test_a_clear_owed_on_a_lock_only_a_disabled_entry_lists_is_sent(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Decision F: the disabled entry still lists the lock, and sets its provider up."""
    entry = await async_setup_lcm_entry(hass, TWO_LOCKS)
    disabled = await async_setup_lcm_entry(
        hass, {CONF_LOCKS: [LOCK_2_ENTITY_ID], CONF_SLOTS: {}}, unique_id="disabled"
    )
    await hass.config_entries.async_set_disabled_by(
        disabled.entry_id, ConfigEntryDisabler.USER
    )
    await hass.async_block_till_done()
    mock_lock(entry, LOCK_2_ENTITY_ID).fail(None, error=LockDisconnected)
    await _remove_user(hass, entry, 2)
    hass.config_entries.async_update_entry(
        entry, options={**TWO_LOCKS, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    await hass.async_block_till_done()
    assert _record(hass, LOCK_2_ENTITY_ID)["slots"]["2"]["release"] == entry.entry_id

    built: list = []
    with when_mock_lock_built(built.append):
        await async_restart_lcm(hass, entry)

    [lock_2] = [lock for lock in built if lock.lock.entity_id == LOCK_2_ENTITY_ID]
    assert 2 not in lock_2.codes
    assert _record(hass, LOCK_2_ENTITY_ID)["slots"].get("2") is None


# Kept by the registry id the worker is bound to ----------------------------------


async def test_a_lock_renamed_while_it_runs_is_still_remembered(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Its worker, still under the old entity id, writes under the same record.

    Looked up by the entity id at each write, the record of a lock renamed
    mid-run was missed: nothing it wrote until a restart was remembered.
    """
    entry = await async_setup_lcm_entry(hass)
    entities = er.async_get(hass)
    key = entities.async_get(LOCK_1_ENTITY_ID).id
    entities.async_update_entity(LOCK_1_ENTITY_ID, new_entity_id="lock.renamed")
    await hass.async_block_till_done()
    assert lock_registry(hass).worker(LOCK_1_ENTITY_ID) is not None

    async_want(hass, entry, LOCK_1_ENTITY_ID, 1, "4321")
    await hass.async_block_till_done()

    assert mock_lock(entry).codes[1] == "4321"
    store: LockStore = hass.data[DOMAIN][STORE_KEY]
    salt = hass.data[DOMAIN]["instance_id"].encode()
    assert list(store._locks) == [key]
    assert store.wrote(key, pin_address(1)) == fingerprint("4321", salt)


async def test_a_device_re_paired_back_starts_over(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A, then B, then A again under one entity id: A's first worker's writes are moot.

    The lock is re-paired: its codes may be gone. Trusting what the first
    worker wrote, A's new worker would write nothing to a lock that will
    not show its codes.
    """
    entities = er.async_get(hass)
    with when_mock_lock_built(_masked):
        entry = await async_setup_lcm_entry(hass)
        first = entities.async_get(LOCK_1_ENTITY_ID)
        # B under the entity id, A moved aside.
        entities.async_update_entity(LOCK_1_ENTITY_ID, new_entity_id="lock.first")
        other = entities.async_get_or_create(
            "lock",
            first.platform,
            f"{first.unique_id}-re-paired",
            suggested_object_id="test_1",
            config_entry=hass.config_entries.async_get_entry(first.config_entry_id),
        )
        await hass.async_block_till_done()
        assert lock_registry(hass).worker(LOCK_1_ENTITY_ID).port.provider.lock.id == (
            other.id
        )
        # And A back.
        entities.async_update_entity(LOCK_1_ENTITY_ID, new_entity_id="lock.other")
        entities.async_update_entity("lock.first", new_entity_id=LOCK_1_ENTITY_ID)
        await hass.async_block_till_done()

    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    assert worker.port.provider.lock.id == first.id
    assert len(_writes(mock_lock(entry))) == 2


# What a release is told ---------------------------------------------------------


@pytest.mark.parametrize(
    ("enabled", "written"),
    [(True, True), (False, False)],
    ids=["a_pin_written", "only_ever_cleared"],
)
async def test_a_release_is_told_whether_lcm_wrote_a_pin_there(
    hass: HomeAssistant, mock_lock_config_entry, enabled: bool, written: bool
) -> None:
    """
    Taken as the release begins: its own send forgets what the slot last wrote.

    A slot only ever cleared never held Lock Code Manager's code, so a
    provider finding nothing of its own there has nothing to warn about.
    """
    entry = await async_setup_lcm_entry(
        hass,
        {
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            # Not the code the lock starts with: a slot already in sync is never
            # written.
            CONF_SLOTS: {1: {CONF_NAME: "a", CONF_PIN: "4321", CONF_ENABLED: enabled}},
        },
    )
    lock = mock_lock(entry)
    released: list[tuple[int, bool]] = []

    async def _release(self, slot: int, *, written: bool = False) -> None:
        released.append((slot, written))
        await self.async_clear_usercode(slot)

    with patch.object(type(lock), "async_release_managed_slot", _release):
        await _remove_user(hass, entry, 1)

    assert released == [(1, written)]


async def test_a_slot_never_written_holds_nothing_lcm_wrote(
    hass: HomeAssistant,
) -> None:
    """Nothing kept for the lock or the slot, or only clears: nothing of Lock Code Manager's."""
    store = LockStore(hass)
    store._locks = {
        "key": {
            "answered": True,
            "slots": {
                "2": {"wrote": CLEARED},
                "3": {"fingerprints": {"pin": CLEARED, "password": CLEARED}},
            },
        }
    }

    assert not store.wrote_any("other", 1)
    assert not store.wrote_any("key", 1)
    assert not store.wrote_any("key", 2)
    assert not store.wrote_any("key", 3)


async def test_a_password_alone_is_something_lcm_wrote(hass: HomeAssistant) -> None:
    """A release of a slot holding only a password has Lock Code Manager's to remove."""
    store = LockStore(hass)
    store._locks = {
        "key": {
            "answered": True,
            "slots": {"4": {"fingerprints": {"pin": CLEARED, "password": "fp"}}},
        }
    }

    assert store.wrote_any("key", 4)


async def test_a_record_without_types_is_read_as_the_pins(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage
) -> None:
    """A record saved before credential types, or by a release from before them."""
    key = er.async_get(hass).async_get(LOCK_1_ENTITY_ID).id
    data = {
        "locks": {
            key: {
                "entity_id": LOCK_1_ENTITY_ID,
                "answered": True,
                "reports": True,
                "slots": {
                    "1": {"wrote": "abc"},
                    "2": {"wrote": CLEARED, "release": "entry"},
                    "3": {"release": "entry"},
                },
            }
        }
    }
    hass_storage[SAVED_KEY] = {
        "version": STORE_VERSION,
        "minor_version": 1,
        "key": SAVED_KEY,
        "data": deepcopy(data),
    }
    store = LockStore(hass)
    await store.async_load()

    assert store.wrote(key, pin_address(1)) == "abc"
    assert store.wrote(key, pin_address(2)) == CLEARED
    assert store.wrote(key, pin_address(3)) is None
    assert store.wrote(key, CredentialAddress(1, CredentialType.PASSWORD)) is None
    assert store.releases() == [
        (LOCK_1_ENTITY_ID, 2, "entry"),
        (LOCK_1_ENTITY_ID, 3, "entry"),
    ]
    assert hass_storage[SAVED_KEY]["data"] == data


async def test_remembering_one_type_keeps_the_others_in_the_slot(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage
) -> None:
    """A type the worker does not track keeps what was last written for it."""
    key = er.async_get(hass).async_get(LOCK_1_ENTITY_ID).id
    hass_storage[SAVED_KEY] = {
        "version": STORE_VERSION,
        "minor_version": 1,
        "key": SAVED_KEY,
        "data": {
            "locks": {
                key: {
                    "entity_id": LOCK_1_ENTITY_ID,
                    "answered": True,
                    "slots": {
                        "1": {
                            "wrote": "old",
                            "fingerprints": {"pin": "old", "password": "pw"},
                        }
                    },
                }
            }
        },
    }
    store = LockStore(hass)
    await store.async_load()
    # Never started, so the port is never asked anything.
    worker = LockWorker(object(), b"salt")
    worker.slots[pin_address(1)] = Slot("1234", wrote="new")

    store.remember(key, LOCK_1_ENTITY_ID, worker, lambda slot: None)

    assert store.wrote(key, pin_address(1)) == "new"
    assert store.wrote(key, CredentialAddress(1, CredentialType.PASSWORD)) == "pw"
    # A release from before credential types reads the PIN's from ``wrote``.
    assert store._locks[key]["slots"]["1"]["wrote"] == "new"


async def test_a_slot_with_no_pin_written_keeps_no_wrote(
    hass: HomeAssistant, mock_lock_config_entry, hass_storage
) -> None:
    """``wrote`` is only ever the PIN's: another type is never read as one."""
    key = er.async_get(hass).async_get(LOCK_1_ENTITY_ID).id
    store = LockStore(hass)
    await store.async_load()
    password = CredentialAddress(2, CredentialType.PASSWORD)
    # Never started, so the port is never asked anything.
    worker = LockWorker(object(), b"salt")
    worker.slots[password] = Slot("secret", wrote="pw")

    store.remember(key, LOCK_1_ENTITY_ID, worker, lambda slot: None)

    assert store._locks[key]["slots"]["2"] == {"fingerprints": {"password": "pw"}}
    assert store.wrote_any(key, 2)
