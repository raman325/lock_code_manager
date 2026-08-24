"""Lock-instance queries and factory (provider-aware layer)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_AREA_ID, ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback, split_entity_id
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)

from ..providers import BaseLock, resolve_provider_class_for_entity
from .exceptions import UnclaimedLockError
from .queries import iter_loaded_lcm_entries

_LOGGER = logging.getLogger(__name__)


@callback
def async_create_lock_instance(
    hass: HomeAssistant,
    dev_reg: dr.DeviceRegistry,
    ent_reg: er.EntityRegistry,
    config_entry: ConfigEntry,
    lock_entity_id: str,
) -> BaseLock:
    """Generate lock from config entry."""
    lock_entry = ent_reg.async_get(lock_entity_id)
    assert lock_entry
    lock_config_entry = hass.config_entries.async_get_entry(lock_entry.config_entry_id)
    lock_cls = resolve_provider_class_for_entity(dev_reg, lock_entry)
    if lock_cls is None:
        # Selection-time validation and this guard share one rule: never
        # guess a provider for an unclaimed lock.
        raise UnclaimedLockError(
            f"No Lock Code Manager provider claims {lock_entity_id} "
            f"(platform {lock_entry.platform})"
        )
    lock = lock_cls(hass, dev_reg, ent_reg, lock_config_entry, lock_entry)
    _LOGGER.debug(
        "%s (%s): Created lock instance %s",
        config_entry.entry_id,
        config_entry.title,
        lock,
    )
    return lock


@asynccontextmanager
async def borrowed_lock_instance(lock: BaseLock) -> AsyncIterator[BaseLock]:
    """
    Yield a provider built for one query and release its transport afterwards.

    Answering a question about a lock can leave transport behind: an MQTT
    provider subscribes to its lock's topics on the way to answering, and a
    zwave-js-ui one also opens its gateway's api channel. Nothing else ever
    tears a throwaway down, so every leftover subscription outlives the query
    and goes on firing code slot events alongside the entry's real provider --
    one more ghost per config flow attempt, per keypad press.

    Releasing the transport is the whole of what a borrowed instance
    acquired, which is why this is not ``async_unload``. Unload means "this
    lock is leaving the entry", and providers act on that: the virtual lock
    writes its store back, which from an instance that never read one would
    erase every code in it.
    """
    try:
        yield lock
    finally:
        lock.unsubscribe_push_updates()


def get_managed_locks(hass: HomeAssistant) -> dict[str, BaseLock]:
    """
    Return the union of locks across all loaded Lock Code Manager entries.

    When two entries share the same physical lock they hold the same
    ``BaseLock`` instance, so a flat ``entity_id -> BaseLock`` mapping is
    well-defined.
    """
    return {
        entity_id: lock
        for entry in iter_loaded_lcm_entries(hass)
        for entity_id, lock in entry.runtime_data.locks.items()
    }


def get_managed_lock(hass: HomeAssistant, lock_entity_id: str) -> BaseLock:
    """Get a managed lock by entity ID, raising if not found."""
    lock = next(
        (
            entry.runtime_data.locks[lock_entity_id]
            for entry in iter_loaded_lcm_entries(hass)
            if lock_entity_id in entry.runtime_data.locks
        ),
        None,
    )
    if not lock:
        raise ServiceValidationError(
            f"Lock {lock_entity_id} is not managed by Lock Code Manager"
        )
    return lock


def get_locks_from_targets(
    hass: HomeAssistant, target_data: dict[str, Any]
) -> set[BaseLock]:
    """Get lock(s) from target IDs."""
    area_ids: list[str] = cv.ensure_list(target_data.get(ATTR_AREA_ID, []))
    device_ids: list[str] = cv.ensure_list(target_data.get(ATTR_DEVICE_ID, []))
    entity_ids: list[str] = cv.ensure_list(target_data.get(ATTR_ENTITY_ID, []))
    managed_locks = get_managed_locks(hass)
    lcm_lock_entity_ids = managed_locks.keys()
    lock_entity_ids: set[str] = set()
    ent_reg = er.async_get(hass)
    lock_entity_ids.update(
        ent.entity_id
        for area_id in area_ids
        for ent in er.async_entries_for_area(ent_reg, area_id)
        if ent.domain == LOCK_DOMAIN
    )
    lock_entity_ids.update(
        ent.entity_id
        for device_id in device_ids
        for ent in er.async_entries_for_device(ent_reg, device_id)
        if ent.domain == LOCK_DOMAIN
    )
    # Split invalid (non-lock domain) from unmanaged lock entities for clearer logs.
    invalid_entities: set[str] = set()
    unmanaged_entities: set[str] = set()
    for entity_id in entity_ids:
        domain = split_entity_id(entity_id)[0]
        if domain != LOCK_DOMAIN:
            invalid_entities.add(entity_id)
            continue
        if entity_id in lcm_lock_entity_ids:
            lock_entity_ids.add(entity_id)
        else:
            unmanaged_entities.add(entity_id)

    if invalid_entities:
        _LOGGER.warning(
            "%s lock(s) are invalid lock entities: %s",
            len(invalid_entities),
            ", ".join(invalid_entities),
        )
    if unmanaged_entities:
        _LOGGER.warning(
            "%s lock(s) are not managed by Lock Code Manager: %s",
            len(unmanaged_entities),
            ", ".join(unmanaged_entities),
        )

    return {
        lock
        for ent_id in (lock_entity_ids & lcm_lock_entity_ids)
        if (lock := managed_locks.get(ent_id))
    }
