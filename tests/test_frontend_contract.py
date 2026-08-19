"""The keys the dashboard strategy matches on are the ones the backend sends."""

from __future__ import annotations

import pathlib
import re

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_CODE,
    ATTR_IN_SYNC,
    EVENT_CREDENTIAL_USED,
)

_CONST_TS = pathlib.Path(__file__).resolve().parent.parent / "ts" / "const.ts"

# The strategy picks entities out of the websocket payload by comparing
# entity.key against these. A key that changes on one side and not the other
# does not fail anywhere -- the strategy just stops finding that entity and
# silently renders the dashboard wrong.
_SHARED_KEYS = {
    "CODE_SENSOR_KEY": ATTR_CODE,
    "CODE_EVENT_KEY": EVENT_CREDENTIAL_USED,
    "ACTIVE_KEY": ATTR_ACTIVE,
    "IN_SYNC_KEY": ATTR_IN_SYNC,
}


def test_the_frontend_and_backend_agree_on_entity_keys() -> None:
    """Renaming a key on one side only is invisible until a dashboard breaks."""
    source = _CONST_TS.read_text(encoding="utf-8")
    declared = dict(
        re.findall(r"^export const ([A-Z_]+) = '([^']+)';", source, re.MULTILINE)
    )

    missing = _SHARED_KEYS.keys() - declared.keys()
    assert not missing, f"ts/const.ts no longer declares {sorted(missing)}"

    assert {name: declared[name] for name in _SHARED_KEYS} == _SHARED_KEYS
