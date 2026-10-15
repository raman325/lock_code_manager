"""What a lock's worker remembers across a restart: the core's store."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.core.slot import CLEARED, fingerprint
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.lock_runtime import (
    STORE_KEY,
    lock_registry,
)
from custom_components.lock_code_manager.domain.lock_store import (
    STORE_KEY as SAVED_KEY,
    LockStore,
)
from custom_components.lock_code_manager.domain.queries import subentry_id_for_slot
from custom_components.lock_code_manager.domain.util import per_lock_issue_id

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
    return store._find(lock)


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
    assert record["slots"]["1"] == {"wrote": fingerprint("4321", salt)}
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
    entry.runtime_data.retained_pairs = {(LOCK_2_ENTITY_ID, 1), (LOCK_2_ENTITY_ID, 2)}

    hass.config_entries.async_update_entry(
        entry, options={**TWO_LOCKS, CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    # Its worker stops once the job it is in has waited out its gap.
    await async_advance_clock(hass, freezer, 60)

    assert lock_registry(hass).worker(LOCK_2_ENTITY_ID) is None
    assert _OFFLINE_2 not in lcm_issue_ids(hass, "lock_offline")
    assert _record(hass, LOCK_2_ENTITY_ID) is None
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
    assert store.wrote("lock.renamed", 1) == fingerprint("4321", salt)
    assert store.prune({"lock.renamed"}) == []


async def test_a_record_kept_by_entity_id_moves_to_the_registry_id(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A lock with no registry entry is kept by entity id, and moved once it has one."""
    store = LockStore(hass)
    store._locks = {
        LOCK_1_ENTITY_ID: {
            "entity_id": LOCK_1_ENTITY_ID,
            "answered": True,
            "slots": {"5": {"wrote": CLEARED}},
        }
    }
    await async_setup_lcm_entry(hass)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)

    store.remember(LOCK_1_ENTITY_ID, worker, lambda slot: None)

    entity = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    assert list(store._locks) == [entity.id]
    assert store._locks[entity.id]["slots"]["5"] == {"wrote": CLEARED}
    assert store.answered(LOCK_1_ENTITY_ID)


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
                    "slots": {"1": {"wrote": CLEARED}},
                }
            }
        },
    }
    store = LockStore(hass)
    await store.async_load()

    assert store.wrote(LOCK_1_ENTITY_ID, 1) is None
    assert not store.answered(LOCK_1_ENTITY_ID)
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
