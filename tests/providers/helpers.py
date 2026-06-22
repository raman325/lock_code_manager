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

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.domain.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
)
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


class ProviderNativeTransportContractTests:
    """
    Enforce that a native transport exception surfaces as ``LockDisconnected``.

    The provider seam contract is that read-path provider methods raise only
    typed ``LockCodeManagerProviderError`` subclasses. The sync layer routes
    ``LockDisconnected`` to a safe retry, but any untyped exception that escapes
    a provider hits the catch-all ``except Exception`` and suspends the slot
    (creating a repair, not self-healing on reconnect).

    The recurring bug (issue #1257, fixed for Matter in PR #1286) is a provider
    that wraps its client's exceptions but misses a NATIVE transport/connection
    exception type that is independent of ``HomeAssistantError`` -- so the read
    path lets it escape unmapped. This mixin injects that native exception at
    the provider's lowest read seam and asserts the read path surfaces
    ``LockDisconnected``.

    Service-based providers (Akuvox, Schlage) reach their integration through
    ``BaseLock.async_call_service``, whose native transport exception is
    ``OSError`` (e.g. a ``ConnectionError`` from an integration that does not
    wrap it in ``HomeAssistantError``). Such providers set
    ``native_transport_read_service`` to the read service name and inherit the
    default injection, which registers that service to raise the native
    exception.

    Client/library-based providers (Matter, Z-Wave JS) instead override
    ``inject_native_transport_error`` to patch their lowest SDK call (or its
    mock) to raise their native exception type.
    """

    # Default native transport exception for service-based providers. Override
    # with the provider's own native non-HomeAssistantError exception instance.
    native_transport_exception: Exception = OSError("connection refused")

    # Service-based providers set this to the read service name; the default
    # injection registers it to raise ``native_transport_exception``.
    native_transport_read_service: str | None = None

    def inject_native_transport_error(
        self, hass: HomeAssistant, provider_lock: BaseLock
    ) -> AbstractContextManager[None] | None:
        """
        Wire the native transport exception into the provider's read seam.

        Return a context manager active for the duration of the read call, or
        ``None`` when the injection is an in-place mutation (e.g. setting a
        mock's ``side_effect``).

        The default targets service-based providers via
        ``native_transport_read_service``. Client/library providers override
        this to patch their lowest SDK call instead.
        """
        service = self.native_transport_read_service
        assert service is not None, (
            "Set native_transport_read_service or override "
            "inject_native_transport_error"
        )

        @contextmanager
        def _inject() -> Iterator[None]:
            register_mock_service(
                hass,
                provider_lock.domain,
                service,
                AsyncMock(side_effect=self.native_transport_exception),
            )
            yield

        return _inject()

    async def test_get_usercodes_surfaces_lock_disconnected_on_native_transport_error(
        self, hass: HomeAssistant, provider_lock: BaseLock
    ) -> None:
        """
        Native transport exception in the read path surfaces as LockDisconnected.

        Guards the issue #1257 bug class: a transport exception independent of
        ``HomeAssistantError`` must NOT escape the read path unmapped (where the
        sync catch-all would suspend the slot) -- the provider has to route it to
        ``LockDisconnected`` so sync retries safely on reconnect.
        """
        injection = self.inject_native_transport_error(hass, provider_lock)
        with injection if injection is not None else nullcontext():
            with pytest.raises(LockDisconnected):
                await provider_lock.async_get_usercodes()
