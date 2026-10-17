"""
Import boundaries between the integration's layers.

``sync/`` is the engine and knows nothing of Home Assistant or the rest of
the package. ``model/`` is shared by providers and the HA layer, so it never
imports either. Providers never import the HA layer. Every import counts,
including those under ``TYPE_CHECKING`` and inside functions: a layer that
names another only for typing still depends on it.
"""

import ast
from pathlib import Path

import pytest

PACKAGE = "custom_components.lock_code_manager"
PACKAGE_DIR = Path(__file__).parent.parent / "custom_components" / "lock_code_manager"


def _module_name(path: Path) -> str:
    """Return the dotted module name of a file in the package."""
    parts = path.relative_to(PACKAGE_DIR.parent.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """
    Return every module ``path`` imports, as absolute dotted names.

    ``from X import name`` yields both ``X`` and ``X.name``, because ``name``
    may be a submodule (``from ..ha import runtime``).
    """
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                source = ".".join([*base, node.module] if node.module else base)
            else:
                source = node.module or ""
            found.add(source)
            found.update(f"{source}.{alias.name}" for alias in node.names)
    return found


def _violations(layer: str, forbidden: tuple[str, ...]) -> list[str]:
    """List ``file: import`` for each forbidden import in ``layer``."""
    files = sorted((PACKAGE_DIR / layer).rglob("*.py"))
    assert files, f"no modules found in {layer}/"
    return [
        f"{path.relative_to(PACKAGE_DIR)}: {name}"
        for path in files
        for name in sorted(_imports(path))
        if any(name == f or name.startswith(f"{f}.") for f in forbidden)
    ]


@pytest.mark.parametrize(
    ("layer", "forbidden"),
    [
        (
            "sync",
            (
                "homeassistant",
                f"{PACKAGE}.model",
                f"{PACKAGE}.ha",
                f"{PACKAGE}.providers",
            ),
        ),
        ("model", (f"{PACKAGE}.ha", f"{PACKAGE}.providers")),
        ("providers", (f"{PACKAGE}.ha",)),
    ],
)
def test_layer_imports(layer: str, forbidden: tuple[str, ...]) -> None:
    """Each layer imports nothing from the layers it must not know about."""
    assert _violations(layer, forbidden) == []


def test_sync_imports_nothing_else_from_the_package() -> None:
    """The engine imports only its own modules from the package."""
    outside = [
        f"{path.relative_to(PACKAGE_DIR)}: {name}"
        for path in sorted((PACKAGE_DIR / "sync").rglob("*.py"))
        for name in sorted(_imports(path))
        if name.startswith(f"{PACKAGE}.") and not name.startswith(f"{PACKAGE}.sync")
    ]
    assert outside == []


def test_import_reader_resolves_relative_imports() -> None:
    """The reader sees a relative import as the module it names."""
    imports = _imports(PACKAGE_DIR / "ha" / "entry.py")
    assert f"{PACKAGE}.model.config" in imports
    assert f"{PACKAGE}.ha.callbacks" in imports
    assert f"{PACKAGE}.providers" in imports  # under TYPE_CHECKING
