"""Test the exceptions module."""

from dataclasses import dataclass

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    EntityNotFoundError,
    LockCodeManagerError,
    LockDisconnected,
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.providers._base import BaseLock


@dataclass(repr=False, eq=False)
class MinimalMockLock(BaseLock):
    """Minimal mock lock that doesn't implement required methods."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "test"

    def is_integration_connected(self) -> bool:
        """Return whether the integration's client/driver/broker is connected."""
        return True


def create_minimal_lock(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> MinimalMockLock:
    """Create a minimal mock lock for testing."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        f"test_lock_{config_entry.entry_id}",
        config_entry=config_entry,
    )

    return MinimalMockLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )


# =============================================================================
# ProviderNotImplementedError Tests
# =============================================================================


def test_provider_not_implemented_error_basic():
    """Test ProviderNotImplementedError with basic parameters."""

    class FakeProvider:
        pass

    provider = FakeProvider()
    err = ProviderNotImplementedError(provider, "some_method")

    assert "FakeProvider" in str(err)
    assert "some_method" in str(err)
    assert "does not implement" in str(err)


def test_provider_not_implemented_error_with_guidance():
    """Test ProviderNotImplementedError with guidance."""

    class FakeProvider:
        pass

    provider = FakeProvider()
    err = ProviderNotImplementedError(
        provider, "some_method", "Override this to do something."
    )

    assert "FakeProvider" in str(err)
    assert "some_method" in str(err)
    assert "Override this to do something." in str(err)


def test_provider_not_implemented_error_inherits_correctly():
    """Test ProviderNotImplementedError inherits from both base classes."""

    class FakeProvider:
        pass

    err = ProviderNotImplementedError(FakeProvider(), "test")

    assert isinstance(err, LockCodeManagerError)
    assert isinstance(err, NotImplementedError)


@pytest.mark.parametrize(
    ("method_name", "call"),
    [
        ("get_usercodes", lambda lock: lock.get_usercodes()),
        ("set_usercode", lambda lock: lock.set_usercode(1, "1234")),
        ("clear_usercode", lambda lock: lock.clear_usercode(1)),
        ("hard_refresh_codes", lambda lock: lock.hard_refresh_codes()),
        ("subscribe_push_updates", lambda lock: lock.subscribe_push_updates()),
        ("unsubscribe_push_updates", lambda lock: lock.unsubscribe_push_updates()),
    ],
)
async def test_base_lock_raises_provider_not_implemented(
    hass: HomeAssistant, method_name: str, call
):
    """Test that BaseLock raises ProviderNotImplementedError for unimplemented methods."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        call(lock)

    assert "MinimalMockLock" in str(exc_info.value)
    assert method_name in str(exc_info.value)


# =============================================================================
# Other Exception Tests
# =============================================================================


def test_lock_code_manager_error_is_base():
    """Test LockCodeManagerError is the base exception."""
    err = LockCodeManagerError("test error")
    assert str(err) == "test error"


def test_lock_disconnected_inherits_from_base():
    """Test LockDisconnected inherits from LockCodeManagerError."""
    err = LockDisconnected("lock offline")
    assert isinstance(err, LockCodeManagerError)
    assert "lock offline" in str(err)


async def test_entity_not_found_error(hass: HomeAssistant):
    """Test EntityNotFoundError contains lock, slot, and key info."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)
    err = EntityNotFoundError(lock, 5, "pin")

    assert err.lock is lock
    assert err.slot_num == 5
    assert err.key == "pin"
    assert "slot 5" in str(err)
    assert "pin" in str(err)


# =============================================================================
# CodeRejectedError Tests
# =============================================================================


def test_code_rejected_error_default_reason():
    """Test CodeRejectedError with default reason."""
    err = CodeRejectedError(code_slot=3, lock_entity_id="lock.front_door")
    assert err.code_slot == 3
    assert err.lock_entity_id == "lock.front_door"
    assert "lock.front_door" in str(err)
    assert "slot 3" in str(err)
    assert "appears to reject" in str(err)


def test_code_rejected_error_custom_reason():
    """Test CodeRejectedError with custom reason."""
    err = CodeRejectedError(
        code_slot=3, lock_entity_id="lock.front_door", reason="PIN too short"
    )
    assert "PIN too short" in str(err)
    assert "appears to reject" not in str(err)


def test_code_rejected_error_inherits_from_base():
    """Test CodeRejectedError inherits from LockCodeManagerError."""
    err = CodeRejectedError(code_slot=1, lock_entity_id="lock.test")
    assert isinstance(err, LockCodeManagerError)


# =============================================================================
# DuplicateCodeError Tests
# =============================================================================


@pytest.mark.parametrize(
    ("managed", "expected_word"),
    [
        (False, "unmanaged"),
        (True, "managed"),
    ],
)
def test_duplicate_code_error_managed_label(managed: bool, expected_word: str):
    """Test DuplicateCodeError message labels managed/unmanaged correctly."""
    err = DuplicateCodeError(
        code_slot=3,
        conflicting_slot=7,
        conflicting_slot_managed=managed,
        lock_entity_id="lock.front_door",
    )
    assert err.code_slot == 3
    assert err.conflicting_slot == 7
    assert err.conflicting_slot_managed is managed
    assert err.lock_entity_id == "lock.front_door"
    assert expected_word in str(err)
    assert "lock.front_door" in str(err)
    assert "slot 3" in str(err)
    assert "slot 7" in str(err)
    if not managed:
        assert "unmanaged" in str(err)
    else:
        assert "unmanaged" not in str(err)


def test_duplicate_code_error_inherits_from_code_rejected():
    """Test DuplicateCodeError inherits from CodeRejectedError."""
    err = DuplicateCodeError(
        code_slot=1,
        conflicting_slot=2,
        conflicting_slot_managed=False,
        lock_entity_id="lock.test",
    )
    assert isinstance(err, CodeRejectedError)
    assert isinstance(err, LockCodeManagerError)
