"""
Upgrading must not cost a user their history.

The migration re-slugs every entity id onto its user's name. Home
Assistant's recorder repoints history when an entity is renamed -- but
only when the rename goes through the entity registry, and only when the
destination id is not already taken. Both conditions are ours to get
right, and neither fails loudly: the recorder logs a warning and the
history is simply not there any more.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder.db_schema import StatesMeta
from homeassistant.components.recorder.util import session_scope
from homeassistant.const import CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN

from .common import LOCK_1_ENTITY_ID


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """
    Claim the recorder database before Home Assistant starts.

    Overrides the project-wide autouse fixture of the same name. That one
    pulls in `hass` before any test's own fixtures, and `recorder_db_url`
    refuses to run once it has -- so the recorder cannot be exercised
    without asking for the URL first.
    """
    yield


OLD_PIN_ENTITY = "text.all_locks_code_slot_1_pin"
NEW_PIN_ENTITY = "text.all_locks_raman_pin"


def _recorded_entity_ids(hass: HomeAssistant) -> set[str]:
    """Every entity id the recorder holds states under."""
    with session_scope(hass=hass, read_only=True) as session:
        return {row[0] for row in session.query(StatesMeta.entity_id).all()}


async def _v3_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry as the released version leaves it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="All Locks",
        data={
            "locks": [LOCK_1_ENTITY_ID],
            "slots": {1: {"enabled": True, "name": "Raman", "pin": "1111"}},
        },
        version=3,
        minor_version=1,
    )
    entry.add_to_hass(hass)
    return entry


async def test_upgrading_keeps_a_users_history(
    recorder_mock,
    hass: HomeAssistant,
    mock_lock_config_entry,
    entity_registry: er.EntityRegistry,
) -> None:
    """
    History recorded before the upgrade is readable after it.

    This is the guarantee that matters to somebody upgrading: their
    entity ids change, and the four months of state behind them has to
    come along. It does, because the migration renames the registry entry
    in place rather than removing and recreating it -- the recorder
    listens for that rename and repoints `states_meta`.
    """
    entry = await _v3_entry(hass)
    entity_registry.async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|{CONF_PIN}",
        config_entry=entry,
        suggested_object_id="all_locks_code_slot_1_pin",
    )

    # History under the old id, as a real install would have.
    hass.states.async_set(OLD_PIN_ENTITY, "1111")
    await async_wait_recording_done(hass)
    assert OLD_PIN_ENTITY in _recorded_entity_ids(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    recorded = _recorded_entity_ids(hass)
    # The row moved rather than being duplicated: the same history, under
    # the name the entity answers to now.
    assert NEW_PIN_ENTITY in recorded
    assert OLD_PIN_ENTITY not in recorded

    await hass.config_entries.async_unload(entry.entry_id)


async def test_migrating_twice_over_one_recorder_strands_the_history(
    recorder_mock,
    hass: HomeAssistant,
    mock_lock_config_entry,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Rewinding the configuration without rewinding the recorder loses it.

    Home Assistant declines to repoint history onto an id that already
    exists -- it warns and carries on, and the states stay under the name
    nothing points at any more. Reached by restoring a pre-upgrade backup
    of the configuration while the recorder database moves forward, which
    is what a "try it and roll back" does.

    Not a defect to fix here so much as one to know about: the integration
    cannot merge the two, and a normal one-way upgrade never gets here.
    """
    entry = await _v3_entry(hass)
    entity_registry.async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|{CONF_PIN}",
        config_entry=entry,
        suggested_object_id="all_locks_code_slot_1_pin",
    )

    # The recorder has already seen the post-upgrade name, from a run the
    # configuration has since been rewound past. The state is then removed:
    # what makes this case bite is that the name survives ONLY in the
    # recorder, where the entity id generator cannot see it to avoid it.
    hass.states.async_set(NEW_PIN_ENTITY, "1111")
    hass.states.async_set(OLD_PIN_ENTITY, "1111")
    await async_wait_recording_done(hass)
    hass.states.async_remove(NEW_PIN_ENTITY)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)

    assert "Cannot migrate history" in caplog.text
    # Both rows survive; the old one is now unreachable by any entity.
    recorded = _recorded_entity_ids(hass)
    assert OLD_PIN_ENTITY in recorded
    assert NEW_PIN_ENTITY in recorded

    await hass.config_entries.async_unload(entry.entry_id)
