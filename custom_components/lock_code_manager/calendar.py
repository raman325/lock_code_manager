"""Calendar for lock_code_manager."""
from __future__ import annotations

import logging
from pathlib import Path

from ical.calendar import Calendar
from ical.calendar_stream import IcsCalendarStream

from homeassistant.components.local_calendar.calendar import LocalCalendarEntity
from homeassistant.components.local_calendar.store import LocalCalendarStore
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CALENDAR, CONF_LOCKS, DOMAIN
from .entity import BaseLockCodeManagerEntity
from .providers import BaseLock

_LOGGER = logging.getLogger(__name__)


class LockCodeManagerStorage(LocalCalendarStore):
    """Lock code manager calendar storage."""

    async def async_delete(self) -> None:
        """Delete the calendar from storage."""
        async with self._lock:
            await self._hass.async_add_executor_job(self._delete)

    def _delete(self) -> None:
        """Delete the calendar from storage."""
        self._path.unlink(True)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> bool:
    """Setup config entry."""
    locks: list[BaseLock] = list(
        hass.data[DOMAIN][config_entry.entry_id][CONF_LOCKS].values()
    )

    # @callback
    async def add_calendar_entities(slot_num: int) -> None:
        """Add calendar entities for slot."""
        store = LockCodeManagerStorage(
            hass,
            Path(
                hass.config.path(
                    f".storage/lock_code_manager.{config_entry.entry_id}_{slot_num}_{CONF_CALENDAR}.ics"
                )
            ),
        )
        ics = await store.async_load()
        calendar = IcsCalendarStream.calendar_from_ics(ics)
        calendar.prodid = "-//homeassistant.io//lock_code_manager 1.0//EN"
        async_add_entities(
            [
                LockCodeManagerCalendar(
                    config_entry, locks, slot_num, CONF_CALENDAR, store, calendar
                )
            ],
            True,
        )

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_{config_entry.entry_id}_add_{CONF_CALENDAR}",
            add_calendar_entities,
        )
    )
    return True


class LockCodeManagerCalendar(BaseLockCodeManagerEntity, LocalCalendarEntity):
    """Calendar entity for lock code manager."""

    def __init__(
        self,
        config_entry: ConfigEntry,
        locks: list[BaseLock],
        slot_num: int,
        key: str,
        store: LockCodeManagerStorage,
        calendar: Calendar,
    ) -> None:
        """Initialize calendar entity."""
        BaseLockCodeManagerEntity.__init__(self, config_entry, locks, slot_num, key)
        LocalCalendarEntity.__init__(self, store, calendar, self.name, self.unique_id)
        self._store = store

    async def _async_remove(self) -> None:
        """Handle entity removal."""
        _LOGGER.info("Removing calendar %s", self._store._path)
        await self._store.async_delete()

    async def async_added_to_hass(self) -> None:
        """Handle entity added to hass."""
        await BaseLockCodeManagerEntity.async_added_to_hass(self)
        await LocalCalendarEntity.async_added_to_hass(self)
