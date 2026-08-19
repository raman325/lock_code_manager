"""Every registered service is described everywhere a service is described.

A service missing from ``strings.json`` still works, and still appears in the
action picker -- under its raw key, with no description and unlabelled fields.
Nothing fails, so nothing catches it: four services shipped that way.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest
import yaml

_COMPONENT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components"
    / "lock_code_manager"
)

_REGISTERED = sorted(
    set(
        re.findall(
            r'^SERVICE_\w+ = "(\w+)"', (_COMPONENT / "const.py").read_text(), re.M
        )
    )
)

_SERVICES_YAML = yaml.safe_load((_COMPONENT / "services.yaml").read_text())
_ICONS = json.loads((_COMPONENT / "icons.json").read_text())["services"]
_TRANSLATIONS = {
    name: json.loads((_COMPONENT / name).read_text())
    for name in ("strings.json", "translations/en.json")
}


def test_every_service_constant_is_registered() -> None:
    """The catalog below covers every service the integration defines."""
    assert _REGISTERED
    assert set(_SERVICES_YAML) == set(_REGISTERED)


@pytest.mark.parametrize("service", _REGISTERED)
@pytest.mark.parametrize("source", sorted(_TRANSLATIONS))
def test_service_is_translated(service: str, source: str) -> None:
    """A service carries a name and a description on both string files."""
    described = _TRANSLATIONS[source]["services"]
    assert service in described, f"{service} is missing from {source}"
    assert described[service].get("name")
    assert described[service].get("description")


@pytest.mark.parametrize("service", _REGISTERED)
@pytest.mark.parametrize("source", sorted(_TRANSLATIONS))
def test_service_fields_are_translated(service: str, source: str) -> None:
    """Every field the schema accepts is labelled, and no label is stale."""
    declared = set(_SERVICES_YAML[service].get("fields", {}))
    described = set(_TRANSLATIONS[source]["services"][service].get("fields", {}))
    assert described == declared, f"{service} in {source}"


@pytest.mark.parametrize("service", _REGISTERED)
def test_service_has_an_icon(service: str) -> None:
    """A service with no icon falls back to a generic one in the picker."""
    assert service in _ICONS
