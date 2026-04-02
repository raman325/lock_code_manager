"""Data model types for lock_code_manager.

Canonical home for dataclasses, type aliases, and structured data types used
across the integration. Enums like SlotCode should migrate here in the future.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .callbacks import EntityCallbackRegistry

if TYPE_CHECKING:
    from .providers import BaseLock


@dataclass
class LockCodeManagerConfigEntryData:
    """Runtime data for a Lock Code Manager config entry."""

    locks: dict[str, BaseLock] = field(default_factory=dict)
    setup_tasks: dict[str | Platform, asyncio.Task[Any]] = field(default_factory=dict)
    callbacks: EntityCallbackRegistry = field(default_factory=EntityCallbackRegistry)


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryData]


@dataclass(frozen=True)
class SlotState:
    """Snapshot of entity states for a slot on a specific lock."""

    active_state: str
    pin_state: str
    name_state: str | None
    code_state: str
    coordinator_code: str | None
