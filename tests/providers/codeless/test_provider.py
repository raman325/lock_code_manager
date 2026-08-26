"""Unit tests for the codeless provider itself."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.domain.credentials import CredentialType
from custom_components.lock_code_manager.providers.codeless import CodelessLock
from custom_components.lock_code_manager.providers.virtual import VirtualLock


def _build(cls, hass: HomeAssistant, lock_entry: er.RegistryEntry):
    """Build a provider of ``cls`` over a lock entity, without setting it up."""
    return cls(hass, dr.async_get(hass), er.async_get(hass), None, lock_entry)


async def test_the_store_is_keyed_by_the_declaration_not_the_integration(
    hass: HomeAssistant, codeless_lock_entity: er.RegistryEntry
) -> None:
    """
    Where a member's credentials live follows the declaration, and only it.

    ``domain`` is what diagnostics reports per lock and what the store key
    is built from, so it decides both what somebody debugging is told and
    which file the credentials are in. The member's own platform would name
    the integration that has nothing to do with these credentials; the
    virtual provider's would put two classes holding different things --
    an integration's codes, and a declaration's -- on one file for the same
    entity.
    """
    codeless = _build(CodelessLock, hass, codeless_lock_entity)
    virtual = _build(VirtualLock, hass, codeless_lock_entity)
    await codeless.async_setup(None)
    await virtual.async_setup(None)

    assert codeless.domain == "codeless" != codeless_lock_entity.platform
    assert codeless.domain in codeless._store.key
    assert codeless_lock_entity.entity_id in codeless._store.key
    assert codeless._store.key != virtual._store.key


async def test_it_reports_observing_nothing(
    hass: HomeAssistant, codeless_lock_entity: er.RegistryEntry
) -> None:
    """
    A lock no code can be read from cannot report which one was used, either.

    Uses reach this integration through the ``use_credential`` action, which
    asks nothing of the member it names -- so the honest answer here is the
    one inherited, and a provider claiming otherwise would be advertising an
    observation nobody makes.
    """
    codeless = _build(CodelessLock, hass, codeless_lock_entity)

    assert codeless.supports_code_slot_events is False


async def test_it_advertises_no_limits_and_is_always_connected(
    hass: HomeAssistant, codeless_lock_entity: er.RegistryEntry
) -> None:
    """
    Nothing is asked of the device, so nothing about the device bounds a code.

    A capacity or a length this reported would be invented: the credentials
    live in a Lock Code Manager store, which has neither.
    """
    codeless = _build(CodelessLock, hass, codeless_lock_entity)

    capabilities = await codeless.async_get_capabilities()

    assert await codeless.async_is_integration_connected() is True
    assert capabilities.bounded_slot_count(CredentialType.PIN) is None
    assert not capabilities.supports_user_management
