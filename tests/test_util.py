"""Test utility functions."""

import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.const import CONF_LOCKS, DOMAIN
from custom_components.lock_code_manager.domain.util import (
    async_disable_slot,
    build_pin_deobfuscation_map,
    deobfuscate_pins,
    lock_display_name,
    mask_pin,
)

from .common import user_subentries

INSTANCE_ID = "test-instance-uuid"
LOCK = "lock.front_door"


@pytest.mark.parametrize("pin", ["1234", "5678", "0"])
def test_mask_pin_returns_hash(pin: str):
    """Test mask_pin returns a pin# prefixed 8-char lowercase hex hash."""
    result = mask_pin(pin, LOCK, INSTANCE_ID)
    assert re.fullmatch(r"pin#[0-9a-f]{8}", result), f"Unexpected format: {result}"


def test_mask_pin_deterministic():
    """Test same PIN + lock always produces the same hash."""
    assert mask_pin("1234", LOCK, INSTANCE_ID) == mask_pin("1234", LOCK, INSTANCE_ID)


def test_mask_pin_differs_by_lock():
    """Test same PIN on different locks produces different hashes."""
    assert mask_pin("1234", "lock.front", INSTANCE_ID) != mask_pin(
        "1234", "lock.back", INSTANCE_ID
    )


def test_mask_pin_differs_by_pin():
    """Test different PINs on the same lock produce different hashes."""
    assert mask_pin("1234", LOCK, INSTANCE_ID) != mask_pin("5678", LOCK, INSTANCE_ID)


def test_mask_pin_differs_by_instance():
    """Test same PIN+lock on different instances produces different hashes."""
    assert mask_pin("1234", LOCK, "instance-a") != mask_pin("1234", LOCK, "instance-b")


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        (None, "<empty>"),
        ("", "<empty>"),
    ],
)
def test_mask_pin_empty(pin: str | None, expected: str):
    """Test mask_pin returns <empty> for None and empty string."""
    assert mask_pin(pin, LOCK, INSTANCE_ID) == expected


async def test_async_disable_slot_creates_repair_issue(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_disable_slot creates a repair issue when reason is provided."""
    ent_reg = er.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    result = await async_disable_slot(
        hass,
        ent_reg,
        entry_id,
        2,
        reason="Duplicate code detected",
        lock_entity_id="lock.test_1",
    )
    assert result is True

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"slot_disabled_{entry_id}_2"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == "warning"
    assert issue.is_fixable is True


def test_deobfuscate_pins_replaces_known_tokens():
    """Known tokens get replaced; unknown tokens are left verbatim."""
    pin = "1234"
    token = mask_pin(pin, 1, INSTANCE_ID)
    unknown_token = "pin#deadbeef"
    text = f"Setting usercode on lock.front_door slot 1 (pin={token}) and also {unknown_token}"

    deobfuscated, summary = deobfuscate_pins(text, {token: pin})

    assert pin in deobfuscated
    assert token not in deobfuscated
    assert unknown_token in deobfuscated
    assert summary == {"total": 2, "matched": 1, "unmatched_tokens": [unknown_token]}


def test_deobfuscate_pins_no_tokens_in_text():
    """Empty summary when the input has no pin# tokens at all."""
    text = "Nothing to deobfuscate here"
    deobfuscated, summary = deobfuscate_pins(text, {})
    assert deobfuscated == text
    assert summary == {"total": 0, "matched": 0, "unmatched_tokens": []}


def test_deobfuscate_pins_counts_each_occurrence():
    """Same token appearing multiple times is replaced each time and counted as multiple."""
    pin = "5678"
    token = mask_pin(pin, 2, INSTANCE_ID)
    text = f"first {token} second {token} third {token}"

    deobfuscated, summary = deobfuscate_pins(text, {token: pin})

    assert deobfuscated.count(pin) == 3
    assert summary["total"] == 3
    assert summary["matched"] == 3
    assert summary["unmatched_tokens"] == []


def test_deobfuscate_pins_unmatched_tokens_deduplicated():
    """Repeated unmatched tokens appear once in the summary list."""
    text = "pin#aaaaaaaa once, pin#aaaaaaaa twice, pin#bbbbbbbb"

    _deobfuscated, summary = deobfuscate_pins(text, {})

    assert summary["total"] == 3
    assert summary["matched"] == 0
    assert summary["unmatched_tokens"] == ["pin#aaaaaaaa", "pin#bbbbbbbb"]


def test_deobfuscate_pins_does_not_match_partial_tokens():
    """Tokens shorter or longer than 8 hex chars are not matched."""
    text = "pin#abc pin#abcdefgh pin#abcdef0123456789"

    deobfuscated, summary = deobfuscate_pins(text, {})

    # First is too short (3 chars), second has 'g' (non-hex), third is too long.
    # None should be counted.
    assert summary["total"] == 0
    assert deobfuscated == text


async def test_build_pin_deobfuscation_map_uses_configured_pins(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Map covers every slot with a non-empty PIN and matches mask_pin output."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    table = build_pin_deobfuscation_map(entries, INSTANCE_ID)

    # BASE_CONFIG has slot 1 (pin 1234) and slot 2 (pin 5678) — both should
    # round-trip through mask_pin to the same tokens this builds.
    assert table[mask_pin("1234", 1, INSTANCE_ID)] == "1234"
    assert table[mask_pin("5678", 2, INSTANCE_ID)] == "5678"
    assert len(table) == 2


async def test_build_pin_deobfuscation_map_skips_empty_pin_slots(
    hass: HomeAssistant,
):
    """A slot with no configured PIN contributes no entry to the map.

    ``mask_pin`` returns the sentinel ``<empty>`` for a falsy PIN, which is
    not a token worth reversing, so the empty slot must be skipped rather
    than mapped to ``<empty>``.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: []},
        subentries_data=user_subentries(
            {
                1: {CONF_NAME: "a", CONF_PIN: "1234", CONF_ENABLED: True},
                2: {CONF_NAME: "b", CONF_PIN: "", CONF_ENABLED: False},
            }
        ),
    )
    entry.add_to_hass(hass)

    table = build_pin_deobfuscation_map([entry], INSTANCE_ID)

    assert table == {mask_pin("1234", 1, INSTANCE_ID): "1234"}


async def test_async_disable_slot_returns_false_when_switch_not_found(
    hass: HomeAssistant,
):
    """async_disable_slot returns False when no switch entity exists for the slot."""
    ent_reg = er.async_get(hass)

    result = await async_disable_slot(hass, ent_reg, "nonexistent_entry", 99)

    assert result is False


async def test_async_disable_slot_no_issue_without_reason(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_disable_slot does not create a repair issue without reason."""
    ent_reg = er.async_get(hass)
    entry_id = lock_code_manager_config_entry.entry_id

    result = await async_disable_slot(
        hass,
        ent_reg,
        entry_id,
        2,
    )
    assert result is True

    issue_registry = async_get_issue_registry(hass)
    matching_issues = [
        issue
        for issue in issue_registry.issues.values()
        if issue.domain == DOMAIN and issue.issue_id.startswith("slot_disabled_")
    ]
    assert len(matching_issues) == 0


async def test_lock_display_name_prefers_the_name_on_screen(
    hass: HomeAssistant,
) -> None:
    """The friendly name is the string a person would use to say which lock."""
    hass.states.async_set("lock.front_door", "locked", {"friendly_name": "Front Door"})

    assert lock_display_name(hass, "lock.front_door") == "Front Door"


async def test_lock_display_name_falls_back_to_the_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """
    A lock named by its device is still named.

    This is the shape Z-Wave, Matter and most modern integrations use:
    ``has_entity_name`` with no name of its own, so ``original_name`` is
    empty and the whole visible name comes from the device. Reading the
    registry row alone names such a lock ``lock.front_door``, which is
    what every per-lock entity was called.
    """
    entry = MockConfigEntry(domain="zwave_js")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zwave_js", "node-3")},
        name="Front Door",
    )
    entity = entity_registry.async_get_or_create(
        "lock",
        "zwave_js",
        "node-3-98-0-currentMode",
        device_id=device.id,
        original_name="",
        suggested_object_id="front_door",
    )
    assert entity.entity_id == "lock.front_door"

    assert lock_display_name(hass, "lock.front_door") == "Front Door"


async def test_lock_display_name_prefers_a_renamed_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """What the person called it beats what the integration called it."""
    entry = MockConfigEntry(domain="zwave_js")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zwave_js", "node-4")},
        name="Schlage BE469",
    )
    device_registry.async_update_device(device.id, name_by_user="Back Door")
    entity_registry.async_get_or_create(
        "lock",
        "zwave_js",
        "node-4-98-0-currentMode",
        device_id=device.id,
        original_name="",
        suggested_object_id="back_door",
    )

    assert lock_display_name(hass, "lock.back_door") == "Back Door"


async def test_lock_display_name_uses_the_entity_name_when_it_has_one(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """An integration that names its lock entity is taken at its word."""
    entity_registry.async_get_or_create(
        "lock",
        "demo",
        "somelock",
        original_name="Side Gate",
        suggested_object_id="side_gate",
    )

    assert lock_display_name(hass, "lock.side_gate") == "Side Gate"


async def test_lock_display_name_falls_back_to_the_object_id(
    hass: HomeAssistant,
) -> None:
    """Nothing left to ask: the object id is what the entity id was slugged from."""
    assert lock_display_name(hass, "lock.garage_side") == "garage side"
