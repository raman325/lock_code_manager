"""The inputs BLUEPRINTS.md documents are the ones the blueprints declare."""

from __future__ import annotations

import pathlib
import re
from typing import Any

import pytest
import yaml

_REPO = pathlib.Path(__file__).resolve().parent.parent
_DOC = _REPO / "BLUEPRINTS.md"
_BLUEPRINTS = sorted(
    (_REPO / "blueprints").glob("*/lock_code_manager/*.yaml"),
    key=lambda path: path.stem,
)

# The doc's headings are prose, so the file name cannot be derived from them.
# The import badge in each section links back to the blueprint it documents,
# which is what ties a table to its source.
_IMPORT_LINK = re.compile(r"blueprints%2F\w+%2Flock_code_manager%2F(\w+)\.yaml")


class _BlueprintLoader(yaml.SafeLoader):
    """Reads a blueprint without resolving the ``!input`` references in it.

    Subclasses ``SafeLoader``, so the added tag is the only one beyond the
    plain-data set and no Python object can be constructed from a blueprint.
    """


def _keep_input_tag(loader: _BlueprintLoader, node: yaml.Node) -> dict[str, Any]:
    assert isinstance(node, yaml.ScalarNode)
    return {"!input": loader.construct_scalar(node)}


_BlueprintLoader.add_constructor("!input", _keep_input_tag)


def _documented_inputs() -> dict[str, list[str]]:
    """Map each blueprint's file name to the input names its table lists."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in _DOC.read_text().splitlines():
        if match := _IMPORT_LINK.search(line):
            current = sections.setdefault(match[1], [])
        elif current is not None and line.startswith("|"):
            name = line.split("|")[1].strip()
            if name != "Input" and not name.startswith("--"):
                current.append(name)
    return sections


_DOCUMENTED = _documented_inputs()


@pytest.mark.parametrize("path", _BLUEPRINTS, ids=lambda path: path.stem)
def test_documented_inputs_match_the_blueprint(path: pathlib.Path) -> None:
    """Every input is documented, under the name the blueprint gives it."""
    blueprint = yaml.load(path.read_text(), _BlueprintLoader)["blueprint"]
    declared = [config.get("name", key) for key, config in blueprint["input"].items()]
    assert _DOCUMENTED[path.stem] == declared
