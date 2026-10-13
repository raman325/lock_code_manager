"""
What ships with the integration: its manifest, HACS floor, strings and release.

Each check stands for a release that went out wrong once; the regression
each guards is named in its docstring.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

from awesomeversion import AwesomeVersion
import pytest
import yaml

import homeassistant.components

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
    the integration imports. It must be a stable version, in the same
    release line as the Home Assistant the suite is tested against or later
    (the tested floor's patch level is a test-environment choice).
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
    assert (floor.major, floor.minor) >= (tested.major, tested.minor)


def test_no_version_numbers_in_user_facing_strings() -> None:
    """
    Regression for PR #1459: no text names a release by number.

    A repair told users to upgrade to a version that never shipped. Text
    that names versions goes stale or wrong; the release notes carry them.
    """
    version = re.compile(r"\b(?:v?\d+\.\d+\.\d+|20\d{2}\.\d{1,2})\b")
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

    assert json.loads((target / "manifest.json").read_text())["version"] == "v9.8.7"
    assert 'VERSION = "v9.8.7"' in (target / "const.py").read_text()
