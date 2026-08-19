"""Repointing automations and scripts after the migration moves entity IDs."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import automation
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml, save_yaml

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.repairs import async_create_fix_flow

from .common import LOCK_1_ENTITY_ID

OLD_PIN_ENTITY = "text.all_locks_code_slot_1_pin"
NEW_PIN_ENTITY = "text.raman_pin"


@pytest.fixture(name="automations_file")
def automations_file_fixture(hass: HomeAssistant, tmp_path):
    """Point Home Assistant's config dir at a temp dir holding automations."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    save_yaml(
        str(tmp_path / AUTOMATION_CONFIG_PATH),
        [
            {
                "id": "reacts_to_the_pin",
                "alias": "Reacts to the PIN",
                "triggers": [{"trigger": "state", "entity_id": OLD_PIN_ENTITY}],
                "actions": [{"action": "homeassistant.turn_on"}],
            }
        ],
    )
    return tmp_path / AUTOMATION_CONFIG_PATH


async def _migrated_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Set up a version 3 entry so the migration renames its entity IDs."""
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
    er.async_get(hass).async_get_or_create(
        "text",
        DOMAIN,
        f"{entry.entry_id}|1|pin",
        config_entry=entry,
        suggested_object_id="all_locks_code_slot_1_pin",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_repair_repoints_an_automation_at_the_new_entity_id(
    hass: HomeAssistant, mock_lock_config_entry, automations_file
) -> None:
    """
    The whole point: an automation keeps working across the rename.

    Driven through the real repair flow and the real automations file, since
    the value of this is entirely in whether a user's automation still fires.
    """
    assert await async_setup_component(
        hass, automation.DOMAIN, {automation.DOMAIN: load_yaml(str(automations_file))}
    )
    await hass.async_block_till_done()

    entry = await _migrated_entry(hass)
    assert (
        er.async_get(hass).async_get_entity_id(
            "text", DOMAIN, f"{entry.entry_id}|1|pin"
        )
        == NEW_PIN_ENTITY
    )

    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["type"] == "form"
    # It names the automation it is about to change.
    assert "Reacts to the PIN" in form["description_placeholders"]["fixable"]

    await flow.async_step_init({})
    await hass.async_block_till_done()

    stored = load_yaml(str(automations_file))
    assert stored[0]["triggers"][0]["entity_id"] == NEW_PIN_ENTITY

    await hass.config_entries.async_unload(entry.entry_id)


async def test_the_repair_repoints_a_blueprint_automation(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """
    The case that matters most: an automation built from one of our blueprints.

    Creating one in the user interface stores the entity IDs the user picked
    under ``use_blueprint.input``, several levels down in automations.yaml.
    Nothing about that shape is special-cased, so this pins that the
    substitution reaches it.
    """
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    automations = tmp_path / AUTOMATION_CONFIG_PATH
    save_yaml(
        str(automations),
        [
            {
                "id": "built_from_a_blueprint",
                "alias": "Limit guest PIN",
                "use_blueprint": {
                    "path": "lock_code_manager/slot_usage_limiter.yaml",
                    "input": {
                        "pin_used_entity": "event.all_locks_code_slot_1",
                        "enabled_switch": OLD_PIN_ENTITY,
                        "locks": [LOCK_1_ENTITY_ID],
                    },
                },
            }
        ],
    )

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_init({})
    await hass.async_block_till_done()

    stored = load_yaml(str(automations))[0]["use_blueprint"]["input"]
    assert stored["enabled_switch"] == NEW_PIN_ENTITY
    # Everything else in the input is left exactly as the user set it.
    assert stored["pin_used_entity"] == "event.all_locks_code_slot_1"
    assert stored["locks"] == [LOCK_1_ENTITY_ID]

    await hass.config_entries.async_unload(entry.entry_id)
