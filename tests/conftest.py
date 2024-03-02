"""Fixtures for lock_code_manager tests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    CalendarEntity,
    CalendarEvent,
)
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, LockEntity
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.providers import BaseLock

from .common import BASE_CONFIG, LOCK_DATA

pytest_plugins = ["pytest_homeassistant_custom_component"]

TEST_DOMAIN = "test"


@pytest.fixture(autouse=True)
def aiohttp_client(event_loop, aiohttp_client, socket_enabled):
    """Return aiohttp_client and allow opening sockets."""
    return aiohttp_client


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


@dataclass(repr=False, eq=False)
class MockLCMLock(BaseLock):
    """Mock Lock Code Manager lock instance."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "test"

    def setup(self) -> None:
        """Set up lock."""
        self.hass.data.setdefault(LOCK_DATA, {}).setdefault(
            self.lock.entity_id,
            {"codes": {1: "1234", 2: "5678"}, "service_calls": defaultdict(list)},
        )

    def unload(self) -> None:
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


class MockFlow(ConfigFlow):
    """Test flow."""


@pytest.fixture(name="mock_config_flow")
def config_flow_fixture(hass: HomeAssistant) -> Generator[None, None, None]:
    """Mock config flow."""
    mock_platform(hass, f"{TEST_DOMAIN}.config_flow")

    with mock_config_flow(TEST_DOMAIN, MockFlow):
        yield


@pytest.fixture(name="mock_lock_config_entry")
async def mock_lock_config_entry_fixture(hass: HomeAssistant, mock_config_flow):
    """Set up lock entities using an entity platform."""

    async def async_setup_entry_init(
        hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Set up test config entry."""
        for platform in (Platform.CALENDAR, Platform.LOCK):
            await hass.config_entries.async_forward_entry_setup(config_entry, platform)
        return True

    async def async_unload_entry_init(
        hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Unload test config entry."""
        for platform in (Platform.CALENDAR, Platform.LOCK):
            await hass.config_entries.async_forward_entry_unload(config_entry, platform)
        return True

    MockPlatform(hass, f"{TEST_DOMAIN}.config_flow")
    mock_integration(
        hass,
        MockModule(
            TEST_DOMAIN,
            async_setup_entry=async_setup_entry_init,
            async_unload_entry=async_unload_entry_init,
        ),
    )

    config_entry = MockConfigEntry(domain=TEST_DOMAIN)

    async def async_setup_entry_lock_platform(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Set up test lock platform via config entry."""
        async_add_entities([MockLockEntity("test_1"), MockLockEntity("test_2")])

    mock_platform(
        hass,
        f"{TEST_DOMAIN}.{LOCK_DOMAIN}",
        MockPlatform(async_setup_entry=async_setup_entry_lock_platform),
    )

    calendar = hass.data["lock_code_manager_calendar"] = MockCalendarEntity("test")

    async def async_setup_entry_calendar_platform(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Set up test calendar platform via config entry."""
        async_add_entities([calendar])

    mock_platform(
        hass,
        f"{TEST_DOMAIN}.{CALENDAR_DOMAIN}",
        MockPlatform(async_setup_entry=async_setup_entry_calendar_platform),
    )

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.fixture(name="lock_code_manager_config_entry")
async def lock_code_manager_config_entry_fixture(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
):
    """Set up the config entry for lock code manager."""
    monkeypatch.setattr(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock},
    )

    assert await async_setup_component(hass, "lovelace", {})
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)
