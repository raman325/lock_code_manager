"""Common constants for tests."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any, Literal
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.lock import LockEntity
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ENABLED, CONF_ENTITY_ID, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.util import slugify

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_IN_SYNC,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.allocation import build_lock_instance
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.credentials import WriteResult
from custom_components.lock_code_manager.domain.models import SlotCredential
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
            # Legacy field name; the migration in async_setup_entry strips it.
            "number_of_uses": 5,
            CONF_ENTITY_ID: "calendar.test_1",
        },
    },
}


UNCLAIMED_IDENTIFIER = "somebridge_1"
UNCLAIMED_UNIQUE_ID = f"{UNCLAIMED_IDENTIFIER}_lock"


@contextmanager
def reading_for():
    """
    Record the entry every lock read allocation performs is made on behalf of.

    The real factory still runs, so the caller under test behaves exactly as
    it would unwatched; only the entry it was handed is captured.
    """
    read_for: list[ConfigEntry | None] = []

    def _spy(hass, dev_reg, ent_reg, config_entry, lock_entity_id):
        read_for.append(config_entry)
        return build_lock_instance(hass, dev_reg, ent_reg, config_entry, lock_entity_id)

    with patch(
        "custom_components.lock_code_manager.domain.allocation.build_lock_instance",
        _spy,
    ):
        yield read_for


async def async_discover_unclaimed_mqtt_lock(
    hass: HomeAssistant, suffix: str = ""
) -> er.RegistryEntry:
    """
    Discover an mqtt lock whose device identifier no provider recognizes.

    Going through real discovery is what puts the bridge's identifier on the
    device registry entry -- the very field dispatch reads -- so a hand-built
    registry row would be testing this test's idea of the payload.

    ``suffix`` distinguishes a second such lock from the first, for the tests
    that need to tell one the entry already holds from one being added now.
    """
    identifier = f"{UNCLAIMED_IDENTIFIER}{suffix}"
    unique_id = f"{identifier}_lock"
    async_fire_mqtt_message(
        hass,
        f"homeassistant/lock/{identifier}/lock/config",
        json.dumps(
            {
                "name": None,
                "command_topic": f"somebridge/lock1{suffix}/set",
                "state_topic": f"somebridge/lock1{suffix}",
                "unique_id": unique_id,
                "device": {
                    "identifiers": [identifier],
                    "name": f"Unclaimed Bridge Lock{suffix}",
                },
            }
        ),
    )
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("lock", "mqtt", unique_id)
    assert entity_id is not None, "discovery did not create the lock entity"
    lock_entry = ent_reg.async_get(entity_id)
    assert lock_entry is not None
    return lock_entry


def code_entity_id(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    lock_entity_id: str = LOCK_1_ENTITY_ID,
) -> str:
    """Return the code sensor for one slot on one lock."""
    return _per_lock_entity_id(
        hass, SENSOR_DOMAIN, config_entry, slot_num, ATTR_CODE, lock_entity_id
    )


def in_sync_entity_id(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    lock_entity_id: str = LOCK_1_ENTITY_ID,
) -> str:
    """
    Return the in-sync entity for one slot on one lock.

    Looked up by unique ID rather than spelled out. The entity ID is derived
    from the config entry's title and the lock's name, so writing it into a
    test ties that test to both -- and a test that renames either then looks
    for an entity that was never going to exist.
    """
    return _per_lock_entity_id(
        hass, BINARY_SENSOR_DOMAIN, config_entry, slot_num, ATTR_IN_SYNC, lock_entity_id
    )


def _per_lock_entity_id(
    hass: HomeAssistant,
    platform: str,
    config_entry: ConfigEntry,
    slot_num: int,
    key: str,
    lock_entity_id: str,
) -> str:
    """Resolve a per-lock entity by unique ID."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform,
        DOMAIN,
        build_slot_unique_id(config_entry.entry_id, slot_num, key, lock_entity_id),
    )
    assert entity_id, f"No {key} entity for slot {slot_num} on {lock_entity_id}"
    return entity_id


def slot_entity_id(
    hass: HomeAssistant,
    platform: str,
    config_entry: ConfigEntry,
    slot_num: int,
    key: str,
) -> str:
    """
    Resolve a slot's entity by unique ID.

    Spelling the entity ID out ties the test to the name of whoever holds the
    slot, since that is what the device -- and so the slug -- is named after.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, build_slot_unique_id(config_entry.entry_id, slot_num, key)
    )
    assert entity_id, f"No {key} entity for slot {slot_num}"
    return entity_id


# The integration the mock lock entities belong to.
LOCK_DEVICE_DOMAIN = "test"

SLOT_1_ACTIVE_ENTITY = "binary_sensor.mock_title_test1_active"
SLOT_1_ENABLED_ENTITY = "switch.mock_title_test1_enabled"
SLOT_1_EVENT_ENTITY = "event.mock_title_test1_credential_used"
SLOT_1_NAME_ENTITY = "text.mock_title_test1_name"
SLOT_1_PIN_ENTITY = "text.mock_title_test1_pin"
SLOT_1_IN_SYNC_ENTITY = "binary_sensor.mock_title_test1_test_1_in_sync"

SLOT_2_ENABLED_ENTITY = "switch.mock_title_test2_enabled"
SLOT_2_ACTIVE_ENTITY = "binary_sensor.mock_title_test2_active"
SLOT_2_EVENT_ENTITY = "event.mock_title_test2_credential_used"
SLOT_2_PIN_ENTITY = "text.mock_title_test2_pin"
SLOT_2_NAME_ENTITY = "text.mock_title_test2_name"
SLOT_2_IN_SYNC_ENTITY = "binary_sensor.mock_title_test2_test_1_in_sync"


@dataclass(repr=False, eq=False)
class MockLCMLock(BaseLock):
    """Mock Lock Code Manager lock instance."""

    def __init__(self, *args, **kwargs):
        """Initialize mock lock."""
        super().__init__(*args, **kwargs)
        self._connected = True
        self._device_available = True
        self._hard_refresh_interval: timedelta | None = None
        self.codes: dict[int, str] = {1: "1234", 2: "5678"}
        # Slots this lock reports as occupied without giving up the value,
        # the way a lock with masked Personal Identification Numbers does.
        # Reported regardless of ``codes``, so a slot keeps reading occupied
        # after a clear -- which is the state a clear can never confirm.
        self.write_only: set[int] = set()
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

    def set_device_available(self, available: bool) -> None:
        """Set device (node) availability for testing."""
        self._device_available = available

    async def async_is_integration_connected(self) -> bool:
        """Return whether the integration's client/driver/broker is connected."""
        return self._connected

    async def async_is_device_available(self) -> bool:
        """Return whether the physical device (node) is reachable."""
        return self._device_available

    async def async_unload(self, remove_permanently: bool) -> None:
        """Record the teardown so tests can assert on remove_permanently."""
        self.service_calls["unload"].append((remove_permanently,))
        await super().async_unload(remove_permanently)

    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Perform hard refresh; records the scope it was asked for."""
        self.service_calls["hard_refresh_codes"].append((slots,))
        return await self.async_get_usercodes()

    async def async_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> WriteResult:
        """
        Set a usercode on a code slot.

        Returns CONFIRMED if the value was changed, NO_CHANGE if already set.
        """
        if self.codes.get(code_slot) == usercode:
            return WriteResult.NO_CHANGE
        self.codes[code_slot] = usercode
        self.service_calls["set_usercode"].append((code_slot, usercode, name))
        return WriteResult.CONFIRMED

    async def async_clear_usercode(
        self, code_slot: int, *, adopt_untagged: bool = True
    ) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.

        ``adopt_untagged`` is accepted and ignored: this lock is slot-only, so
        it has no users to adopt. Matching the base signature is what lets the
        release path pass it without special-casing the double.
        """
        if code_slot not in self.codes:
            return False
        self.codes.pop(code_slot, None)
        self.service_calls["clear_usercode"].append((code_slot,))
        return True

    async def async_get_usercodes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Return dictionary of code slots and usercodes."""
        snapshot = self.codes.copy()
        self.service_calls["get_usercodes"].append(snapshot)
        codes = {slot: SlotCredential.known(pin) for slot, pin in snapshot.items()}
        codes.update({slot: SlotCredential.unreadable() for slot in self.write_only})
        # Mirrors the base projection, including the part that matters: a slot
        # in the scope -- the managed slots, when the caller names none -- that
        # holds nothing is empty, and a slot the lock holds OUTSIDE the scope
        # is still reported. Answering with exactly the scope would model the
        # one shape where a caller's own bounds check is a no-op; omitting an
        # empty managed slot would hide the read the pending-write machinery
        # waits on.
        scope = self.managed_slots if slots is None else slots
        return {**dict.fromkeys(scope, SlotCredential.empty()), **codes}


@dataclass(repr=False, eq=False)
class MockLCMPushLock(MockLCMLock):
    """Mock lock that supports push-based updates."""

    async def async_set_usercode(
        self, code_slot: int, usercode: str, *args: Any, **kwargs: Any
    ) -> WriteResult:
        """Set a code and push it, as every push provider does before CONFIRMED."""
        result = await super().async_set_usercode(code_slot, usercode, *args, **kwargs)
        if result is WriteResult.CONFIRMED:
            self._push_credential_update(code_slot, SlotCredential.known(usercode))
        return result

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
        # The mock lock's device belongs to the mock lock's integration, not
        # to Lock Code Manager. Using Lock Code Manager's domain here would
        # make the device read as one of ours, and the sweep that moves
        # entities off other integrations' devices would never be exercised
        # against the shape it exists for.
        self._attr_device_info = DeviceInfo(
            identifiers={(LOCK_DEVICE_DOMAIN, f"lock.{slugify(name)}")}, name=name
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


async def async_configure_flow(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any] | None = None
) -> Any:
    """
    Submit to a config flow, waiting out any progress step it shows.

    Allocation runs as a progress task (#1536): the step that takes the
    submission shows progress, Home Assistant re-enters it when the task is
    done, and the next result is what the user would see.
    """
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    while result["type"] == FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


async def async_configure_options(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any] | None = None
) -> Any:
    """Submit to an options flow, waiting out any progress step it shows."""
    result = await hass.config_entries.options.async_configure(flow_id, user_input)
    while result["type"] == FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(flow_id)
    return result
