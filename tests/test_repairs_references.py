"""Reporting what still points at an entity ID the migration moved."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components import automation
from homeassistant.config import AUTOMATION_CONFIG_PATH, SCRIPT_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import save_yaml

from custom_components.lock_code_manager.const import (
    DOMAIN,
    ENTITY_IDS_RENAMED_ISSUE,
)
from custom_components.lock_code_manager.domain import references
from custom_components.lock_code_manager.repairs import async_create_fix_flow

from .common import LOCK_1_ENTITY_ID

OLD_PIN_ENTITY = "text.all_locks_code_slot_1_pin"
NEW_PIN_ENTITY = "text.raman_pin"


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
        original_name="PIN",
        suggested_object_id="all_locks_code_slot_1_pin",
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (
        er.async_get(hass).async_get_entity_id(
            "text", DOMAIN, f"{entry.entry_id}|1|pin"
        )
        == NEW_PIN_ENTITY
    )
    return entry


async def _open_repair(hass: HomeAssistant, entry: MockConfigEntry):
    """Open the repair the migration raised, and hand back its flow."""
    issue_id = ENTITY_IDS_RENAMED_ISSUE
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    return await flow.async_step_init(), flow


@pytest.fixture(name="config_dir")
def config_dir_fixture(hass: HomeAssistant, tmp_path):
    """Point Home Assistant's config dir at a temp dir."""
    hass.config.config_dir = str(tmp_path)
    (tmp_path / "configuration.yaml").write_text("", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("shape", "alias"),
    [
        # A bare value, as an entity selector stores it.
        ({"triggers": [{"trigger": "state", "entity_id": OLD_PIN_ENTITY}]}, "Plain"),
        # Inside a template, which Home Assistant does not report at all.
        (
            {
                "conditions": [
                    {
                        "condition": "template",
                        "value_template": f"{{{{ states('{OLD_PIN_ENTITY}') }}}}",
                    }
                ]
            },
            "Templated",
        ),
        # As a mapping key.
        (
            {
                "actions": [
                    {"action": "scene.apply", "data": {"entities": {OLD_PIN_ENTITY: 1}}}
                ]
            },
            "Keyed",
        ),
        # Inside a list, which is as common as the string form.
        (
            {
                "triggers": [
                    {"trigger": "state", "entity_id": [OLD_PIN_ENTITY, "light.k"]}
                ]
            },
            "Listed",
        ),
        # Under use_blueprint.input, which is where a blueprint automation
        # keeps the entities the user picked.
        (
            {
                "use_blueprint": {
                    "path": "lock_code_manager/slot_usage_limiter.yaml",
                    "input": {"enabled_switch": OLD_PIN_ENTITY},
                }
            },
            "From a blueprint",
        ),
    ],
)
async def test_every_shape_a_reference_takes_is_reported(
    hass: HomeAssistant, mock_lock_config_entry, config_dir, shape: dict, alias: str
) -> None:
    """
    The user is told what to go and fix, by name.

    A blueprint automation keeps its entity IDs several levels down under
    ``use_blueprint.input``, and an ID used only inside a template never
    appears in what Home Assistant reports as referenced.
    """
    save_yaml(
        str(config_dir / AUTOMATION_CONFIG_PATH),
        [{"id": "referring", "alias": alias, **shape}],
    )

    entry = await _migrated_entry(hass)
    result, flow = await _open_repair(hass, entry)

    assert result["type"] == "form"
    assert alias in result["description_placeholders"]["referrers"]
    assert OLD_PIN_ENTITY in result["description_placeholders"]["renames"]

    # Reading it is the whole job, so dismissing is all there is to do.
    assert (await flow.async_step_init({}))["type"] == "create_entry"

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_lookalike_id_is_not_reported(
    hass: HomeAssistant, mock_lock_config_entry, config_dir
) -> None:
    """A longer ID that merely begins with a moved one is a different entity."""
    save_yaml(
        str(config_dir / AUTOMATION_CONFIG_PATH),
        [
            {
                "id": "lookalike",
                "alias": "A different entity",
                "triggers": [
                    {"trigger": "state", "entity_id": f"{OLD_PIN_ENTITY}_backup"}
                ],
            }
        ],
    )

    entry = await _migrated_entry(hass)
    issue_id = ENTITY_IDS_RENAMED_ISSUE
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    result = await flow.async_step_init()

    # Nothing refers to the old ID, so the repair closes itself.
    assert result["type"] == "create_entry"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None

    await hass.config_entries.async_unload(entry.entry_id)


async def test_a_script_is_reported_too(
    hass: HomeAssistant, mock_lock_config_entry, config_dir
) -> None:
    """scripts.yaml is a mapping rather than a list."""
    save_yaml(
        str(config_dir / SCRIPT_CONFIG_PATH),
        {
            "my_script": {
                "alias": "My script",
                "sequence": [
                    {
                        "action": "text.set_value",
                        "target": {"entity_id": OLD_PIN_ENTITY},
                    }
                ],
            }
        },
    )

    entry = await _migrated_entry(hass)
    result, flow = await _open_repair(hass, entry)

    assert "My script" in result["description_placeholders"]["referrers"]

    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_automation_defined_outside_the_managed_files_is_reported(
    hass: HomeAssistant, mock_lock_config_entry, config_dir
) -> None:
    """
    Home Assistant is the second source, and it sees what the files cannot.

    An automation configured in YAML is nowhere in automations.yaml, so only
    the loaded-entity lookup finds it.
    """
    save_yaml(str(config_dir / AUTOMATION_CONFIG_PATH), [])
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
    result, flow = await _open_repair(hass, entry)

    assert "Configured in YAML" in result["description_placeholders"]["referrers"]

    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    "contents",
    [
        # Will not parse.
        "this: [is: not: valid: yaml\n  - ???\n",
        # Parses, but into a shape this does not expect.
        "just a string\n",
    ],
)
async def test_an_unreadable_file_does_not_break_the_repair(
    hass: HomeAssistant, mock_lock_config_entry, config_dir, contents: str
) -> None:
    """An install part-way through an upgrade is where a broken file turns up."""
    (config_dir / AUTOMATION_CONFIG_PATH).write_text(contents, encoding="utf-8")

    entry = await _migrated_entry(hass)
    result, flow = await _open_repair(hass, entry)

    assert result["type"] in ("form", "create_entry")

    await hass.config_entries.async_unload(entry.entry_id)


async def test_nothing_is_written_to_the_config_files(
    hass: HomeAssistant, mock_lock_config_entry, config_dir
) -> None:
    """
    This reports; it does not repair.

    Saving a config file back resolves away whatever the loader resolved on
    the way in, so an ``!include`` would return inlined and a ``!secret`` as
    the secret itself. The file must come out byte for byte as it went in.
    """
    automations = config_dir / AUTOMATION_CONFIG_PATH
    automations.write_text(
        "# A comment the user wrote\n"
        "- id: tagged\n"
        "  alias: Tagged\n"
        "  triggers:\n"
        f"    - trigger: state\n      entity_id: {OLD_PIN_ENTITY}\n"
        "  variables:\n"
        "    token: !secret my_token\n",
        encoding="utf-8",
    )
    (config_dir / "secrets.yaml").write_text("my_token: shhh\n", encoding="utf-8")
    before = automations.read_text(encoding="utf-8")

    await _migrated_entry(hass)
    issue_id = ENTITY_IDS_RENAMED_ISSUE
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    flow = await async_create_fix_flow(hass, issue_id, issue.data)
    flow.hass = hass
    result = await flow.async_step_init()
    if result["type"] == "form":
        await flow.async_step_init({})
        await hass.async_block_till_done()

    assert automations.read_text(encoding="utf-8") == before


def test_helpers_ignore_what_cannot_hold_an_entity_id() -> None:
    """Guards for shapes that carry no entity ID at all."""
    assert not references._mentions(42, {"a": "b"})
    assert references.format_labels([]) == "- (none found)"
