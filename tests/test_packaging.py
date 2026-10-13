"""
What ships with the integration: its manifest, HACS floor, strings and release.

Each check stands for a release that went out wrong once; the regression
each guards is named in its docstring.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

from awesomeversion import AwesomeVersion
import pytest
import yaml

import homeassistant.components
import homeassistant.helpers.deprecation

from .conftest import DEPRECATED_USE_FORMATS

ROOT = Path(__file__).resolve().parent.parent
INTEGRATION = ROOT / "custom_components" / "lock_code_manager"
MANIFEST = INTEGRATION / "manifest.json"


def test_hard_dependencies_are_core_integrations() -> None:
    """
    Regression for PR #755: ``dependencies`` names only core integrations.

    A custom integration listed there makes Lock Code Manager fail to load
    for everyone who does not have it installed; those belong in
    ``after_dependencies``.
    """
    core = Path(homeassistant.components.__file__).parent
    dependencies = json.loads(MANIFEST.read_text())["dependencies"]

    assert [name for name in dependencies if not (core / name).is_dir()] == []


def test_the_hacs_floor_is_a_stable_release_in_the_tested_line() -> None:
    """
    Regressions for PR #599, #1312 and #1548: the HACS floor is installable.

    It named an unreleased version, then a beta, then one too old for what
    the integration imports. It must be a stable version in the release line
    the suite is tested against, and no later than the version tested: a
    later one may not exist yet, and nothing has checked it.
    """
    floor = AwesomeVersion(
        json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
    )
    tested = next(
        AwesomeVersion(line.split(">=", 1)[1].strip())
        for line in (ROOT / "requirements_dev.txt").read_text().splitlines()
        if line.startswith("homeassistant>=")
    )

    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d+", floor.string)
    assert (floor.major, floor.minor) == (tested.major, tested.minor)
    assert floor <= tested


def test_no_version_numbers_in_user_facing_strings() -> None:
    """
    Regression for PR #1459: no text names a release by number.

    A repair told users to upgrade to a version that never shipped. Text
    that names versions goes stale or wrong; the release notes carry them.
    """
    # Any dotted number: three-part releases, Home Assistant's 2026.9, and a
    # two-part "6.1" alike.
    version = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
    files = [
        INTEGRATION / "strings.json",
        *(INTEGRATION / "translations").glob("*.json"),
    ]

    assert [
        f"{path.name}: {match.group()}"
        for path in files
        for match in version.finditer(path.read_text())
    ] == []


def _gnu_sed() -> bool:
    """Return whether ``sed`` here is GNU sed, which the release runner uses."""
    sed = shutil.which("sed")
    if sed is None:
        return False
    result = subprocess.run(
        [sed, "--version"], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and "GNU" in result.stdout


@pytest.mark.skipif(not _gnu_sed(), reason="the release script is GNU sed's")
def test_the_release_stamps_the_tag_into_the_shipped_version(tmp_path: Path) -> None:
    """
    Regressions for PR #35, #763 and #1290: the released zip carries its tag.

    The asset went out unstamped, then missing. The stamping step is run
    here as the workflow runs it, on copies of the two files it edits.
    """
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yaml").read_text())
    [stamp] = [
        step
        for step in workflow["jobs"]["release_zip_file"]["steps"]
        if step.get("name") == "Update versions"
    ]
    target = tmp_path / "custom_components" / "lock_code_manager"
    target.mkdir(parents=True)
    for name in ("const.py", "manifest.json"):
        shutil.copy(INTEGRATION / name, target / name)

    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stamp["run"]],
        env={
            "VERSION": "v9.8.7",
            "WORKSPACE": str(tmp_path),
            # The sed the skip check found, ahead of the system's.
            "PATH": f"{Path(shutil.which('sed') or '/usr/bin/sed').parent}:/usr/bin:/bin",
        },
        check=True,
    )

    # Exactly the version line changes in each file: a pattern that also
    # matches another line (STORAGE_VERSION = ...) ships a broken file.
    for name, stamped in (
        ("const.py", 'VERSION = "v9.8.7"'),
        ("manifest.json", '"version": "v9.8.7"'),
    ):
        before = (INTEGRATION / name).read_text().splitlines()
        after = (target / name).read_text().splitlines()
        changed = [new for old, new in zip(before, after, strict=True) if old != new]
        assert [line.strip().rstrip(",") for line in changed] == [stamped]
    assert json.loads((target / "manifest.json").read_text())["version"] == "v9.8.7"
    compile((target / "const.py").read_text(), "const.py", "exec")


def test_home_assistant_still_reports_deprecated_use_as_the_detector_expects() -> None:
    """
    The deprecation detector (``tests/conftest.py``) matches these formats.

    Reworded upstream, the detector would match nothing and pass every test;
    this fails instead, so the formats get updated.
    """
    source = inspect.getsource(homeassistant.helpers.deprecation)

    assert [fmt for fmt in DEPRECATED_USE_FORMATS if f'"{fmt}"' not in source] == []


def test_no_module_uses_a_deprecated_name_when_imported() -> None:
    """
    Regressions for #531 and PR #592, at import: no deprecated name is used.

    A deprecated constant or alias imported at the top of a module is
    reported when the module is imported, which happens while tests are
    collected, before any test's detector listens. Every module is imported
    in a fresh interpreter here, with the reports collected.
    """
    script = f"""
import importlib, logging, pkgutil
formats = {sorted(DEPRECATED_USE_FORMATS)!r}
reports = []
class Collect(logging.Handler):
    def emit(self, record):
        if record.msg in formats and record.args[3] == "lock_code_manager":
            reports.append(record.getMessage())
# Listening before the package itself is imported: it imports modules too.
logging.getLogger("homeassistant").addHandler(Collect())
import custom_components.lock_code_manager as package
for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
    importlib.import_module(module.name)
print("\\n".join(reports))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == ""
