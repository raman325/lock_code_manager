"""Validate that every shipped blueprint parses against the Home Assistant schema."""

from __future__ import annotations

import pathlib

import pytest

from homeassistant.components.blueprint import BLUEPRINT_SCHEMA, models
from homeassistant.util import yaml as yaml_util

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
