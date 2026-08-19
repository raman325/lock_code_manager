"""Repointing automations and scripts after the migration moves entity IDs."""

from __future__ import annotations

import stat

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import automation
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml, save_yaml

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain import references
from custom_components.lock_code_manager.domain.references import _rewrite, _substitute
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


async def test_a_broken_automations_file_does_not_break_the_repair(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """
    A file that will not parse is one this cannot rewrite, nothing worse.

    An install part-way through an upgrade is exactly where a broken
    automations file turns up, and Home Assistant raises its own error type
    for a syntax error rather than the ones a file read usually produces.
    """
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    (tmp_path / AUTOMATION_CONFIG_PATH).write_text(
        "this: [is: not: valid: yaml\n  - ???\n", encoding="utf-8"
    )

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    # Reaches a conclusion rather than raising at the user.
    result = await flow.async_step_init()
    assert result["type"] in ("form", "create_entry")

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_reference_inside_a_template_is_found_and_rewritten(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """
    An entity ID is not always the whole string.

    Nothing in Home Assistant reports an ID used inside a template as a
    reference -- ``referenced_entities`` is built from the rendered config,
    not from template bodies -- so an automation like this would have been
    left broken AND left off the list of things needing attention.

    The lookalike pins the boundaries: an ID that merely begins with a moved
    one is a different entity and must not be touched.
    """
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    automations = tmp_path / AUTOMATION_CONFIG_PATH
    save_yaml(
        str(automations),
        [
            {
                "id": "templated",
                "alias": "Uses a template",
                "conditions": [
                    {
                        "condition": "template",
                        "value_template": f"{{{{ states('{OLD_PIN_ENTITY}') }}}}",
                    }
                ],
            },
            {
                "id": "keyed",
                "alias": "Keyed",
                "actions": [
                    {
                        "action": "scene.apply",
                        "data": {"entities": {OLD_PIN_ENTITY: "1234"}},
                    }
                ],
            },
            {
                "id": "lookalike",
                "alias": "A different entity",
                "triggers": [
                    {"trigger": "state", "entity_id": f"{OLD_PIN_ENTITY}_backup"}
                ],
            },
        ],
    )

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None

    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    form = await flow.async_step_init()
    listed = form["description_placeholders"]["fixable"]
    assert "Uses a template" in listed
    assert "Keyed" in listed
    assert "A different entity" not in listed

    await flow.async_step_init({})
    await hass.async_block_till_done()

    stored = load_yaml(str(automations))
    assert NEW_PIN_ENTITY in stored[0]["conditions"][0]["value_template"]
    # The ID can be the key, not only the value.
    assert NEW_PIN_ENTITY in stored[1]["actions"][0]["data"]["entities"]
    # A longer ID that starts with a moved one is somebody else.
    assert stored[2]["triggers"][0]["entity_id"] == f"{OLD_PIN_ENTITY}_backup"

    await hass.config_entries.async_unload(entry.entry_id)


def test_one_rename_is_not_applied_on_top_of_another() -> None:
    """
    Renames are one pass over the string, not one pass per rename.

    Applying them in turn lets a value an earlier rule rewrote be rewritten
    again by a later one, carrying an ``a`` all the way past ``b`` to ``c``.
    The exact-match path never had this problem; a template body did.
    """
    moved = {"text.a": "text.b", "text.b": "text.c"}
    assert _rewrite("text.a", moved) == "text.b"
    assert _rewrite("{{ states('text.a') }}", moved) == "{{ states('text.b') }}"
    assert _rewrite("{{ states('text.b') }}", moved) == "{{ states('text.c') }}"


async def test_a_file_that_cannot_be_written_is_not_reported_as_updated(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """Telling the user their automations were updated has to be true."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    automations = tmp_path / AUTOMATION_CONFIG_PATH
    save_yaml(
        str(automations),
        [
            {
                "id": "read_only",
                "alias": "Read only",
                "triggers": [{"trigger": "state", "entity_id": OLD_PIN_ENTITY}],
            }
        ],
    )

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    await flow.async_step_init()

    automations.chmod(stat.S_IRUSR)
    try:
        result = await flow.async_step_init({})
    finally:
        automations.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert result["type"] == "abort"
    assert result["reason"] == "write_failed"

    await hass.config_entries.async_unload(entry.entry_id)


def test_a_key_rewrite_never_overwrites_an_existing_key() -> None:
    """
    Both IDs can already be keys in the same mapping.

    Moving the old one on top would throw away whatever the new one holds,
    and this is the user's own configuration, not ours to lose.
    """
    config = {
        "entities": {OLD_PIN_ENTITY: "from the old one", NEW_PIN_ENTITY: "from the new"}
    }
    _substitute(config, {OLD_PIN_ENTITY: NEW_PIN_ENTITY})
    assert config["entities"][NEW_PIN_ENTITY] == "from the new"
    assert config["entities"][OLD_PIN_ENTITY] == "from the old one"


async def test_an_automation_outside_the_managed_files_is_reported_not_touched(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """
    The honest half: say what could not be fixed.

    An automation configured in YAML is loaded but is not in
    automations.yaml, so it cannot be rewritten. Leaving it off the list
    would tell the user nothing was outstanding when something was.
    """
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    save_yaml(str(tmp_path / AUTOMATION_CONFIG_PATH), [])

    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "alias": "Configured in YAML",
                    "trigger": {"platform": "state", "entity_id": OLD_PIN_ENTITY},
                    "action": {"action": "homeassistant.turn_on"},
                }
            ]
        },
    )
    await hass.async_block_till_done()

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    form = await flow.async_step_init()

    assert "Configured in YAML" in form["description_placeholders"]["unfixable"]
    assert form["description_placeholders"]["fixable"] == "- (none)"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_id_inside_a_list_is_rewritten(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """``entity_id`` is a list as often as it is a string."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    automations = tmp_path / AUTOMATION_CONFIG_PATH
    save_yaml(
        str(automations),
        [
            {
                "id": "listed",
                "alias": "Listed",
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": [OLD_PIN_ENTITY, "light.kitchen"],
                    }
                ],
            }
        ],
    )

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_init({})
    await hass.async_block_till_done()

    assert load_yaml(str(automations))[0]["triggers"][0]["entity_id"] == [
        NEW_PIN_ENTITY,
        "light.kitchen",
    ]

    await hass.config_entries.async_unload(entry.entry_id)


def test_helpers_ignore_what_they_cannot_contain() -> None:
    """Guards for shapes that carry no entity ID at all."""
    assert _rewrite("anything", {}) == "anything"
    assert not _substitute(42, {"a": "b"})
    assert not references._mentions(42, {"a": "b"})


async def test_a_file_whose_only_reference_cannot_be_changed_is_left_alone(
    hass: HomeAssistant, mock_lock_config_entry, tmp_path
) -> None:
    """
    Detected, but nothing safe to do.

    The mapping already holds both IDs, so the key rewrite is refused; there
    is then nothing to write, and the file must be left exactly as it was.
    """
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    automations = tmp_path / AUTOMATION_CONFIG_PATH
    save_yaml(
        str(automations),
        [
            {
                "id": "collides",
                "alias": "Collides",
                "actions": [
                    {
                        "action": "scene.apply",
                        "data": {
                            "entities": {
                                OLD_PIN_ENTITY: "from the old one",
                                NEW_PIN_ENTITY: "from the new",
                            }
                        },
                    }
                ],
            }
        ],
    )
    before = automations.read_text(encoding="utf-8")

    entry = await _migrated_entry(hass)
    issue_id = f"entity_ids_renamed_{entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    await flow.async_step_init()
    await flow.async_step_init({})
    await hass.async_block_till_done()

    assert automations.read_text(encoding="utf-8") == before

    await hass.config_entries.async_unload(entry.entry_id)
