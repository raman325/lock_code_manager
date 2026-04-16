"""Data model types for lock_code_manager.

Canonical home for dataclasses, type aliases, enums, and structured data types
used across the integration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform

from .callbacks import EntityCallbackRegistry
from .data import EntryConfig

if TYPE_CHECKING:
    from .providers import BaseLock


class SlotCode(StrEnum):
    """Sentinel values for slot codes in coordinator data.

    Used alongside str values: a readable code is a plain string,
    while EMPTY and UNKNOWN represent non-string slot states.
    """

    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass
class LockCodeManagerConfigEntryData:
    """Runtime data for a Lock Code Manager config entry."""

    locks: dict[str, BaseLock] = field(default_factory=dict)
    setup_tasks: dict[str | Platform, asyncio.Task[Any]] = field(default_factory=dict)
    callbacks: EntityCallbackRegistry = field(default_factory=EntityCallbackRegistry)
    # Cached typed view of the entry's current config; refreshed by the
    # update listener on every change. Readers should prefer this over
    # parsing config_entry.data/options directly. See data.EntryConfig.
    config: EntryConfig = field(default_factory=EntryConfig.empty)


type LockCodeManagerConfigEntry = ConfigEntry[LockCodeManagerConfigEntryData]


@dataclass
class SlotEntities:
    """Entity IDs for a single slot's LCM entities.

    All entity ID fields are optional so callers that only need a subset
    can populate what they have. ``config_entry_id`` is included for
    callers iterating across entries who need to track origin.
    """

    slot_num: int
    config_entry_id: str | None = None
    name_entity_id: str | None = None
    pin_entity_id: str | None = None
    enabled_entity_id: str | None = None
    active_entity_id: str | None = None
    number_of_uses_entity_id: str | None = None
    event_entity_id: str | None = None

    def all_entity_ids(self) -> list[str]:
        """Return all non-None entity IDs (excluding config_entry_id)."""
        return [
            eid
            for eid in (
                self.name_entity_id,
                self.pin_entity_id,
                self.enabled_entity_id,
                self.active_entity_id,
                self.number_of_uses_entity_id,
                self.event_entity_id,
            )
            if eid
        ]


@dataclass
class SlotMetadata:
    """Metadata for a single slot from LCM entities."""

    name: str | None = None
    configured_pin: str | None = None
    active: bool | None = None
    enabled: bool | None = None
