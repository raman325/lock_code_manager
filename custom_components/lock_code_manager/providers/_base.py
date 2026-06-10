"""
Abstract base for lock providers.

Handles rate limiting, connection checking, and coordinator refresh after operations.
See ARCHITECTURE.md for the provider interface contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
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
from ..domain.config import build_slot_unique_id
from ..domain.coordinator import LockUsercodeUpdateCoordinator
from ..domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    LockCapabilities,
    SetUserResult,
    User,
    credential_from_slot,
    user_from_slot,
)
from ..domain.exceptions import (
    DuplicateCodeError,
    LockCodeManagerError,
    LockDisconnected,
    LockOperationFailed,
    ProviderNotImplementedError,
)
from ..domain.models import SlotCredential
from ..domain.queries import find_entry_for_lock_slot, get_managed_slots
from ..domain.util import mask_pin
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
       - Updates pushed via self._push_credential_update(slot, credential)

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
    LockCodeManagerError for lock/integration failures — use
    LockCodeManagerProviderError or a subclass so callers can distinguish
    provider failures from LCM-internal errors. LockCodeManagerError is
    reserved for programming errors such as a missing required override.
    """

    hass: HomeAssistant = field(repr=False)
    dev_reg: dr.DeviceRegistry = field(repr=False)
    ent_reg: er.EntityRegistry = field(repr=False)
    lock_config_entry: ConfigEntry | None = field(repr=False)
    lock: er.RegistryEntry
    device_entry: dr.DeviceEntry | None = field(default=None, init=False)
    coordinator: LockUsercodeUpdateCoordinator | None = field(default=None, init=False)
    _aio_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # Sequence lock for read-modify-write operations that need atomicity
    # across multiple service calls. Outer lock so each leaf call can
    # still acquire _aio_lock for rate limiting without deadlocking.
    _sequence_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    # Registry of push-subscription unsub callables. Providers append to
    # this list via _register_push_unsub() so the base helper can release
    # everything on teardown without each provider tracking its own field.
    _push_unsubs: list[Callable[[], None]] = field(
        default_factory=list, init=False, repr=False
    )
    # Read via ``_get_cached_capabilities``; cleared by recreating the
    # provider on integration reload.
    _capabilities_cache: LockCapabilities | None = field(
        default=None, init=False, repr=False
    )
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
    # Reconnect task spawned by the config-entry state listener when the lock
    # integration transitions to LOADED. Tracked so async_unload can cancel it
    # before teardown -- otherwise a late reconnect can call
    # coordinator.async_request_refresh() against an already-shutdown
    # coordinator.
    _reconnect_task: asyncio.Task[None] | None = field(default=None, init=False)

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
        """
        Execute operation with connection check, serialization, and delay.

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
    def _serialize_sequence(self) -> contextlib.AbstractAsyncContextManager[None]:
        """
        Return an async context manager that serializes a multi-step operation.

        Use around read-modify-write sequences (e.g. get-codes/delete/add)
        so concurrent callers see each sequence as atomic. Uses a separate
        lock from ``_aio_lock`` so leaf calls inside the sequence can still
        go through ``_execute_rate_limited`` without deadlocking.
        """
        return self._sequence_lock

    @final
    @callback
    def _register_push_unsub(self, unsub: Callable[[], None]) -> None:
        """
        Register a push-subscription unsub for base teardown management.

        Scope is explicitly the push-subscription lifecycle: cluster
        listeners, event subscriptions, and MQTT unsubscribes wired from
        ``subscribe_push_updates`` / ``setup_push_subscription``.
        Listeners with a different lifecycle (Home Assistant event-bus
        listeners tied to setup/unload, like Z-Wave JS's) do NOT belong
        here -- they must be tracked separately.
        """
        self._push_unsubs.append(unsub)

    @final
    @callback
    def _clear_push_unsubs(self) -> None:
        """Release every registered push-subscription unsub, logging individual failures."""
        # Snapshot first: an unsub that re-registers (or otherwise mutates
        # the registry) would otherwise break iteration.
        unsubs = list(self._push_unsubs)
        self._push_unsubs.clear()
        for unsub in unsubs:
            try:
                unsub()
            except Exception as err:
                LOGGER.warning(
                    "Lock %s: push unsubscribe raised, continuing teardown: %s",
                    self.lock.entity_id,
                    err,
                )

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
        """Return a string representation."""
        return f"{self.__class__.__name__}(domain={self.domain}, lock={self.lock.entity_id})"

    @final
    def __hash__(self) -> int:
        """Hash by lock entity ID (one BaseLock instance per physical lock)."""
        return hash(self.lock.entity_id)

    @final
    def __eq__(self, other: Any) -> bool:
        """Two BaseLock instances are equal when they wrap the same lock entity."""
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

    def mask_pin(self, pin: str | None, code_slot: int | str = 0) -> str:
        """Return a masked representation of a PIN for logging."""
        return mask_pin(
            pin,
            code_slot,
            self.hass.data.get(DOMAIN, {}).get("instance_id", ""),
        )

    @final
    @callback
    def _push_credential_update(
        self, code_slot: int, credential: SlotCredential
    ) -> None:
        """Push a coordinator credential update; no-op when no coordinator is attached."""
        if self.coordinator is not None:
            self.coordinator.push_update({code_slot: credential})

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
                for other_code_slot, other_credential in self.coordinator.data.items()
                if other_code_slot != code_slot and other_credential.matches(usercode)
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
    def supports_native_users(self) -> bool:
        """
        Return whether the provider speaks the User->Credential model natively.

        True for providers whose integration manages users and credentials as
        distinct entities (the Z-Wave unified access control surface, the
        Matter DoorLock cluster). The base orchestration then runs the
        user-first lifecycle: create or update the user, then write its
        credential; delete the user when its last credential is removed.

        False (the default) for slot-only providers (zha, zigbee2mqtt,
        schlage, akuvox, virtual): the base skips every user operation and
        addresses the credential by slot, so behavior is identical to the
        legacy one-Personal-Identification-Number-per-slot model.
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
        """
        Subscribe to push-based value updates.

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
        except LockDisconnected as err:
            LOGGER.debug(
                "Lock %s: push subscription deferred (disconnected): %s",
                self.lock.entity_id,
                err,
            )
        except Exception as err:
            LOGGER.warning(
                "Lock %s: push subscription failed unexpectedly: %s",
                self.lock.entity_id,
                err,
            )

    @callback
    def setup_push_subscription(self) -> None:
        """
        Subscribe to push-based value updates.

        Override in subclasses that support push. Raise on failure;
        the caller will log and retry on the next reconnect event.

        Implementations MUST be idempotent (no-op if already subscribed).
        """
        self._raise_not_implemented(
            "setup_push_subscription",
            "Override this method to subscribe to real-time value updates "
            "and call self._push_credential_update(slot, credential) when updates "
            "arrive. Must be idempotent (no-op if already subscribed). "
            "Raise on failure.",
        )

    @final
    @callback
    def unsubscribe_push_updates(self) -> None:
        """Unsubscribe from push-based value updates."""
        with contextlib.suppress(ProviderNotImplementedError):
            self.teardown_push_subscription()

    @callback
    def teardown_push_subscription(self) -> None:
        """
        Unsubscribe from push-based value updates.

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
        except (LockDisconnected, LockOperationFailed) as err:
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
            lock_entity_id = self.lock.entity_id
            # Track the provider's config entry (e.g., zwave_js) so we can resubscribe
            # when that integration reloads or reconnects.
            self._setup_config_entry_state_listener()

            if self.coordinator is not None:
                self.hass.async_create_task(
                    self.coordinator.async_request_refresh(),
                    f"Refresh coordinator for {lock_entity_id}",
                )
            else:
                self.coordinator = LockUsercodeUpdateCoordinator(
                    self.hass, self, config_entry
                )
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
        finally:
            self._setup_complete.set()

    async def _async_on_integration_loaded(self) -> None:
        """
        Handle provider integration LOADED transition.

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
        except LockDisconnected, LockOperationFailed:
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

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """
        Set up lock by provider.

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
        """Tear down config-entry-state listener, reconnect task, and push subscription."""
        if self._config_entry_state_unsub:
            self._config_entry_state_unsub()
            self._config_entry_state_unsub = None

        # Cancel any in-flight reconnect spawned by _handle_state_change so
        # it cannot call coordinator.async_request_refresh() against an
        # already-shutdown coordinator. Await it to confirm the cancellation
        # took effect before we return from unload.
        reconnect_task = self._reconnect_task
        if reconnect_task is not None and not reconnect_task.done():
            reconnect_task.cancel()
            try:
                await reconnect_task
            except asyncio.CancelledError:
                # If our own task is being cancelled, propagate; otherwise
                # the CancelledError is for the reconnect task we just
                # cancelled and is expected.
                current = asyncio.current_task()
                if current is not None and current.cancelling() > 0:
                    raise
            except Exception as err:
                _LOGGER.warning(
                    "Reconnect task raised during teardown of %s: %s",
                    self.lock.entity_id,
                    err,
                    exc_info=err,
                )
        self._reconnect_task = None

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
                # The provider transitioned through LOADED twice in quick
                # succession (e.g. reload during reconnect). Cancel any
                # prior in-flight reconnect; drain any pending exception
                # on a prior task that already completed with an error so
                # we do not leak an unretrieved exception at GC time.
                if self._reconnect_task is not None:
                    self._reconnect_task.add_done_callback(
                        self._drain_superseded_reconnect
                    )
                    if not self._reconnect_task.done():
                        self._reconnect_task.cancel()
                self._reconnect_task = self.hass.async_create_task(
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

    def _drain_superseded_reconnect(self, task: asyncio.Task[None]) -> None:
        """Consume any leftover exception on a superseded reconnect task."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _LOGGER.warning(
                "Superseded reconnect task raised for %s: %s",
                self.lock.entity_id,
                exc,
                exc_info=exc,
            )

    async def async_is_integration_connected(self) -> bool:
        """
        Return True iff the lock's parent config entry is loaded.

        Providers override for integration-specific connection signals.
        Raises ``LockCodeManagerError`` if ``lock_config_entry`` is None —
        providers without a config entry must override this method.
        """
        if not self.lock_config_entry:
            raise LockCodeManagerError(
                f"Lock {self.lock.entity_id} has no lock_config_entry. "
                f"Providers without a config entry must override "
                f"async_is_integration_connected()."
            )
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
        # coordinator creation in async_setup_internal.
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

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """Re-fetch all codes from the lock and return them in the same shape as async_get_usercodes()."""
        self._raise_not_implemented(
            "async_hard_refresh_codes",
            "Override this method to re-fetch codes from the lock device.",
        )

    @final
    async def async_internal_hard_refresh_codes(self) -> dict[int, SlotCredential]:
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
        """
        Set a usercode on a code slot via the User->Credential primitives.

        Projects the slot to a single Personal Identification Number
        credential. Native-user providers run the create-on-first user
        lifecycle via ``_put_credential``; slot-only providers write the
        credential directly, addressing it by slot. Returns True if the value
        changed, False if it was already set to this value -- and True when the
        provider cannot determine whether a change occurred, so the coordinator
        refreshes and verifies the actual state.
        """
        state = SlotCredential.known(usercode)
        credential = credential_from_slot(code_slot, state)
        if not self.supports_native_users:
            return await self.async_set_credential(
                code_slot, credential, name=name, source=source
            )
        return await self._put_credential(
            user_from_slot(code_slot, state, name),
            credential,
            name=name,
            source=source,
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
            self.mask_pin(usercode, code_slot),
            source,
        )

        def _pre_execute_checks() -> None:
            """Check for duplicate PINs and firmware-rejected codes, atomically inside the lock."""
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
        # Skip coordinator refresh for push providers — they update optimistically
        # via push_update(), and refreshing from cache could overwrite with stale
        # data when the driver defers cache updates until device confirmation.
        if changed and self.coordinator and not self.supports_push:
            await self.coordinator.async_request_refresh()

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot via the User->Credential primitives.

        Slot-only providers address the credential by slot and delete it
        directly. Native-user providers must target the credential's actual
        owning user, which is not assumed to equal the slot index: a
        credential may have been created on the lock by another controller, or
        the integration may have allocated a user identifier that differs from
        the slot. The owner is resolved from the lock's current users and the
        delete-on-last user lifecycle runs via ``_drop_credential``. Returns
        True if the value changed, False if it was already cleared -- and True
        when the provider cannot determine whether a change occurred, so the
        coordinator refreshes and verifies the actual state.
        """
        if not self.supports_native_users:
            ref = CredentialRef(
                user_id=code_slot, type=CredentialType.PIN, slot=code_slot
            )
            return await self.async_delete_credential(ref)

        owner = next(
            (
                user
                for user in await self.async_get_users()
                for credential in user.pin_credentials
                if credential.slot == code_slot
            ),
            None,
        )
        if owner is None:
            # No user owns this slot's credential -- nothing to clear.
            return False

        ref = CredentialRef(
            user_id=owner.user_id, type=CredentialType.PIN, slot=code_slot
        )
        return await self._drop_credential(owner, ref)

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
        if changed and self.coordinator and not self.supports_push:
            await self.coordinator.async_request_refresh()

    async def _project_users_to_slots(
        self, credential_type: CredentialType
    ) -> dict[int, SlotCredential]:
        """
        Project the lock's users to a slot -> ``SlotCredential`` map.

        Every managed slot is present even when empty: the projection
        starts from ``managed_slots`` mapped to ``SlotCredential.empty()``
        and then overlays the credentials of ``credential_type`` read via
        ``async_get_users``. This preserves the slot-keyed contract the
        coordinator, sync manager, and slot entities depend on -- a
        managed slot missing from the map is treated as unavailable, not
        empty, so the empty placeholders are load-bearing. Occupied slots
        the lock reports that are not managed are surfaced too.
        Credentials of other types are dropped here -- the seam keeps
        everything below it slot-shaped and single-type this round.

        This is the chokepoint for "the base class filters per credential
        type before passing to the coordinator/entities" (Option A in the
        design discussion). Adding a second supported type means calling
        this helper from a second projection method -- providers store
        every type they can map, so no provider changes are required.

        TODO(option-b): when the integration adds a second supported
        credential type (Z-Wave User Credential CC also exposes
        ``PASSWORD``), revisit whether the coordinator/entities should
        instead be type-scoped from the top -- one set of slot entities
        per credential type -- rather than threading the type through a
        single projection. The provider-side model is already ready for
        that move; the open question is configuration / user experience
        (do users configure "PIN slots 1-10" and "password slots 1-5"
        separately, or is each slot polymorphic?).
        """
        codes = {slot: SlotCredential.empty() for slot in self.managed_slots}
        codes.update(
            {
                credential.slot: credential.state
                for user in await self.async_get_users()
                for credential in user.credentials_of_type(credential_type)
            }
        )
        return codes

    async def async_get_usercodes(self) -> dict[int, SlotCredential]:
        """
        Return the slot -> ``SlotCredential`` map for Personal Identification Numbers.

        Thin Personal-Identification-Number-shaped wrapper over
        ``_project_users_to_slots``; preserved as a stable name because
        the coordinator, sync manager, and slot entities are all
        Personal Identification Number-scoped today.
        """
        return await self._project_users_to_slots(CredentialType.PIN)

    @final
    async def _get_cached_capabilities(self) -> LockCapabilities:
        """
        Return the lock's capabilities, populating the cache on first call.

        Cache lives for the provider instance's lifetime; reload
        recreates the provider and naturally invalidates the cache.
        """
        if self._capabilities_cache is None:
            self._capabilities_cache = await self.async_get_capabilities()
        return self._capabilities_cache

    @final
    async def _put_credential(
        self,
        user: User,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """
        Run the create-on-first user lifecycle around a credential write.

        Native-user only (the slot adapters call the credential primitive
        directly). Rolls back a newly-created user when the credential
        write fails so the lock isn't left with a credential-less user
        the slot-keyed coordinator can't reconcile. Returns True if the
        value changed.
        """
        caps = await self._get_cached_capabilities()
        needs_user_write = (
            caps.supports_user_management and caps.max_user_name_length > 0
        )
        if needs_user_write:
            result = await self.async_set_user(user)
            credential_user_id = result.user_id
            rollback_user_id = result.user_id if result.created else None
        else:
            credential_user_id = user.user_id
            rollback_user_id = None
        try:
            return await self.async_set_credential(
                credential_user_id, credential, name=name, source=source
            )
        except Exception:
            if rollback_user_id is not None:
                try:
                    await self.async_delete_user(rollback_user_id)
                except Exception as rollback_err:
                    LOGGER.warning(
                        "Lock %s: failed to roll back newly created user %s "
                        "after a failed credential write: %s",
                        self.lock.entity_id,
                        rollback_user_id,
                        rollback_err,
                    )
            raise

    @final
    async def _drop_credential(self, owner: User, ref: CredentialRef) -> bool:
        """
        Run the delete-on-last user lifecycle around a credential delete.

        Native-user only. Deletes the credential, then deletes ``owner`` when
        that was its last credential (the invariant: a user exists if and only
        if it owns at least one credential). The count is read from ``owner``,
        a pre-deletion snapshot, so the decision does not depend on whether the
        provider mutates the user during the delete. Returns True if changed.

        The credential delete is the operation that clears the slot, so a
        failure of the follow-up user delete is logged rather than raised: the
        slot is already cleared, and raising would only churn retries that
        re-resolve to no owner. The leftover empty user is surfaced in the log.
        """
        was_only_credential = len(owner.credentials) <= 1
        changed = await self.async_delete_credential(ref)
        if changed and was_only_credential:
            try:
                await self.async_delete_user(owner.user_id)
            except Exception as err:
                LOGGER.warning(
                    "Lock %s: cleared the credential at slot %s but failed to "
                    "remove the now-empty user %s: %s",
                    self.lock.entity_id,
                    ref.slot,
                    owner.user_id,
                    err,
                )
        return changed

    async def async_set_user(self, user: User) -> SetUserResult:
        """
        Create or update a lock user.

        Native-user providers only. Returns a ``SetUserResult`` carrying the
        resolved ``user_id`` (threaded into the following
        ``async_set_credential`` call, so a provider whose integration
        auto-allocates the identifier must return the allocated value) and
        ``created`` -- True when this call added a new user, False when it
        updated an existing one. ``created`` lets the base roll the user back
        if the subsequent credential write fails. The Z-Wave set-credential
        command requires an existing user, which is why the base runs this
        first.

        ``user.name`` of ``None`` means leave the existing name unchanged (not
        clear it), matching how the providers already treat an absent name.
        """
        self._raise_not_implemented(
            "async_set_user",
            "Override on native-user providers to create or update a lock "
            "user and return a SetUserResult(user_id, created).",
        )

    async def async_delete_user(self, user_id: int) -> None:
        """
        Delete a lock user (and, per the Z-Wave/Matter spec, its credentials).

        Native-user providers only. The base calls this once a user's last
        credential has been removed -- the lifecycle invariant is that a user
        exists if and only if it owns at least one credential.
        """
        self._raise_not_implemented(
            "async_delete_user",
            "Override on native-user providers to delete a lock user.",
        )

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """
        Set or update one credential, returning whether the lock changed.

        Every migrated provider implements this. ``user_id`` identifies the
        owning user for native-user providers; slot-only providers ignore it
        and address the credential by ``credential.slot``. Providers raise
        ``DuplicateCodeError`` when the lock rejects the value as a duplicate.
        Native-user providers carry the user's name on the user record, so they
        may treat ``name`` here as advisory; slot-only providers use it (for
        example as a tagged code name). A ``name`` of ``None`` means leave any
        existing name unchanged, never clear it.

        Return True if the value changed, False if it was already set to this
        value. When the provider cannot determine whether a change occurred
        (for example a write-only lock), return True: the returned flag drives
        the coordinator refresh, so reporting True makes it re-read and verify
        rather than leaving stale state.
        """
        self._raise_not_implemented(
            "async_set_credential",
            "Override to write a credential to the lock.",
        )

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ``ref``; return whether it changed.

        Every migrated provider implements this. Slot-only providers use
        ``ref.slot`` and ignore ``ref.user_id``.

        Return True if the credential was removed, False if it was already
        absent. When the provider cannot determine whether a change occurred,
        return True: the returned flag drives the coordinator refresh, so
        reporting True makes it re-read and verify rather than leaving stale
        state.
        """
        self._raise_not_implemented(
            "async_delete_credential",
            "Override to delete a credential from the lock.",
        )

    async def async_get_users(self) -> list[User]:
        """
        Read every user and their credentials from the lock.

        Backs the default ``async_get_usercodes`` projection. Native-user
        providers map their integration's user list; slot-only providers
        project each occupied slot to a single-credential user via
        ``user_from_slot``.
        """
        self._raise_not_implemented(
            "async_get_users",
            "Override to read users and credentials from the lock.",
        )

    async def async_get_capabilities(self) -> LockCapabilities:
        """
        Report the lock's user/credential capabilities.

        Native-user providers must override; the base orchestration reads
        this to decide whether to write a user record before a credential.
        """
        self._raise_not_implemented(
            "async_get_capabilities",
            "Override to report the lock's user/credential capabilities.",
        )

    @final
    async def async_internal_get_usercodes(self) -> dict[int, SlotCredential]:
        """Rate-limited wrapper around async_get_usercodes()."""
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
        """
        Call a hass service and re-raise failures as LockDisconnected.

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
        except OSError as err:
            # OSError covers transient connectivity errors (ReadTimeout,
            # ConnectionError) from integrations that don't wrap them in
            # HomeAssistantError. These mean the lock could not be reached.
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )
            raise LockDisconnected(
                f"Service call {domain}.{service} failed: {err}"
            ) from err
        except HomeAssistantError as err:
            # HomeAssistantError covers ServiceValidationError and HA-wrapped
            # failures. The lock was reachable but the operation was rejected
            # or otherwise failed. CancelledError and programming bugs
            # (TypeError, KeyError) deliberately propagate.
            LOGGER.error(
                "Error calling %s.%s service call: %s", domain, service, str(err)
            )
            raise LockOperationFailed(
                f"Service call {domain}.{service} failed: {err}"
            ) from err

    @final
    @property
    def managed_slots(self) -> set[int]:
        """Return slot numbers managed by any Lock Code Manager config entry that includes this lock."""
        return get_managed_slots(self.hass, self.lock.entity_id)

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
