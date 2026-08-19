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
from custom_components.lock_code_manager.domain.config import build_slot_unique_id

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


def test_the_frontend_and_backend_agree_on_the_unique_id_shape() -> None:
    """
    The dashboard takes entities apart by position, and nothing checks that.

    ``createLockCodeManagerEntity`` splits a unique ID on the separator and
    reads the slot number, the key and the lock out of fixed positions. Move
    a field or change the separator on the Python side and it does not fail
    anywhere -- the slot number parses to NaN, the key matches nothing, and
    the strategy renders a dashboard that is simply wrong. The separator has
    already been changed once, to let a name contain it.
    """
    source = (_CONST_TS.parent / "generate-view.ts").read_text(encoding="utf-8")

    separator = re.search(r"unique_id\.split\('([^']+)'\)", source)
    assert separator, "the frontend no longer splits the unique ID"

    positions = {
        field: int(index)
        for field, index in re.findall(r"(\w+): (?:parseInt\()?split\[(\d+)\]", source)
    }
    assert {"slotNum", "key", "lockEntityId"} <= positions.keys(), positions

    per_lock = build_slot_unique_id("ENTRY", 7, ATTR_IN_SYNC, "lock.front")
    parts = per_lock.split(separator.group(1))

    assert parts[positions["slotNum"]] == "7"
    assert parts[positions["key"]] == ATTR_IN_SYNC
    assert parts[positions["lockEntityId"]] == "lock.front"

    # The slot-level variant has no lock, so that position must simply be
    # absent rather than holding something else.
    assert (
        len(build_slot_unique_id("ENTRY", 7, ATTR_IN_SYNC).split(separator.group(1)))
        == positions["lockEntityId"]
    )
