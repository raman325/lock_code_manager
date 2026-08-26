"""The keys the dashboard strategy matches on are the ones the backend sends."""

from __future__ import annotations

import json
import pathlib
import re

from homeassistant.const import CONF_CONDITION, CONF_ENABLED, CONF_NAME, CONF_PIN

from custom_components.lock_code_manager.const import (
    ATTR_ACTIVE,
    ATTR_CALENDAR,
    ATTR_CODE,
    ATTR_CONDITION_ENTITY,
    ATTR_IN_SYNC,
    CONDITION_ENTITY_DOMAINS,
    EVENT_CREDENTIAL_USED,
    PER_LOCK_ENTITY_SUFFIX,
)
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.credentials import CredentialType

_CONST_TS = pathlib.Path(__file__).resolve().parent.parent / "ts" / "const.ts"

# The strategy picks entities out of the websocket payload by comparing
# entity.key against these. A key that changes on one side and not the other
# does not fail anywhere -- the strategy just stops finding that entity and
# silently renders the dashboard wrong.
# Every key the backend puts in a unique ID, so the card cannot order one
# that will never arrive.
_BACKEND_KEYS = {
    CONF_NAME,
    CONF_ENABLED,
    CONF_PIN,
    ATTR_ACTIVE,
    ATTR_CALENDAR,
    ATTR_CONDITION_ENTITY,
    ATTR_IN_SYNC,
    ATTR_CODE,
    EVENT_CREDENTIAL_USED,
}

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


def test_the_frontend_and_backend_agree_on_the_condition_domains() -> None:
    """
    The picker offers what the backend will accept, or it lies to the user.

    The list is written out on both sides. Add a domain to one and the
    frontend either hides a domain that works or offers one that does not,
    and nothing fails on either side.
    """
    source = _CONST_TS.read_text(encoding="utf-8")
    listed = re.search(r"CONDITION_ENTITY_DOMAINS = \[(.*?)\]", source, re.DOTALL)
    assert listed, "ts/const.ts no longer declares CONDITION_ENTITY_DOMAINS"

    assert re.findall(r"'([^']+)'", listed.group(1)) == list(CONDITION_ENTITY_DOMAINS)


def test_the_frontend_orders_keys_the_backend_actually_produces() -> None:
    """
    KEY_ORDER decides what the card shows and in what order.

    A key in it that the backend never emits renders nothing; a key the
    backend emits that is missing from it falls to the end of the card
    silently. Either way no test fails, which is how a renamed key shipped a
    broken dashboard earlier today.
    """
    source = _CONST_TS.read_text(encoding="utf-8")
    listed = re.search(r"KEY_ORDER = \[(.*?)\];", source, re.DOTALL)
    assert listed, "ts/const.ts no longer declares KEY_ORDER"

    declared = dict(
        re.findall(r"^export const ([A-Z_]+) = '([^']+)';", source, re.MULTILINE)
    )
    ordered = [
        declared.get(token.strip(), token.strip().strip("'"))
        for token in listed.group(1).split(",")
        if token.strip() and not token.strip().startswith("...")
    ]

    assert set(ordered) <= _BACKEND_KEYS, (
        f"the card orders keys the backend never emits: "
        f"{sorted(set(ordered) - _BACKEND_KEYS)}"
    )


def test_the_frontend_and_backend_agree_on_the_slot_payload() -> None:
    """
    The strategy reads two named fields out of each slot the backend sends.

    Renaming one on either side leaves the other reading ``undefined``: the
    section loses its heading, or every condition entity silently vanishes
    from the dashboard. Neither fails anywhere.
    """
    source = (_CONST_TS.parent / "types.ts").read_text(encoding="utf-8")
    declared = re.search(r"interface SlotInfo \{(.*?)\}", source, re.DOTALL)
    assert declared, "ts/types.ts no longer declares SlotInfo"

    assert set(re.findall(r"^\s*(\w+)\??:", declared.group(1), re.MULTILINE)) == {
        CONF_NAME,
        CONF_CONDITION,
    }


def test_per_lock_suffixes_match_the_entity_names() -> None:
    """
    The migration's per-lock suffixes are the ones the entities really use.

    The migration has to build the entity ID the running integration would
    generate, but it runs before any entity exists, so it cannot read the
    name off one. It carries its own copy of the suffix instead -- and a copy
    that drifts renames every per-lock entity onto an ID nothing else agrees
    with.
    """
    names = json.loads(
        (
            pathlib.Path(__file__).resolve().parent.parent
            / "custom_components"
            / "lock_code_manager"
            / "strings.json"
        ).read_text()
    )["entity"]

    for key, suffix in PER_LOCK_ENTITY_SUFFIX.items():
        declared = next(
            entity["name"]
            for domain in names.values()
            for entity_key, entity in domain.items()
            if entity_key == key
        )
        assert declared == f"{{lock_name}} {suffix}"


# Keys that name no entity, so no entity name is owed for them. The
# condition entity is somebody else's; the calendar key predates the
# condition rename and is carried for old configurations.
_KEYS_WITHOUT_AN_ENTITY = {ATTR_CONDITION_ENTITY, ATTR_CALENDAR}


def test_every_entity_key_has_a_name() -> None:
    """
    An entity whose translation key has no name is named after its device.

    Home Assistant does not complain: ``has_entity_name`` with nothing to
    append leaves the friendly name as the device's own, so the entity is
    called "All Locks Raman and Sherene" and gives no clue what it is. That
    is what happened to the credential-used event, which had
    ``state_attributes`` and no ``name``.
    """
    strings = json.loads(
        (
            pathlib.Path(__file__).resolve().parent.parent
            / "custom_components"
            / "lock_code_manager"
            / "strings.json"
        ).read_text(encoding="utf-8")
    )
    named = {
        key
        for platform in strings["entity"].values()
        for key, fields in platform.items()
        if fields.get("name")
    }

    assert not (_BACKEND_KEYS - _KEYS_WITHOUT_AN_ENTITY) - named


def test_every_translation_matches_the_strings_it_translates() -> None:
    """
    A string added to one file and not the other ships untranslated.

    ``strings.json`` is what Home Assistant uploads for translation and
    ``translations/en.json`` is what it actually renders for an English
    install, so a string that reaches only the first is one nobody sees.
    The whole document is compared, not just the entity names: the config
    flow reads its field labels from here too, and a rename that lands in
    one file leaves the other naming a key that no longer exists.
    """
    root = (
        pathlib.Path(__file__).resolve().parent.parent
        / "custom_components"
        / "lock_code_manager"
    )
    strings = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (root / "translations" / "en.json").read_text(encoding="utf-8")
    )

    assert strings == english


def test_every_credential_kind_the_event_can_publish_is_translated() -> None:
    """
    The event entity's event types are credential kinds, so name them all.

    Home Assistant asks nothing of an untranslated event type -- it renders
    the raw value, so ``rfid`` appears verbatim in the UI and nothing warns.
    The vocabulary is derived from what the entry's locks advertise, so it
    can hold any ``CredentialType``, and the translation is the only thing
    that turns one into a word a person recognizes.
    """
    strings = json.loads(
        (
            pathlib.Path(__file__).resolve().parent.parent
            / "custom_components"
            / "lock_code_manager"
            / "strings.json"
        ).read_text(encoding="utf-8")
    )
    translated = strings["entity"]["event"][EVENT_CREDENTIAL_USED]["state_attributes"][
        "event_type"
    ]["state"]

    assert set(translated) == {member.value for member in CredentialType}
