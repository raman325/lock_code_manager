"""
Shared test mixins and helpers for service-based lock providers.

Service-based providers (Matter, Schlage, Akuvox) communicate with their
respective integrations via Home Assistant services. They share common
patterns for property checks, connection tests, and error handling.

Each provider test module should define provider-specific fixtures and
inherit from these mixins for the common BaseLock interface tests.

Required fixtures for mixins (define in conftest.py or test module):
    - provider_lock: the lock instance under test
    - provider_config_entry: the provider's mock config entry
    - provider_domain: string, the provider integration's domain
    - provider_lock_class: the provider's BaseLock subclass
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.exceptions import LockCodeManagerError
from custom_components.lock_code_manager.providers import BaseLock


def register_mock_service(
    hass: HomeAssistant,
    domain: str,
    service_name: str,
    handler: AsyncMock,
) -> None:
    """
    Register a mock service that supports responses.

    Replaces the per-provider _register_<provider>_service helpers that were
    identical except for the domain string.
    """

    async def _service_handler(call):
        return await handler(call)

    hass.services.async_register(
        domain,
        service_name,
        _service_handler,
        supports_response=SupportsResponse.OPTIONAL,
    )


class ServiceProviderConnectionTests:
    """
    Shared connection tests for service-based providers.

    Service-based providers (Matter, Schlage, Akuvox) all use the same pattern
    for async_is_integration_connected: check that the lock's config entry exists
    and is in the LOADED state.
    """

    async def test_is_integration_connected_not_loaded(
        self, provider_lock: BaseLock
    ) -> None:
        """Test integration not connected when config entry is not loaded."""
        assert await provider_lock.async_is_integration_connected() is False

    async def test_is_integration_connected_loaded(
        self, provider_lock: BaseLock
    ) -> None:
        """Test integration connected when config entry is loaded."""
        mock_entry = MagicMock()
        mock_entry.state = ConfigEntryState.LOADED
        provider_lock.lock_config_entry = mock_entry
        assert await provider_lock.async_is_integration_connected() is True

    async def test_is_integration_connected_no_config_entry(
        self,
        hass: HomeAssistant,
        provider_config_entry: MockConfigEntry,
        provider_domain: str,
        provider_lock_class: type[BaseLock],
    ) -> None:
        """Test integration raises when lock has no config entry."""
        entity_reg = er.async_get(hass)
        lock_entity = entity_reg.async_get_or_create(
            "lock",
            provider_domain,
            "test_no_config_entry",
            config_entry=provider_config_entry,
        )
        lock = provider_lock_class(
            hass,
            dr.async_get(hass),
            entity_reg,
            None,
            lock_entity,
        )
        with pytest.raises(LockCodeManagerError):
            await lock.async_is_integration_connected()


class ServiceProviderDeviceAvailabilityTests:
    """
    Shared device availability tests for providers that use service calls.

    Subclasses must define a class attribute:
        availability_service: str - the service name used to check availability
    """

    availability_service: str

    async def test_is_device_available_success(
        self, hass: HomeAssistant, provider_lock: BaseLock
    ) -> None:
        """Test device availability returns True on successful service call."""
        lock_entity_id = provider_lock.lock.entity_id
        mock_response = {lock_entity_id: {}}
        handler = AsyncMock(return_value=mock_response)
        register_mock_service(
            hass, provider_lock.domain, self.availability_service, handler
        )
        assert await provider_lock.async_is_device_available() is True

    async def test_is_device_available_error(
        self, hass: HomeAssistant, provider_lock: BaseLock
    ) -> None:
        """Test device availability returns False when service call fails."""
        handler = AsyncMock(side_effect=HomeAssistantError("device offline"))
        register_mock_service(
            hass, provider_lock.domain, self.availability_service, handler
        )
        assert await provider_lock.async_is_device_available() is False
