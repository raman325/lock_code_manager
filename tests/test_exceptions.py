"""Test the exceptions module."""

from dataclasses import dataclass

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.exceptions import (
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

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
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


async def test_base_lock_raises_provider_not_implemented_for_get_usercodes(
    hass: HomeAssistant,
):
    """Test that BaseLock.get_usercodes raises ProviderNotImplementedError."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        lock.get_usercodes()

    assert "MinimalMockLock" in str(exc_info.value)
    assert "get_usercodes" in str(exc_info.value)


async def test_base_lock_raises_provider_not_implemented_for_set_usercode(
    hass: HomeAssistant,
):
    """Test that BaseLock.set_usercode raises ProviderNotImplementedError."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        lock.set_usercode(1, "1234")

    assert "MinimalMockLock" in str(exc_info.value)
    assert "set_usercode" in str(exc_info.value)


async def test_base_lock_raises_provider_not_implemented_for_clear_usercode(
    hass: HomeAssistant,
):
    """Test that BaseLock.clear_usercode raises ProviderNotImplementedError."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        lock.clear_usercode(1)

    assert "MinimalMockLock" in str(exc_info.value)
    assert "clear_usercode" in str(exc_info.value)


async def test_base_lock_raises_provider_not_implemented_for_hard_refresh(
    hass: HomeAssistant,
):
    """Test that BaseLock.hard_refresh_codes raises ProviderNotImplementedError."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        lock.hard_refresh_codes()

    assert "MinimalMockLock" in str(exc_info.value)
    assert "hard_refresh_codes" in str(exc_info.value)


async def test_base_lock_raises_provider_not_implemented_for_subscribe_push(
    hass: HomeAssistant,
):
    """Test that BaseLock.subscribe_push_updates raises ProviderNotImplementedError."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        lock.subscribe_push_updates()

    assert "MinimalMockLock" in str(exc_info.value)
    assert "subscribe_push_updates" in str(exc_info.value)


async def test_base_lock_raises_provider_not_implemented_for_unsubscribe_push(
    hass: HomeAssistant,
):
    """Test that BaseLock.unsubscribe_push_updates raises ProviderNotImplementedError."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock = create_minimal_lock(hass, config_entry)

    with pytest.raises(ProviderNotImplementedError) as exc_info:
        lock.unsubscribe_push_updates()

    assert "MinimalMockLock" in str(exc_info.value)
    assert "unsubscribe_push_updates" in str(exc_info.value)


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
