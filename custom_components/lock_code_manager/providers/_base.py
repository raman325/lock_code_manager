"""Base integration module."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
import functools
import time
from typing import Any, Literal, NoReturn, final

from homeassistant.components.lock import LockState
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_STATE, CONF_NAME
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_EXTRA_DATA,
    ATTR_FROM,
    ATTR_LCM_CONFIG_ENTRY_ID,
    ATTR_LOCK_CONFIG_ENTRY_ID,
    ATTR_NOTIFICATION_SOURCE,
    ATTR_TO,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from ..coordinator import LockUsercodeUpdateCoordinator
from ..data import get_entry_data
from ..exceptions import LockDisconnected, ProviderNotImplementedError
from .const import LOGGER

MIN_OPERATION_DELAY = 2.0
_OPERATION_MESSAGES: dict[Literal["get", "set", "clear", "refresh"], str] = {
    "get": "get from",
    "set": "set on",
    "clear": "clear on",
    "refresh": "hard refresh",
}


@dataclass(repr=False, eq=False)
class BaseLock:
    """
    Base class for lock provider implementations.

    Data Fetching Modes
    -------------------
    The coordinator supports three update modes. All modes include an initial poll.

    1. Poll for updates (default):
       - Periodic calls to get_usercodes() at usercode_scan_interval
       - Used when supports_push = False
       - Suitable for integrations without real-time events

    2. Push for updates:
       - Real-time value updates via subscribe_push_updates()
       - Enabled when supports_push = True
       - Disables periodic polling (poll for updates)
       - Updates pushed via coordinator.push_update({slot: value})

    3. Poll for drift:
       - Periodic hard_refresh_codes() at hard_refresh_interval
       - Detects out-of-band changes (e.g., codes changed at keypad)
       - Runs regardless of push/poll mode
       - Set hard_refresh_interval = None to disable

    4. Poll connection state:
       - Periodic async_internal_is_connection_up() at connection_check_interval
       - Helps detect reconnects for integrations without config entry state signals
       - Set connection_check_interval = None to disable

    Configuration Examples
    ----------------------
    Poll-only (default):
        supports_push = False
        usercode_scan_interval = timedelta(minutes=1)
        hard_refresh_interval = None
        connection_check_interval = timedelta(seconds=30)

    Push with drift detection (recommended for Z-Wave JS):
        supports_push = True
        hard_refresh_interval = timedelta(hours=1)
        connection_check_interval = None
        # Override subscribe_push_updates() to handle value events

    Poll with drift detection:
        supports_push = False
        usercode_scan_interval = timedelta(minutes=1)
        hard_refresh_interval = timedelta(hours=1)
        connection_check_interval = timedelta(seconds=30)

    Exception Handling
    ------------------
    Provider implementations should raise LockCodeManagerError (or subclasses like
    LockDisconnected) for lock communication failures. The coordinator catches
    LockCodeManagerError and handles it appropriately (e.g., retrying, logging).

    Do NOT raise generic exceptions or HomeAssistantError directly - always use
    LCM-derived exceptions so the coordinator can distinguish lock failures from
    other errors.
    """

    hass: HomeAssistant = field(repr=False)
    dev_reg: dr.DeviceRegistry = field(repr=False)
    ent_reg: er.EntityRegistry = field(repr=False)
    lock_config_entry: ConfigEntry | None = field(repr=False)
    lock: er.RegistryEntry
    device_entry: dr.DeviceEntry | None = field(default=None, init=False)
    coordinator: LockUsercodeUpdateCoordinator | None = field(default=None, init=False)
    _aio_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _last_operation_time: float = field(default=0.0, init=False)
    _min_operation_delay: float = field(default=MIN_OPERATION_DELAY, init=False)
    _last_connection_up: bool | None = field(default=None, init=False)
    _config_entry_state_unsub: Callable[[], None] | None = field(
        default=None, init=False
    )
    _last_entry_state: ConfigEntryState | None = field(default=None, init=False)

    async def _async_executor_call(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run a sync method in the executor."""
        if kwargs:
            return await self.hass.async_add_executor_job(
                functools.partial(func, *args, **kwargs)
            )
        return await self.hass.async_add_executor_job(func, *args)

    async def _execute_rate_limited(
        self,
        operation_type: Literal["get", "set", "clear", "refresh"],
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Execute operation with connection check, serialization, and delay."""
        if not await self.async_internal_is_connection_up():
            raise LockDisconnected(
                f"Cannot {_OPERATION_MESSAGES[operation_type]} {self.lock.entity_id} - lock not connected"
            )

        async with self._aio_lock:
            elapsed = time.monotonic() - self._last_operation_time
            if elapsed < self._min_operation_delay:
                delay = self._min_operation_delay - elapsed
                LOGGER.debug(
                    "Rate limiting %s operation on %s, waiting %.1f seconds",
                    operation_type,
                    self.lock.entity_id,
                    delay,
                )
                await asyncio.sleep(delay)

            LOGGER.debug(
                "Executing %s operation on %s",
                operation_type,
                self.lock.entity_id,
            )

            result = await func(*args, **kwargs)
            self._last_operation_time = time.monotonic()
            return result

    @final
    @callback
    def __post_init__(self) -> None:
        """Post initialization."""
        if not (device_id := self.lock.device_id):
            return
        self.device_entry = self.dev_reg.async_get(device_id)

    @final
    def __repr__(self) -> str:
        """Return string representation of self."""
        return f"{self.__class__.__name__}(domain={self.domain}, lock={self.lock.entity_id})"

    @final
    def __hash__(self) -> int:
        """Return hash of self."""
        return hash(self.lock.entity_id)

    @final
    def __eq__(self, other: Any) -> bool:
        """Return whether self is equal to other."""
        if not isinstance(other, BaseLock):
            return False
        return self.lock.entity_id == other.lock.entity_id

    @final
    def _raise_not_implemented(self, method_name: str, guidance: str = "") -> NoReturn:
        """Raise ProviderNotImplementedError for unimplemented methods."""
        raise ProviderNotImplementedError(self, method_name, guidance)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        raise NotImplementedError()

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=1)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """
        Return interval between hard refreshes.

        Hard refreshes re-fetch all codes from the lock to detect out-of-band changes
        that wouldn't otherwise be detected through normal polling.
        Returns None to disable periodic hard refreshes (default).
        """
        return None

    @property
    def connection_check_interval(self) -> timedelta | None:
        """
        Return interval for connection state checks.

        Defaults to 30 seconds. Returns None to disable periodic checks.
        """
        return timedelta(seconds=30)

    @property
    def supports_push(self) -> bool:
        """
        Return whether this lock supports push-based updates.

        When True, the lock will receive real-time value updates via
        subscribe_push_updates() instead of periodic polling. Polling is
        still used for initial load and drift detection (hard_refresh_interval).
        """
        return False

    @property
    def supports_code_slot_events(self) -> bool:
        """
        Return whether this lock supports code slot events.

        When True, the lock can fire events indicating which code slot was used
        to lock/unlock. This affects the event entity's event_types - locks that
        support this will have their entity_id included in event_types.

        Locks that don't support this will be listed in the unsupported_locks
        attribute on the event entity.
        """
        return True

    @callback
    def subscribe_push_updates(self) -> None:
        """
        Subscribe to push-based value updates.

        Override in subclasses that support push. Called during async_setup()
        when supports_push is True, and retried during drift detection if
        initial setup failed.

        Implementations MUST be idempotent (no-op if already subscribed).
        """
        self._raise_not_implemented(
            "subscribe_push_updates",
            "Override this method to subscribe to real-time value updates "
            "and call coordinator.push_update({slot: value}) when updates arrive. "
            "Must be idempotent (no-op if already subscribed).",
        )

    @callback
    def unsubscribe_push_updates(self) -> None:
        """
        Unsubscribe from push-based value updates.

        Implementations MUST be idempotent (no-op if already unsubscribed).

        Override in subclasses that support push. Called during async_unload()
        when supports_push is True.
        """
        self._raise_not_implemented(
            "unsubscribe_push_updates",
            "Override this method to clean up any subscriptions "
            "created in subscribe_push_updates().",
        )

    def setup(self) -> None:
        """Set up lock."""
        pass

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock and coordinator."""
        await self.hass.async_add_executor_job(self.setup)

        lock_entity_id = self.lock.entity_id
        # Track the provider's config entry (e.g., zwave_js) so we can resubscribe
        # when that integration reloads or reconnects.
        self._setup_config_entry_state_listener()

        # Reuse existing coordinator or create new one
        if self.coordinator is not None:
            self.hass.async_create_task(
                self.coordinator.async_request_refresh(),
                f"Refresh coordinator for {lock_entity_id}",
            )
            return

        self.coordinator = LockUsercodeUpdateCoordinator(self.hass, self, config_entry)
        if config_entry.state == ConfigEntryState.SETUP_IN_PROGRESS:
            try:
                await self.coordinator.async_config_entry_first_refresh()
            except (ConfigEntryNotReady, UpdateFailed) as err:
                LOGGER.warning(
                    "Failed to fetch initial data for lock %s: %s. "
                    "Entities will be created but unavailable until lock is ready.",
                    lock_entity_id,
                    err,
                )
        else:
            await self.coordinator.async_refresh()
            if not self.coordinator.last_update_success:
                LOGGER.warning(
                    "Failed to fetch initial data for lock %s: %s. "
                    "Entities will be created but unavailable until lock is ready.",
                    lock_entity_id,
                    self.coordinator.last_exception,
                )

        # Subscribe to push updates after coordinator is ready. If the provider's
        # config entry isn't loaded yet, defer and let the state listener resubscribe.
        if self.supports_push:
            if (
                self.lock_config_entry
                and self.lock_config_entry.state != ConfigEntryState.LOADED
            ):
                LOGGER.debug(
                    "Lock %s: deferring push subscription until config entry is loaded",
                    lock_entity_id,
                )
            else:
                self.subscribe_push_updates()

    def unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        pass

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        if self._config_entry_state_unsub:
            self._config_entry_state_unsub()
            self._config_entry_state_unsub = None

        # Unsubscribe from push updates before unloading
        if self.supports_push:
            self.unsubscribe_push_updates()

        await self.hass.async_add_executor_job(self.unload, remove_permanently)

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        raise NotImplementedError()

    def _setup_config_entry_state_listener(self) -> None:
        """Listen for provider config entry state changes to resubscribe."""
        lock_entry = self.lock_config_entry
        if not lock_entry or self._config_entry_state_unsub:
            return

        self._last_entry_state = lock_entry.state

        @callback
        def _handle_state_change() -> None:
            to_state = lock_entry.state
            if to_state == self._last_entry_state:
                return

            if to_state == ConfigEntryState.LOADED:
                if self.coordinator:
                    self.hass.async_create_task(
                        self.coordinator.async_request_refresh(),
                        f"Refresh coordinator for {self.lock.entity_id} after reload",
                    )
                if self.supports_push:
                    self.subscribe_push_updates()
            elif (
                self.supports_push and self._last_entry_state == ConfigEntryState.LOADED
            ):
                self.unsubscribe_push_updates()

            self._last_entry_state = to_state

        self._config_entry_state_unsub = lock_entry.async_on_state_change(
            _handle_state_change
        )

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return await self._async_executor_call(self.is_connection_up)

    @final
    async def async_internal_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        is_up = await self.async_is_connection_up()
        lock_entry = self.lock_config_entry
        if self.supports_push and lock_entry:
            # Only react to connection transitions when the config entry is loaded.
            if lock_entry.state == ConfigEntryState.LOADED:
                if self._last_connection_up is False and is_up:
                    if self.coordinator:
                        self.hass.async_create_task(
                            self.coordinator.async_request_refresh(),
                            f"Refresh coordinator for {self.lock.entity_id} after reconnect",
                        )
                    self.subscribe_push_updates()
                elif self._last_connection_up is True and not is_up:
                    self.unsubscribe_push_updates()
        self._last_connection_up = is_up
        return is_up

    def hard_refresh_codes(self) -> dict[int, int | str]:
        """
        Perform hard refresh and return all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock. Returns codes in the same format as get_usercodes().

        Raises:
            LockDisconnected: If the lock cannot be communicated with.

        """
        self._raise_not_implemented(
            "hard_refresh_codes",
            "Override this method to re-fetch codes from the lock device.",
        )

    async def async_hard_refresh_codes(self) -> dict[int, int | str]:
        """
        Perform hard refresh and return all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock. Returns codes in the same format as async_get_usercodes().
        """
        return await self._async_executor_call(self.hard_refresh_codes)

    @final
    async def async_internal_hard_refresh_codes(self) -> dict[int, int | str]:
        """
        Perform hard refresh and return all codes.

        Needed for integrations where usercodes are cached and may get out of sync with
        the lock. Returns codes in the same format as async_internal_get_usercodes().
        """
        return await self._execute_rate_limited(
            "refresh", self.async_hard_refresh_codes
        )

    def set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this value.
        If the provider cannot determine whether a change occurred, return True
        to ensure the coordinator refreshes and verifies the state.

        Raises:
            LockDisconnected: If the lock cannot be communicated with.

        """
        self._raise_not_implemented(
            "set_usercode",
            "Override this method to set a usercode on the lock.",
        )

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """
        Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this value.
        If the provider cannot determine whether a change occurred, return True
        to ensure the coordinator refreshes and verifies the state.
        """
        return await self._async_executor_call(
            self.set_usercode, code_slot, usercode, name=name
        )

    @final
    async def async_internal_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> None:
        """Set a usercode on a code slot."""
        changed = await self._execute_rate_limited(
            "set", self.async_set_usercode, code_slot, usercode, name=name
        )
        # Refresh coordinator to update entity states from cache (only if changed)
        if changed and self.coordinator:
            await self.coordinator.async_request_refresh()

    def clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        If the provider cannot determine whether a change occurred, return True
        to ensure the coordinator refreshes and verifies the state.

        Raises:
            LockDisconnected: If the lock cannot be communicated with.

        """
        self._raise_not_implemented(
            "clear_usercode",
            "Override this method to clear a usercode from the lock.",
        )

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        If the provider cannot determine whether a change occurred, return True
        to ensure the coordinator refreshes and verifies the state.
        """
        return await self._async_executor_call(self.clear_usercode, code_slot)

    @final
    async def async_internal_clear_usercode(self, code_slot: int) -> None:
        """Clear a usercode on a code slot."""
        changed = await self._execute_rate_limited(
            "clear", self.async_clear_usercode, code_slot
        )
        # Refresh coordinator to update entity states from cache (only if changed)
        if changed and self.coordinator:
            await self.coordinator.async_request_refresh()

    def get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }

        Raises:
            LockDisconnected: If the lock cannot be communicated with.

        """
        self._raise_not_implemented(
            "get_usercodes",
            "Override this method to retrieve usercodes from the lock.",
        )

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }

        Raises:
            LockDisconnected: If the lock cannot be communicated with.

        """
        return await self._async_executor_call(self.get_usercodes)

    @final
    async def async_internal_get_usercodes(self) -> dict[int, int | str]:
        """
        Get dictionary of code slots and usercodes.

        Called by data coordinator to get data for code slot sensors.

        Key is code slot, value is usercode, e.g.:
        {
            1: '1234',
            'B': '5678',
        }
        """
        return await self._execute_rate_limited("get", self.async_get_usercodes)

    @final
    def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = True,
    ):
        """Call a hass service and log a failure on an error."""
        try:
            self.hass.services.call(
                domain, service, service_data=service_data, blocking=blocking
            )
        except Exception as err:
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )

    @final
    async def async_call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        blocking: bool = True,
    ):
        """Call a hass service and re-raise failures as LockDisconnected."""
        try:
            await self.hass.services.async_call(
                domain, service, service_data=service_data, blocking=blocking
            )
        except Exception as err:
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )
            raise LockDisconnected(
                f"Service call {domain}.{service} failed: {err}"
            ) from err

    @final
    @callback
    def async_fire_code_slot_event(
        self,
        code_slot: int | None = None,
        to_locked: bool | None = None,
        action_text: str | None = None,
        source_data: Event | State | dict[str, Any] | None = None,
    ) -> None:
        """
        Fire a code slot event.

        Sub-classes should call this whenever a code slot is used. source_data can
        include any data that is JSON serializable if the source is not a Home
        Assistant event or state.
        """
        name_state: State | None = None
        lock_entity_id = self.lock.entity_id
        lock_device_id = self.lock.device_id
        config_entry_id: str | None = None

        try:
            config_entry = next(
                config_entry
                for config_entry in self.hass.config_entries.async_entries(DOMAIN)
                if (
                    self.lock.entity_id in get_entry_data(config_entry, CONF_LOCKS, [])
                    and code_slot is not None
                    and int(code_slot)
                    in (
                        int(slot)
                        for slot in get_entry_data(config_entry, CONF_SLOTS, {})
                    )
                    and (
                        name_entity_id := self.ent_reg.async_get_entity_id(
                            TEXT_DOMAIN,
                            DOMAIN,
                            f"{config_entry.entry_id}|{code_slot}|{CONF_NAME}",
                        )
                    )
                )
            )
        except StopIteration:
            pass
        else:
            config_entry_id = config_entry.entry_id
            name_state = self.hass.states.get(name_entity_id)

        from_state: str | None = None
        to_state: str | None = None
        if to_locked:
            from_state = LockState.UNLOCKED
            to_state = LockState.LOCKED
        elif to_locked is False:
            from_state = LockState.LOCKED
            to_state = LockState.UNLOCKED

        notification_source: Literal["event", "state"] | None = None
        extra_data: dict[str, Any] | None = None
        if isinstance(source_data, Event):
            notification_source = "event"
            extra_data = {
                "event_type": source_data.event_type,
                "data": source_data.data,
                "time_fired": source_data.time_fired.isoformat(),
            }
        elif isinstance(source_data, State):
            notification_source = "state"
            last_changed_isoformat = source_data.last_changed.isoformat()
            if source_data.last_changed == source_data.last_updated:
                last_updated_isoformat = last_changed_isoformat
            else:
                last_updated_isoformat = source_data.last_updated.isoformat()
            extra_data = {
                "entity_id": source_data.entity_id,
                "state": source_data.state,
                "attributes": source_data.attributes,
                "last_changed": last_changed_isoformat,
                "last_updated": last_updated_isoformat,
            }
        elif isinstance(source_data, dict):
            extra_data = source_data

        event_data = {
            ATTR_NOTIFICATION_SOURCE: notification_source,
            ATTR_ENTITY_ID: lock_entity_id,
            ATTR_DEVICE_ID: lock_device_id,
            ATTR_LCM_CONFIG_ENTRY_ID: config_entry_id,
            ATTR_STATE: (
                state.state if (state := self.hass.states.get(lock_entity_id)) else ""
            ),
            ATTR_ACTION_TEXT: action_text,
            ATTR_CODE_SLOT: code_slot or 0,
            ATTR_CODE_SLOT_NAME: name_state.state if name_state else "",
            ATTR_FROM: from_state,
            ATTR_TO: to_state,
            ATTR_EXTRA_DATA: extra_data,
        }

        if self.lock_config_entry:
            event_data[ATTR_LOCK_CONFIG_ENTRY_ID] = self.lock_config_entry.entry_id

        self.hass.bus.async_fire(EVENT_LOCK_STATE_CHANGED, event_data=event_data)
