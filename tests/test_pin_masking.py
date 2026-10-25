"""Test PIN masking and deobfuscation."""

import re

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, DOMAIN
from custom_components.lock_code_manager.model.credentials import CredentialType
from custom_components.lock_code_manager.model.pin_masking import (
    build_pin_deobfuscation_map,
    deobfuscate_pins,
    mask_credential,
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


def test_a_pin_masks_as_it_always_has() -> None:
    """The PIN's token is unchanged, so logs written before still decode."""
    assert mask_credential("1234", 3, "inst", CredentialType.PIN) == mask_pin(
        "1234", 3, "inst"
    )


def test_a_password_masks_apart_from_a_pin_of_the_same_value() -> None:
    """Salted with its type: one token never stands for two credentials."""
    pin = mask_credential("1234", 3, "inst", CredentialType.PIN)
    password = mask_credential("1234", 3, "inst", CredentialType.PASSWORD)

    assert password.startswith("pwd#")
    # The digests differ too, not just the prefixes.
    assert password.removeprefix("pwd#") != pin.removeprefix("pin#")
    assert "1234" not in password


def test_deobfuscation_covers_passwords(hass: HomeAssistant) -> None:
    """A configured password's token decodes like a PIN's."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: []},
        subentries_data=user_subentries(
            {1: {CONF_NAME: "a", CONF_PIN: "1234", "password": "open sesame"}}
        ),
    )
    entry.add_to_hass(hass)
    table = build_pin_deobfuscation_map([entry], "inst")
    token = mask_credential("open sesame", 1, "inst", CredentialType.PASSWORD)

    text, summary = deobfuscate_pins(f"wrote {token}", table)

    assert text == "wrote open sesame"
    assert summary["matched"] == 1
