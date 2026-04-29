"""Common constants for tests."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.lock import LockEntity
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers import BaseLock

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
            CONF_ENTITY_ID: "calendar.test_1",
        },
    },
}

SLOT_1_ACTIVE_ENTITY = "binary_sensor.mock_title_code_slot_1_active"
SLOT_1_ENABLED_ENTITY = "switch.mock_title_code_slot_1_enabled"
SLOT_1_EVENT_ENTITY = "event.mock_title_code_slot_1"
SLOT_1_PIN_ENTITY = "text.mock_title_code_slot_1_pin"
SLOT_1_IN_SYNC_ENTITY = "binary_sensor.test_1_code_slot_1_in_sync"

SLOT_2_ENABLED_ENTITY = "switch.mock_title_code_slot_2_enabled"
SLOT_2_NUMBER_OF_USES_ENTITY = "number.mock_title_code_slot_2_number_of_uses"
SLOT_2_ACTIVE_ENTITY = "binary_sensor.mock_title_code_slot_2_active"
SLOT_2_EVENT_ENTITY = "event.mock_title_code_slot_2"
SLOT_2_PIN_ENTITY = "text.mock_title_code_slot_2_pin"
SLOT_2_NAME_ENTITY = "text.mock_title_code_slot_2_name"
SLOT_2_IN_SYNC_ENTITY = "binary_sensor.test_1_code_slot_2_in_sync"


@dataclass(repr=False, eq=False)
class MockLCMLock(BaseLock):
    """Mock Lock Code Manager lock instance."""

    def __init__(self, *args, **kwargs):
        """Initialize mock lock."""
        super().__init__(*args, **kwargs)
        self._connected = True
        self._hard_refresh_interval: timedelta | None = None
        self.codes: dict[int, str] = {1: "1234", 2: "5678"}
        self.service_calls: defaultdict[str, list] = defaultdict(list)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "test"

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return configurable hard refresh interval."""
        return self._hard_refresh_interval

    def set_connected(self, connected: bool) -> None:
        """Set connection state for testing."""
        self._connected = connected

    async def async_is_integration_connected(self) -> bool:
        """Return whether the integration's client/driver/broker is connected."""
        return self._connected

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Perform hard refresh of all codes."""
        self.service_calls["hard_refresh_codes"].append(())
        return await self.async_get_usercodes()

    async def async_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set.
        """
        if self.codes.get(code_slot) == usercode:
            return False
        self.codes[code_slot] = usercode
        self.service_calls["set_usercode"].append((code_slot, usercode, name))
        return True

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        """
        if code_slot not in self.codes:
            return False
        self.codes.pop(code_slot, None)
        self.service_calls["clear_usercode"].append((code_slot,))
        return True

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Return dictionary of code slots and usercodes."""
        snapshot = self.codes.copy()
        self.service_calls["get_usercodes"].append(snapshot)
        return snapshot


@dataclass(repr=False, eq=False)
class MockLCMPushLock(MockLCMLock):
    """Mock lock that supports push-based updates."""

    def __init__(self, *args, **kwargs):
        """Initialize mock push lock."""
        super().__init__(*args, **kwargs)
        self._supports_push = True
        self._subscribe_called = False
        self._unsubscribe_called = False

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return self._supports_push

    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates."""
        self._subscribe_called = True

    def teardown_push_subscription(self) -> None:
        """Unsubscribe from push-based value updates."""
        self._unsubscribe_called = True


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
