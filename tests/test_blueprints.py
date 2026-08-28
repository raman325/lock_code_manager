"""Validate that every shipped blueprint parses against the Home Assistant schema."""

from __future__ import annotations

from collections.abc import Iterator
import pathlib
from typing import Any

import pytest

from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, models
from homeassistant.util import yaml as yaml_util

from custom_components.lock_code_manager.const import DOMAIN

BLUEPRINT_ROOT = pathlib.Path(__file__).resolve().parent.parent / "blueprints"


def _discover_blueprints() -> list[pytest.ParameterSet]:
    """Discover every (domain, path) blueprint pair shipped in the repo."""
    return [
        pytest.param(
            path.parent.parent.name, path, id=f"{path.parent.parent.name}/{path.name}"
        )
        for path in sorted(BLUEPRINT_ROOT.glob("*/lock_code_manager/*.yaml"))
    ]


@pytest.mark.parametrize(("domain", "blueprint_path"), _discover_blueprints())
def test_blueprint_schema(domain: str, blueprint_path: pathlib.Path) -> None:
    """Load each blueprint and assert it conforms to the blueprint schema."""
    data = yaml_util.load_yaml(blueprint_path)
    models.Blueprint(
        data,
        expected_domain=domain,
        path=str(blueprint_path),
        schema=BLUEPRINT_SCHEMA,
    )


def _iter_actions(node: Any) -> Iterator[tuple[str, dict]]:
    """Yield every (action, data) pair anywhere in a blueprint's structure."""
    if isinstance(node, dict):
        for key in ("action", "service"):
            if isinstance(name := node.get(key), str):
                yield name, node.get("data") or {}
        for value in node.values():
            yield from _iter_actions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_actions(item)


@pytest.mark.parametrize(("domain", "blueprint_path"), _discover_blueprints())
def test_blueprint_calls_only_actions_that_exist(
    domain: str, blueprint_path: pathlib.Path
) -> None:
    """
    Every Lock Code Manager action a blueprint calls is one the integration has,
    with each of that action's required fields supplied.

    A blueprint is shipped configuration that nothing else type-checks: removing
    or resharing an action leaves the YAML parsing perfectly and failing at run
    time, in somebody's imported automation. That is how
    ``condition_linker`` came to call ``set_slot_condition`` after it was
    removed, and to address a slot number after the action started taking a
    name.
    """
    services = yaml_util.load_yaml(
        pathlib.Path(__file__).resolve().parent.parent
        / "custom_components"
        / "lock_code_manager"
        / "services.yaml"
    )
    data = yaml_util.load_yaml(blueprint_path)

    for action, supplied in _iter_actions(data):
        if not action.startswith(f"{DOMAIN}."):
            continue
        name = action.removeprefix(f"{DOMAIN}.")
        assert name in services, f"{blueprint_path.name} calls unknown action {action}"
        required = {
            field
            for field, spec in (services[name].get("fields") or {}).items()
            if isinstance(spec, dict) and spec.get("required")
        }
        assert required <= set(supplied), (
            f"{blueprint_path.name} calls {action} without "
            f"{sorted(required - set(supplied))}"
        )
