"""Abstract base for lock providers.

Handles rate limiting, connection checking, and coordinator refresh after operations.
See ARCHITECTURE.md for the provider interface contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any, Literal, NoReturn, final

from homeassistant.components.lock import LockState
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, ATTR_STATE, CONF_NAME
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
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
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from ..coordinator import LockUsercodeUpdateCoordinator
from ..data import build_slot_unique_id, find_entry_for_lock_slot
from ..exceptions import (
    DuplicateCodeError,
    LockDisconnected,
    ProviderNotImplementedError,
)
from ..models import SlotCode
from ..util import mask_pin
from .const import LOGGER

_LOGGER = logging.getLogger(__name__)

MIN_OPERATION_DELAY = 2.0
_OPERATION_MESSAGES: dict[Literal["get", "set", "clear", "refresh"], str] = {
    "get": "get from",
    "set": "set on",
    "clear": "clear on",
    "refresh": "hard refresh",
}


def _serialize_source_data(
    source_data: Event | State | dict[str, Any] | None,
) -> tuple[Literal["event", "state"] | None, dict[str, Any] | None]:
    """Serialize an Event, State, or dict into notification_source and extra_data."""
    if isinstance(source_data, Event):
        return "event", {
            "event_type": source_data.event_type,
            "data": source_data.data,
            "time_fired": source_data.time_fired.isoformat(),
        }
    if isinstance(source_data, State):
        last_changed_isoformat = source_data.last_changed.isoformat()
        if source_data.last_changed == source_data.last_updated:
            last_updated_isoformat = last_changed_isoformat
        else:
            last_updated_isoformat = source_data.last_updated.isoformat()
        return "state", {
            "entity_id": source_data.entity_id,
            "state": source_data.state,
            "attributes": source_data.attributes,
            "last_changed": last_changed_isoformat,
            "last_updated": last_updated_isoformat,
        }
    if isinstance(source_data, dict):
        return None, source_data
    return None, None


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
       - Periodic async_internal_is_integration_connected() at connection_check_interval
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
    Provider implementations should raise LockCodeManagerProviderError (or one of
    its subclasses: LockDisconnected, CodeRejectedError/DuplicateCodeError,
    ProviderNotImplementedError) for any failure originating from the lock or
    its integration. The coordinator catches LockCodeManagerError (the broader
    parent) and handles it appropriately (e.g., retrying, logging).

    Do NOT raise generic exceptions, HomeAssistantError, or the bare
    LockCodeManagerError directly — always use LockCodeManagerProviderError
    or a subclass so callers can distinguish provider failures from
    LCM-internal exceptions.
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
    _setup_complete: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _setup_succeeded: bool = field(default=False, init=False)
    _setup_running: bool = field(default=False, init=False)
    _lcm_config_entry: ConfigEntry | None = field(default=None, init=False)
    _rejected_code_slots: set[int] = field(default_factory=set, init=False)

    @final
    @callback
    def mark_code_rejected(self, code_slot: int) -> None:
        """Mark a slot as having its code rejected so the next set attempt raises DuplicateCodeError."""
        self._rejected_code_slots.add(code_slot)

    @final
    async def _execute_rate_limited(
        self,
        operation_type: Literal["get", "set", "clear", "refresh"],
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        pre_execute: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute operation with connection check, serialization, and delay.

        pre_execute runs inside the lock before the operation, for checks
        that must be atomic with the operation (e.g., duplicate detection).
        """
        if not await self.async_internal_is_integration_connected():
            raise LockDisconnected(
                f"Cannot {_OPERATION_MESSAGES[operation_type]} {self.lock.entity_id} - integration not connected"
            )

        if not await self.async_is_device_available():
            raise LockDisconnected(
                f"Cannot {_OPERATION_MESSAGES[operation_type]} {self.lock.entity_id} - device not available"
            )

        async with self._aio_lock:
            if pre_execute:
                pre_execute()

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
            _LOGGER.warning(
                "Lock %s does not have a device ID; push updates and "
                "event subscriptions will be unavailable. "
                "platform=%s, config_entry_id=%s, unique_id=%s",
                self.lock.entity_id,
                self.lock.platform,
                self.lock.config_entry_id,
                self.lock.unique_id,
            )
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
    def display_name(self) -> str:
        """Return a human-readable name for this lock."""
        return self.lock.name or self.lock.original_name or self.lock.entity_id

    def mask_pin(self, pin: str | None) -> str:
        """Return a masked representation of a PIN for logging."""
        return mask_pin(
            pin,
            self.lock.entity_id,
            self.hass.data.get(DOMAIN, {}).get("instance_id", ""),
        )

    @staticmethod
    def is_masked_or_empty(code: str | SlotCode | None) -> bool:
        """Return whether a code is masked or empty (not comparable)."""
        if code is None or code is SlotCode.EMPTY or code is SlotCode.UNREADABLE_CODE:
            return True
        code_str = str(code)
        return code_str == "*" * len(code_str)

    @final
    def is_slot_managed(self, code_slot: int) -> bool:
        """Return whether a code slot is managed by any LCM config entry for this lock."""
        return (
            find_entry_for_lock_slot(self.hass, self.lock.entity_id, code_slot)
            is not None
        )

    @final
    def _check_duplicate_code(self, code_slot: int, usercode: str) -> None:
        """Raise DuplicateCodeError if the PIN duplicates another slot on this lock."""
        # Return early if there's nothing to check
        if not usercode or not self.coordinator or not self.coordinator.data:
            return
        try:
            other_code_slot = next(
                other_code_slot
                for other_code_slot, other_usercode in self.coordinator.data.items()
                if other_code_slot != code_slot
                and not self.is_masked_or_empty(other_usercode)
                and str(other_usercode) == usercode
            )
        except StopIteration:
            pass
        else:
            raise DuplicateCodeError(
                code_slot=code_slot,
                conflicting_slot=other_code_slot,
                conflicting_slot_managed=self.is_slot_managed(other_code_slot),
                lock_entity_id=self.lock.entity_id,
            )

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

    @final
    @callback
    def subscribe_push_updates(self) -> None:
        """Subscribe to push-based value updates.

        Idempotent: safe to call when already subscribed (delegates to
        ``setup_push_subscription`` which must be idempotent).

        On failure, logs and returns — no automatic retry. The existing
        reconnect paths (state listener, connection transition handler)
        will call this again when the integration comes back online.
        """
        try:
            self.setup_push_subscription()
        except ProviderNotImplementedError:
            raise
        except Exception as err:  # noqa: BLE001
            LOGGER.debug(
                "Lock %s: push subscription failed: %s",
                self.lock.entity_id,
                err,
            )

    @callback
    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates.

        Override in subclasses that support push. Raise on failure;
        the caller will log and retry on the next reconnect event.

        Implementations MUST be idempotent (no-op if already subscribed).
        """
        self._raise_not_implemented(
            "setup_push_subscription",
            "Override this method to subscribe to real-time value updates "
            "and call coordinator.push_update({slot: value}) when updates arrive. "
            "Must be idempotent (no-op if already subscribed). "
            "Raise on failure.",
        )

    @final
    @callback
    def unsubscribe_push_updates(self) -> None:
        """Unsubscribe from push-based value updates."""
        self.teardown_push_subscription()

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from push-based value updates.

        Override in subclasses that support push.
        Implementations MUST be idempotent (no-op if already unsubscribed).
        """
        self._raise_not_implemented(
            "teardown_push_subscription",
            "Override this method to clean up any subscriptions "
            "created in setup_push_subscription().",
        )

    @final
    async def async_setup_internal(self, config_entry: ConfigEntry) -> None:
        """
        Set up lock and coordinator, signaling completion to waiters.

        Provider ``async_setup()`` runs first so providers can initialize
        any state the coordinator needs during its first refresh.
        """
        self._lcm_config_entry = config_entry
        try:
            await self.async_setup(config_entry)
        except LockDisconnected as err:
            LOGGER.warning(
                "Provider setup failed for %s: %s. Coordinator will be "
                "created but data will be unavailable until the lock "
                "comes online. Setup will be retried when the lock "
                "integration reconnects.",
                self.lock.entity_id,
                err,
            )
        else:
            self._setup_succeeded = True

        try:
            await self._async_setup_internal(config_entry)
        finally:
            self._setup_complete.set()

    async def _async_on_integration_loaded(self) -> None:
        """Handle provider integration LOADED transition.

        Re-runs ``async_setup`` to re-initialize provider state (e.g.
        re-register event listeners after an integration reload), then
        refreshes the coordinator and subscribes to push updates.

        Operations are chained sequentially so setup completes before
        the coordinator refresh or push subscription begins.
        """
        if (
            self._lcm_config_entry is None
            or self._setup_running
            or not self.lock_config_entry
            or self.lock_config_entry.state != ConfigEntryState.LOADED
        ):
            return

        self._setup_running = True
        try:
            await self.async_setup(self._lcm_config_entry)
        except LockDisconnected:
            LOGGER.debug(
                "Provider setup failed for %s, will retry on next reconnect",
                self.lock.entity_id,
                exc_info=True,
            )
        else:
            if not self._setup_succeeded:
                LOGGER.info(
                    "Provider setup succeeded for %s",
                    self.lock.entity_id,
                )
            self._setup_succeeded = True
        finally:
            self._setup_running = False

        if self.coordinator:
            await self.coordinator.async_request_refresh()
        if self.supports_push:
            self.subscribe_push_updates()

    @final
    async def _async_setup_internal(self, config_entry: ConfigEntry) -> None:
        """Set up lock and coordinator."""
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

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Set up lock by provider.

        Default is a no-op; providers override for provider-specific setup
        (e.g. registering event listeners, validating capabilities).

        Implementations MUST be idempotent — this is called on initial load
        and again on every provider integration reconnect. Clean up any
        previous state before re-initializing.
        """

    @final
    async def async_wait_for_setup(self) -> None:
        """Wait until async_setup has completed."""
        await self._setup_complete.wait()

    async def async_unload(self, remove_permanently: bool) -> None:
        """Tear down config-entry-state listener and push subscription."""
        if self._config_entry_state_unsub:
            self._config_entry_state_unsub()
            self._config_entry_state_unsub = None

        # Unsubscribe from push updates before unloading
        if self.supports_push:
            self.unsubscribe_push_updates()

    async def async_is_device_available(self) -> bool:
        """Return whether the physical device is available for commands."""
        return True

    @final
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
                self.hass.async_create_task(
                    self._async_on_integration_loaded(),
                    f"Provider reconnect for {self.lock.entity_id}",
                )
            elif (
                self.supports_push and self._last_entry_state == ConfigEntryState.LOADED
            ):
                self.unsubscribe_push_updates()

            self._last_entry_state = to_state

        self._config_entry_state_unsub = lock_entry.async_on_state_change(
            _handle_state_change
        )

    async def async_is_integration_connected(self) -> bool:
        """Return True iff the lock's parent config entry is loaded.

        Providers override for integration-specific connection signals.
        """
        if not self.lock_config_entry:
            return False
        return self.lock_config_entry.state == ConfigEntryState.LOADED

    @final
    async def async_internal_is_integration_connected(self) -> bool:
        """Return whether the integration's client/driver/broker is connected."""
        is_up = await self.async_is_integration_connected()
        self._handle_connection_transition(is_up)
        self._last_connection_up = is_up
        return is_up

    @final
    @callback
    def _handle_connection_transition(self, is_up: bool) -> None:
        """Handle push subscribe/unsubscribe on connection state transitions."""
        lock_entry = self.lock_config_entry
        if not self.supports_push or not lock_entry:
            return
        # Skip during SETUP_IN_PROGRESS: the setup path handles the initial
        # subscription, and a parallel subscribe here would race with
        # coordinator creation in _async_setup_internal.
        if lock_entry.state != ConfigEntryState.LOADED:
            return
        if self._last_connection_up is False and is_up:
            if self.coordinator:
                self.hass.async_create_task(
                    self.coordinator.async_request_refresh(),
                    f"Refresh coordinator for {self.lock.entity_id} after reconnect",
                )
            self.subscribe_push_updates()
        elif self._last_connection_up is True and not is_up:
            self.unsubscribe_push_updates()

    async def async_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Re-fetch all codes from the lock and return them in the same shape as async_get_usercodes()."""
        self._raise_not_implemented(
            "async_hard_refresh_codes",
            "Override this method to re-fetch codes from the lock device.",
        )

    @final
    async def async_internal_hard_refresh_codes(self) -> dict[int, str | SlotCode]:
        """Rate-limited wrapper around async_hard_refresh_codes()."""
        return await self._execute_rate_limited(
            "refresh", self.async_hard_refresh_codes
        )

    async def async_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> bool:
        """Set a usercode on a code slot.

        Returns True if the value was changed, False if already set to this
        value. If the provider cannot determine whether a change occurred,
        return True so the coordinator refreshes and verifies the state.

        ``source`` indicates whether the call originates from the sync
        manager ("sync") or a user action ("direct").
        """
        self._raise_not_implemented(
            "async_set_usercode",
            "Override this method to set a usercode on the lock.",
        )

    @final
    async def async_internal_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> None:
        """Set a usercode on a code slot."""
        LOGGER.debug(
            "Setting usercode on %s slot %s (pin=%s, source=%s)",
            self.lock.entity_id,
            code_slot,
            self.mask_pin(usercode),
            source,
        )

        def _pre_execute_checks() -> None:
            """Run pre-execution checks atomically inside the operation lock.

            Checks for duplicate PINs (from coordinator data) and for codes
            previously rejected by the lock firmware (from event 15).
            """
            # Clear the firmware-rejection flag first so it doesn't persist
            # if _check_duplicate_code raises its own DuplicateCodeError
            firmware_rejected = code_slot in self._rejected_code_slots
            self._rejected_code_slots.discard(code_slot)
            self._check_duplicate_code(code_slot, str(usercode))
            if firmware_rejected:
                raise DuplicateCodeError(
                    code_slot=code_slot,
                    lock_entity_id=self.lock.entity_id,
                )

        changed = await self._execute_rate_limited(
            "set",
            self.async_set_usercode,
            code_slot,
            usercode,
            pre_execute=_pre_execute_checks,
            name=name,
            source=source,
        )
        # Refresh coordinator to update entity states from cache (only if changed).
        # Skip for push-based providers — they update the coordinator optimistically
        # via push_update() in their set/clear methods, and refreshing from cache
        # could overwrite the optimistic update with stale data when the underlying
        # driver defers cache updates until device confirmation.
        if changed and self.coordinator and not self.supports_push:
            await self.coordinator.async_request_refresh()

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        If the provider cannot determine whether a change occurred, return
        True so the coordinator refreshes and verifies the state.
        """
        self._raise_not_implemented(
            "async_clear_usercode",
            "Override this method to clear a usercode from the lock.",
        )

    @final
    async def async_internal_clear_usercode(
        self,
        code_slot: int,
        source: Literal["sync", "direct"] = "direct",
    ) -> None:
        """Clear a usercode on a code slot."""
        LOGGER.debug(
            "Clearing usercode on %s slot %s (source=%s)",
            self.lock.entity_id,
            code_slot,
            source,
        )
        changed = await self._execute_rate_limited(
            "clear", self.async_clear_usercode, code_slot
        )
        # Push-based providers handle this via push_update(); see async_internal_set_usercode.
        if changed and self.coordinator and not self.supports_push:
            await self.coordinator.async_request_refresh()

    async def async_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Return a dict of {slot_num: usercode_or_SlotCode_sentinel} for the data coordinator."""
        self._raise_not_implemented(
            "async_get_usercodes",
            "Override this method to retrieve usercodes from the lock.",
        )

    @final
    async def async_internal_get_usercodes(self) -> dict[int, str | SlotCode]:
        """Rate-limited wrapper around async_get_usercodes().

        Slot keys are int; values are usercode strings or SlotCode sentinels.
        """
        return await self._execute_rate_limited("get", self.async_get_usercodes)

    @final
    async def async_call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        blocking: bool = True,
        return_response: bool = False,
    ) -> dict[str, Any] | None:
        """Call a hass service and re-raise failures as LockDisconnected.

        When ``return_response=True``, returns the service response (as a
        dict) so callers don't have to write their own service-call wrapper
        just to access response data. ``target`` mirrors HA's standard
        target dict for platform-aware services.
        """
        try:
            return await self.hass.services.async_call(
                domain,
                service,
                service_data=service_data,
                target=target,
                blocking=blocking,
                return_response=return_response,
            )
        except HomeAssistantError as err:
            # ServiceValidationError is a subclass of HomeAssistantError so
            # it's covered here. CancelledError and programming bugs (TypeError,
            # KeyError) deliberately propagate.
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

        if code_slot is not None and (
            config_entry := find_entry_for_lock_slot(
                self.hass, lock_entity_id, int(code_slot)
            )
        ):
            config_entry_id = config_entry.entry_id
            name_entity_id = self.ent_reg.async_get_entity_id(
                TEXT_DOMAIN,
                DOMAIN,
                build_slot_unique_id(config_entry_id, int(code_slot), CONF_NAME),
            )
            if name_entity_id:
                name_state = self.hass.states.get(name_entity_id)

        from_state: str | None = None
        to_state: str | None = None
        if to_locked:
            from_state = LockState.UNLOCKED
            to_state = LockState.LOCKED
        elif to_locked is False:
            from_state = LockState.LOCKED
            to_state = LockState.UNLOCKED

        notification_source, extra_data = _serialize_source_data(source_data)

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
