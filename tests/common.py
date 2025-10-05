"""Common constants for tests."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.lock import LockEntity
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from custom_components.lock_code_manager.const import (
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers import BaseLock

LOCK_DATA = f"mock_{DOMAIN}"

LOCK_1_ENTITY_ID = "lock.test_1"
LOCK_2_ENTITY_ID = "lock.test_2"

BASE_CONFIG = {
    CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {
            CONF_NAME: "test2",
            CONF_PIN: "5678",
            CONF_ENABLED: True,
            CONF_NUMBER_OF_USES: 5,
            CONF_CALENDAR: "calendar.test_1",
        },
    },
}

ENABLED_ENTITY = "switch.mock_title_code_slot_2_enabled"
NUMBER_OF_USES_ENTITY = "number.mock_title_code_slot_2_number_of_uses"
ACTIVE_ENTITY = "binary_sensor.mock_title_code_slot_2_active"
EVENT_ENTITY = "event.mock_title_code_slot_2"
PIN_ENTITY = "text.mock_title_code_slot_2_pin"
NAME_ENTITY = "text.mock_title_code_slot_2_name"
PIN_SYNCED_ENTITY = "binary_sensor.test_1_code_slot_2_pin_synced"


@dataclass(repr=False, eq=False)
class MockLCMLock(BaseLock):
    """Mock Lock Code Manager lock instance."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "test"

    async def async_setup(self) -> None:
        """Set up lock asynchronously."""
        self.setup()

    @callback
    def setup(self) -> None:
        """Set up lock."""
        self.hass.data.setdefault(LOCK_DATA, {}).setdefault(
            self.lock.entity_id,
            {"codes": {1: "1234", 2: "5678"}, "service_calls": defaultdict(list)},
        )

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock asynchronously."""
        self.unload(remove_permanently)

    @callback
    def unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        self.hass.data[LOCK_DATA].pop(self.lock.entity_id)
        if not self.hass.data[LOCK_DATA]:
            self.hass.data.pop(LOCK_DATA)

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return True

    def hard_refresh_codes(self) -> None:
        """
        Perform hard refresh all codes.

        Needed for integraitons where usercodes are cached and may get out of sync with
        the lock.
        """
        self.hass.data[LOCK_DATA][self.lock.entity_id]["service_calls"][
            "hard_refresh_codes"
        ].append(())

    def set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        self.hass.data[LOCK_DATA][self.lock.entity_id]["codes"][code_slot] = usercode
        self.hass.data[LOCK_DATA][self.lock.entity_id]["service_calls"][
            "set_usercode"
        ].append((code_slot, usercode, name))

    def clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        self.hass.data[LOCK_DATA][self.lock.entity_id]["codes"].pop(code_slot, None)
        self.hass.data[LOCK_DATA][self.lock.entity_id]["service_calls"][
            "clear_usercode"
        ].append((code_slot,))

    def get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }
        """
        codes = self.hass.data[LOCK_DATA][self.lock.entity_id]["codes"]
        self.hass.data[LOCK_DATA][self.lock.entity_id]["service_calls"][
            "get_usercodes"
        ].append(codes)
        return codes


class MockLockEntity(LockEntity):
    """Mocked lock entity."""

    _attr_has_entity_name = True

    def __init__(self, name: str) -> None:
        """Initialize the lock."""
        self._attr_name = name
        self._attr_unique_id = slugify(name)
        self._attr_is_locked = False
        self._attr_has_entity_name = False
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"lock.{slugify(name)}")}, name=name
        )
        super().__init__()


class MockCalendarEntity(CalendarEntity):
    """Test Calendar entity."""

    _attr_has_entity_name = True

    def __init__(self, name: str, events: list[CalendarEvent] | None = None) -> None:
        """Initialize entity."""
        self._attr_name = name.capitalize()
        self._events = events or []

        self._attr_unique_id = slugify(name)

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._events[0] if self._events else None

    @callback
    def create_event(self, **kwargs) -> CalendarEvent:
        """Create a new fake event, used by tests."""
        event = CalendarEvent(
            start=kwargs["dtstart"], end=kwargs["dtend"], summary=kwargs["summary"]
        )
        self._events.append(event)
        self.async_write_ha_state()
        return event

    @callback
    def delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event on the calendar."""
        for event in self._events:
            if event.uid == uid:
                self._events.remove(event)
                self.async_write_ha_state()
                return

    @callback
    def get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        assert start_date < end_date
        events = []
        for event in self._events:
            if event.start_datetime_local >= end_date:
                continue
            if event.end_datetime_local < start_date:
                continue
            events.append(event)
        return events
