"""Test utility functions."""

import re

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.util import (
    async_disable_slot,
    mask_pin,
)

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
