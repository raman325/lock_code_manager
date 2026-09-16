"""Tests for remembering whether a lock answers requests to read its codes."""

from __future__ import annotations

import copy

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.lock_code_manager.const import (
    CONF_INTERNAL,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    INTERNAL_LOCK_READS,
)
from custom_components.lock_code_manager.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.lock_code_manager.domain.config import (
    EntryConfig,
    async_write_entry_config,
)
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.domain.read_health import (
    UNANSWERED_ISSUE,
    ReadHealth,
    async_forget_read_health,
    async_record_read_health,
    read_health,
)
from custom_components.lock_code_manager.domain.util import per_lock_issue_id

from .common import BASE_CONFIG, LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID, write_entry_config


def _stored(entry: MockConfigEntry) -> dict[str, str]:
    """Return the read health an entry has stored, by registry id."""
    return dict((entry.data.get(CONF_INTERNAL) or {}).get(INTERNAL_LOCK_READS) or {})


def _registry_id(hass: HomeAssistant, entity_id: str) -> str:
    """Return a lock's entity registry id."""
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None
    return entry.id


def _issue(hass: HomeAssistant, lock_entity_id: str) -> ir.IssueEntry | None:
    """Return the unanswered-reads repair for a lock, if raised."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, per_lock_issue_id(UNANSWERED_ISSUE, lock_entity_id)
    )


async def test_a_verdict_is_stored_on_the_entry_and_raises_the_repair(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Stored where the next start can find it, and explained to the user."""
    entry = lock_code_manager_config_entry
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)

    assert _stored(entry) == {
        _registry_id(hass, LOCK_1_ENTITY_ID): ReadHealth.UNANSWERED.value
    }
    assert get_entry_config(entry).extra[CONF_INTERNAL] == entry.data[CONF_INTERNAL]
    assert _issue(hass, LOCK_1_ENTITY_ID) is not None

    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.ANSWERED)
    assert _stored(entry) == {
        _registry_id(hass, LOCK_1_ENTITY_ID): ReadHealth.ANSWERED.value
    }
    assert _issue(hass, LOCK_1_ENTITY_ID) is None


async def test_a_verdict_survives_a_restart(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Only what is stored is known after Home Assistant starts again."""
    entry = lock_code_manager_config_entry
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.data[DOMAIN].pop("lock_reads")
    ir.async_delete_issue(
        hass, DOMAIN, per_lock_issue_id(UNANSWERED_ISSUE, LOCK_1_ENTITY_ID)
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert read_health(hass, LOCK_1_ENTITY_ID) is ReadHealth.UNANSWERED
    assert _issue(hass, LOCK_1_ENTITY_ID) is not None


async def test_a_verdict_follows_the_lock_through_a_rename(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Keyed by the registry id, which a rename keeps."""
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)
    er.async_get(hass).async_update_entity(
        LOCK_1_ENTITY_ID, new_entity_id="lock.renamed"
    )
    assert read_health(hass, "lock.renamed") is ReadHealth.UNANSWERED


async def test_answered_anywhere_wins(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """One entry having seen the lock answer settles it for the others."""
    registry_id = _registry_id(hass, LOCK_1_ENTITY_ID)
    other = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_INTERNAL: {
                INTERNAL_LOCK_READS: {registry_id: ReadHealth.ANSWERED.value}
            },
        },
    )
    other.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry,
        data={
            **lock_code_manager_config_entry.data,
            CONF_INTERNAL: {
                INTERNAL_LOCK_READS: {registry_id: ReadHealth.UNANSWERED.value}
            },
        },
    )
    assert read_health(hass, LOCK_1_ENTITY_ID) is ReadHealth.ANSWERED


async def test_a_config_write_from_an_older_view_keeps_the_verdict(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A writer holding a view from before the verdict must not erase it."""
    entry = lock_code_manager_config_entry
    before = EntryConfig.from_entry(entry)
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)

    async_write_entry_config(
        hass, entry, before.with_slot_field_set(1, CONF_ENABLED, False)
    )
    await hass.async_block_till_done()

    assert get_entry_config(entry).slot(1)[CONF_ENABLED] is False
    assert _stored(entry) == {
        _registry_id(hass, LOCK_1_ENTITY_ID): ReadHealth.UNANSWERED.value
    }


async def test_the_options_form_does_not_carry_a_stale_verdict(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    A verdict reached while the form's submission is staged survives it.

    The form stages the entry's other keys alongside the locks, including the
    internal section as it was then, and staged options are otherwise read
    over the entry's data when the update pass folds them in.
    """
    entry = lock_code_manager_config_entry
    async_record_read_health(hass, LOCK_2_ENTITY_ID, ReadHealth.UNANSWERED)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    # Dropping a lock gives the update pass work to await before it folds
    # the staged options in.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_LOCKS: [LOCK_1_ENTITY_ID]}
    )
    assert entry.options
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert not entry.options
    assert _stored(entry) == {
        _registry_id(hass, LOCK_1_ENTITY_ID): ReadHealth.UNANSWERED.value
    }


async def test_removing_the_lock_forgets_it_and_clears_the_repair(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """An entry keeps no verdict about a lock it no longer manages."""
    entry = lock_code_manager_config_entry
    async_record_read_health(hass, LOCK_2_ENTITY_ID, ReadHealth.UNANSWERED)
    assert _issue(hass, LOCK_2_ENTITY_ID) is not None

    one_lock = copy.deepcopy(BASE_CONFIG)
    one_lock[CONF_LOCKS] = [LOCK_1_ENTITY_ID]
    assert write_entry_config(hass, entry, one_lock)
    await hass.async_block_till_done()

    assert _stored(entry) == {}
    assert _issue(hass, LOCK_2_ENTITY_ID) is None


async def test_diagnostics_say_whether_the_lock_answers(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """The first thing to check when a lock's codes never read back."""
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)
    result = await async_get_config_entry_diagnostics(
        hass, lock_code_manager_config_entry
    )
    assert result["locks"][LOCK_1_ENTITY_ID]["reads"] == "unanswered"
    assert result["locks"][LOCK_2_ENTITY_ID]["reads"] is None


async def test_a_lock_missing_from_the_registry_has_no_verdict(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Without a registry entry there is nothing to key a verdict by."""
    async_record_read_health(hass, "lock.gone", ReadHealth.UNANSWERED)
    async_forget_read_health(hass, lock_code_manager_config_entry, "lock.gone")

    assert read_health(hass, "lock.gone") is None
    assert _stored(lock_code_manager_config_entry) == {}


async def test_setup_drops_verdicts_for_locks_the_entry_no_longer_has(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """A lock removed while its registry entry was gone left a record nobody forgot."""
    entry = lock_code_manager_config_entry
    kept = _registry_id(hass, LOCK_1_ENTITY_ID)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_INTERNAL: {
                INTERNAL_LOCK_READS: {
                    kept: ReadHealth.ANSWERED.value,
                    "no-longer-a-registry-id": ReadHealth.UNANSWERED.value,
                }
            },
        },
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _stored(entry) == {kept: ReadHealth.ANSWERED.value}


async def test_a_verdict_does_not_hide_a_change_waiting_for_its_pass(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """
    The cached view is what the next pass diffs against.

    A verdict stored while a change waits for the lock an earlier pass holds
    must not refresh that view to include the change, or the pass that comes
    for it finds nothing to do.
    """
    entry = lock_code_manager_config_entry
    runtime_data = entry.runtime_data
    with_slot_3 = copy.deepcopy(BASE_CONFIG)
    with_slot_3[CONF_SLOTS][3] = {CONF_NAME: "test3", CONF_PIN: "4321"}

    await runtime_data.pass_lock.acquire()
    try:
        assert write_entry_config(hass, entry, with_slot_3)
        async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)
    finally:
        runtime_data.pass_lock.release()
    await hass.async_block_till_done()

    assert 3 in runtime_data.slot_coordinators


async def test_setup_after_a_flow_still_prepares_its_own_data(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """A config flow reads locks before setup runs, and creates the data first."""
    async_record_read_health(hass, LOCK_1_ENTITY_ID, ReadHealth.UNANSWERED)
    assert "resources" not in hass.data[DOMAIN]

    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert "resources" in hass.data[DOMAIN]
    assert await hass.config_entries.async_unload(entry.entry_id)
