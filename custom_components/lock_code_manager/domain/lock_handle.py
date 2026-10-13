"""
One handle per physical lock: who holds it, and when it is torn down.

Several config entries can manage one lock, and they share one provider
instance: one turn on the lock, one coordinator, one push subscription. The
handle is that instance's owner. Entries ``acquire`` it and ``release`` it;
the release that leaves it without holders closes it, and nothing else does.

The registry keeps a handle until its close has finished, not just until its
last holder lets go. An instance still closing can have a write at the lock,
inside the stop grace, and an entry acquiring the lock meanwhile must not
build a second instance and talk over it. It waits for the close, then
builds. A handle that is not closing is joined whatever state its holders
are in: an entry stuck in FAILED_UNLOAD keeps its handle alive, and a new
entry shares it rather than building beside it.

``acquire`` and ``release`` change the holders and each entry's
``runtime_data.locks`` together, with no await in between, so two entries
letting go of one lock at once cannot both find themselves last.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from ..const import DOMAIN

if TYPE_CHECKING:
    from ..providers import BaseLock

_LOGGER = logging.getLogger(__name__)

_REGISTRY_KEY = "lock_handles"


@dataclass(eq=False)
class LockHandle:
    """The one provider instance for a physical lock, and the entries holding it."""

    lock: BaseLock
    holders: set[str] = field(default_factory=set)
    closing: bool = False
    _closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def async_wait_closed(self) -> None:
        """Wait until this handle's close has finished."""
        await self._closed.wait()

    async def async_close(self, hass: HomeAssistant, remove_permanently: bool) -> None:
        """Tear the lock's instance down: its provider, then its coordinator."""
        try:
            await self.lock.async_unload(remove_permanently)
            if self.lock.coordinator is not None:
                await self.lock.coordinator.async_shutdown()
        finally:
            self._finish_close(hass)

    async def async_abandon(self, hass: HomeAssistant) -> None:
        """
        Stop what a lock whose setup failed had started.

        Only the coordinator: the provider never finished setting up, and its
        unload is not safe to run on one that did not (the virtual provider
        writes its store back on unload, which from an instance that never
        read one would erase every code in it).
        """
        try:
            if self.lock.coordinator is not None:
                await self.lock.coordinator.async_shutdown()
        finally:
            self._finish_close(hass)

    @callback
    def _finish_close(self, hass: HomeAssistant) -> None:
        """Take the handle out of the registry and wake whoever waits on it."""
        registry = _registry(hass)
        if registry.get(self.lock.lock.entity_id) is self:
            del registry[self.lock.lock.entity_id]
        self._closed.set()


def _registry(hass: HomeAssistant) -> dict[str, LockHandle]:
    """Return the handles by lock entity id, creating the registry on first use."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(_REGISTRY_KEY, {})


async def async_acquire(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    lock_entity_id: str,
    build: Callable[[], BaseLock],
) -> tuple[LockHandle, bool]:
    """
    Take the lock's handle for ``config_entry``; return it and whether it is new.

    Waits out a handle still closing first. Then, with no await until the
    entry holds it, joins the registered handle or registers one around a
    freshly built instance. The caller sets a new instance up, or waits for
    a joined one's setup, exactly as before handles existed.
    """
    registry = _registry(hass)
    while (handle := registry.get(lock_entity_id)) is not None and handle.closing:
        await handle.async_wait_closed()
    created = handle is None
    if handle is None:
        handle = registry[lock_entity_id] = LockHandle(build())
    handle.holders.add(config_entry.entry_id)
    config_entry.runtime_data.locks[lock_entity_id] = handle.lock
    _LOGGER.debug(
        "%s (%s): %s lock instance %s",
        config_entry.entry_id,
        config_entry.title,
        "Created" if created else "Joined",
        handle.lock,
    )
    return handle, created


@callback
def release(
    hass: HomeAssistant, config_entry: ConfigEntry, lock_entity_id: str
) -> LockHandle | None:
    """
    Let go of the lock for ``config_entry``; return its handle if it must close.

    Returns the handle only when this entry was its last holder. The handle
    is then closing: no entry joins it again, and acquirers wait for the
    caller to close or abandon it. An entry holding nothing for the lock,
    or with no runtime data at all, lets go of nothing.
    """
    if (runtime_data := getattr(config_entry, "runtime_data", None)) is None:
        return None
    if (lock := runtime_data.locks.pop(lock_entity_id, None)) is None:
        return None
    handle = _registry(hass).get(lock_entity_id)
    if handle is None or handle.lock is not lock:
        # Every instance an entry holds came from its handle, so this is a
        # bug; better a lock left running than one torn down under another.
        _LOGGER.error(
            "%s (%s): Lock instance %s has no handle; not tearing it down",
            config_entry.entry_id,
            config_entry.title,
            lock,
        )
        return None
    handle.holders.discard(config_entry.entry_id)
    if handle.holders:
        return None
    handle.closing = True
    return handle
