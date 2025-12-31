"""Typed callback registry for entity lifecycle management.

This module replaces the HA dispatcher mechanism with a type-safe callback
registry pattern. Platforms register callbacks for entity creation, and entities
register callbacks for their own removal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Protocol

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class StandardEntityCallback(Protocol):
    """Protocol for callbacks that create standard slot entities (one per slot)."""

    def __call__(self, slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Create entities for a slot."""
        ...


class LockSlotEntityCallback(Protocol):
    """Protocol for callbacks that create lock-slot entities (one per lock+slot)."""

    def __call__(
        self, lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ) -> None:
        """Create entities for a lock+slot combination."""
        ...


class LockUpdateCallback(Protocol):
    """Protocol for callbacks that handle lock additions."""

    def __call__(self, locks: list[BaseLock]) -> None:
        """Handle lock additions."""
        ...


class LockRemoveCallback(Protocol):
    """Protocol for callbacks that handle lock removal."""

    def __call__(self, lock_entity_id: str) -> None:
        """Handle lock removal."""
        ...


# Type alias for entity removal callbacks (async)
EntityRemoveCallback = Callable[[], Awaitable[None]]

# Type alias for unregister functions
UnregisterFunc = Callable[[], None]


@dataclass
class EntityCallbackRegistry:
    """Registry of entity lifecycle callbacks.

    This replaces the HA dispatcher pattern with explicit, typed callbacks.
    Platforms register their entity creation callbacks here, and entities
    register their removal callbacks.

    Entity Types:
        Standard: Entities created once per slot (e.g., enabled switch, name/PIN
            text fields, event sensor, active binary sensor). These exist for
            every configured slot.

        Lock-slot: Entities created once per lock+slot combination (e.g., PIN
            code sensor showing the actual code on the lock, in-sync binary
            sensor). For 3 locks with 2 slots, this creates 6 entities.

        Keyed: Entities created only when a specific config key has a value
            (e.g., number_of_uses). The key parameter identifies which config
            option triggers entity creation.
    """

    # Entity creation callbacks (platforms register these in async_setup_entry)
    add_standard_entity: list[StandardEntityCallback] = field(default_factory=list)
    add_lock_slot_entity: list[LockSlotEntityCallback] = field(default_factory=list)
    add_keyed_entity: dict[str, list[StandardEntityCallback]] = field(
        default_factory=dict
    )

    # Entity removal callbacks (entities register themselves in async_added_to_hass)
    # Key format: "{slot_num}|{key}" or "{slot_num}|{key}|{lock_entity_id}"
    remove_entity: dict[str, EntityRemoveCallback] = field(default_factory=dict)

    # Lock lifecycle callbacks (entities register to be notified of lock changes)
    lock_added: list[LockUpdateCallback] = field(default_factory=list)
    lock_removed: list[LockRemoveCallback] = field(default_factory=list)

    # --- Registration methods (return unregister functions) ---

    def register_standard_adder(
        self, callback: StandardEntityCallback
    ) -> UnregisterFunc:
        """Register callback for adding standard slot entities.

        Used by: switch, text, event, binary_sensor (active sensor)
        """
        self.add_standard_entity.append(callback)
        return lambda: (
            self.add_standard_entity.remove(callback)
            if callback in self.add_standard_entity
            else None
        )

    def register_lock_slot_adder(
        self, callback: LockSlotEntityCallback
    ) -> UnregisterFunc:
        """Register callback for adding lock-slot entities.

        Used by: sensor (PIN code), binary_sensor (in-sync)
        """
        self.add_lock_slot_entity.append(callback)
        return lambda: (
            self.add_lock_slot_entity.remove(callback)
            if callback in self.add_lock_slot_entity
            else None
        )

    def register_keyed_adder(
        self, key: str, callback: StandardEntityCallback
    ) -> UnregisterFunc:
        """Register callback for adding keyed entities (only when config key is set).

        Used by: number (number_of_uses)
        """
        self.add_keyed_entity.setdefault(key, []).append(callback)
        return lambda: (
            self.add_keyed_entity[key].remove(callback)
            if key in self.add_keyed_entity and callback in self.add_keyed_entity[key]
            else None
        )

    def register_entity_remover(
        self, uid: str, callback: EntityRemoveCallback
    ) -> UnregisterFunc:
        """Register callback for entity removal by unique ID.

        Entities call this in async_added_to_hass to register themselves
        for removal when their slot/key is removed from config.
        """
        self.remove_entity[uid] = callback

        def unregister() -> None:
            self.remove_entity.pop(uid, None)

        return unregister

    def register_lock_added_handler(
        self, callback: LockUpdateCallback
    ) -> UnregisterFunc:
        """Register callback for lock addition notifications."""
        self.lock_added.append(callback)
        return lambda: (
            self.lock_added.remove(callback) if callback in self.lock_added else None
        )

    def register_lock_removed_handler(
        self, callback: LockRemoveCallback
    ) -> UnregisterFunc:
        """Register callback for lock removal notifications."""
        self.lock_removed.append(callback)
        return lambda: (
            self.lock_removed.remove(callback)
            if callback in self.lock_removed
            else None
        )

    # --- Invocation methods (called by __init__.py orchestrator) ---

    @callback
    def invoke_standard_adders(self, slot_num: int, ent_reg: er.EntityRegistry) -> None:
        """Invoke all standard entity creation callbacks."""
        for cb in self.add_standard_entity:
            try:
                cb(slot_num, ent_reg)
            except Exception:
                _LOGGER.exception(
                    "Error in standard entity callback for slot %s", slot_num
                )

    @callback
    def invoke_lock_slot_adders(
        self, lock: BaseLock, slot_num: int, ent_reg: er.EntityRegistry
    ) -> None:
        """Invoke all lock-slot entity creation callbacks."""
        for cb in self.add_lock_slot_entity:
            try:
                cb(lock, slot_num, ent_reg)
            except Exception:
                _LOGGER.exception(
                    "Error in lock-slot entity callback for lock %s slot %s",
                    lock.lock.entity_id,
                    slot_num,
                )

    @callback
    def invoke_keyed_adders(
        self, key: str, slot_num: int, ent_reg: er.EntityRegistry
    ) -> None:
        """Invoke keyed entity creation callbacks for a specific config key."""
        for cb in self.add_keyed_entity.get(key, []):
            try:
                cb(slot_num, ent_reg)
            except Exception:
                _LOGGER.exception(
                    "Error in optional entity callback for key %s slot %s",
                    key,
                    slot_num,
                )

    async def invoke_entity_removers_for_slot(self, slot_num: int) -> None:
        """Invoke entity removal callbacks for an entire slot."""
        prefix = f"{slot_num}|"
        to_remove = [uid for uid in self.remove_entity if uid.startswith(prefix)]
        for uid in to_remove:
            try:
                await self.remove_entity[uid]()
            except Exception:
                _LOGGER.exception("Error removing entity with uid %s", uid)
            finally:
                self.remove_entity.pop(uid, None)

    async def invoke_entity_removers_for_key(self, slot_num: int, key: str) -> None:
        """Invoke entity removal callbacks for a specific slot/key."""
        # Match both "{slot}|{key}" and "{slot}|{key}|{lock}" patterns
        prefix = f"{slot_num}|{key}"
        to_remove = [
            uid
            for uid in self.remove_entity
            if uid == prefix or uid.startswith(f"{prefix}|")
        ]
        for uid in to_remove:
            try:
                await self.remove_entity[uid]()
            except Exception:
                _LOGGER.exception("Error removing entity with uid %s", uid)
            finally:
                self.remove_entity.pop(uid, None)

    @callback
    def invoke_lock_added_handlers(self, locks: list[BaseLock]) -> None:
        """Invoke lock-added callbacks."""
        for cb in self.lock_added:
            try:
                cb(locks)
            except Exception:
                _LOGGER.exception("Error in lock added callback")

    @callback
    def invoke_lock_removed_handlers(self, lock_entity_id: str) -> None:
        """Invoke lock-removed callbacks."""
        for cb in self.lock_removed:
            try:
                cb(lock_entity_id)
            except Exception:
                _LOGGER.exception(
                    "Error in lock removed callback for %s", lock_entity_id
                )
