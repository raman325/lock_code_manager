"""
Abstract base for lock providers.

Handles rate limiting, connection checking, and coordinator refresh after operations.
See ARCHITECTURE.md for the provider interface contract.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection
import contextlib
from dataclasses import dataclass, field, replace
from datetime import timedelta
import logging
from typing import Any, ClassVar, Literal, NoReturn, Protocol, final, override

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import (
    IssueSeverity,
    async_create_issue,
    async_delete_issue,
)

from ..const import (
    DATA,
    DOMAIN,
    LockCodeManagerData,
)
from ..domain.credentials import (
    Credential,
    CredentialAddress,
    CredentialRef,
    CredentialType,
    LockCapabilities,
    SetUserResult,
    User,
    WriteResult,
    credential_from_slot,
    pin_address,
    user_from_slot,
)
from ..domain.events import CredentialOperation, async_fire_credential_used
from ..domain.exceptions import (
    CodeRejectedError,
    LockCodeManagerError,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
    LockOperationUnsupported,
    ProviderNotImplementedError,
)
from ..domain.models import SlotCredential
from ..domain.queries import (
    configured_owner,
    get_entry_config,
    slots_configured_on,
)
from ..domain.util import (
    lock_display_name,
    mask_pin,
    per_lock_issue_id,
)
from ._util import make_tagged_name, parse_tag
from .const import LOGGER

_LOGGER = logging.getLogger(__name__)

MIN_OPERATION_DELAY = 2.0

# How long one device operation may run before the lock is treated as gone.
#
# Chosen to be longer than any legitimate operation rather than to be a tight
# bound. It must not preempt the providers' own recovery: `zwave_js_ui` gives
# an API call 60s and turns its own expiry into `LockDisconnected`, which drops
# the gateway binding and rebinds; ZHA can legitimately spend ~84s on a single
# ZCL command to a battery lock (zigpy forces a 28s reply timeout for end
# devices, inside three attempts). Anything near those misreports a
# healthy-but-slow lock as broken and preempts a real recovery path.
#
# Cancelling is safe at THIS budget and would not be at a shorter one.
# Schlage and Matter both delete a credential
# before writing its replacement, so a cancel mid-sequence can leave a door
# with no working PIN -- but only if the call was going to finish. Ten minutes
# in, the credential is already gone whichever half is stuck, and the wedge,
# not the cancel, is what took it. What the cancel buys back is the lock's
# worker, so the next sync retries the whole sequence. Issue #1523 had three
# PIN changes sit unsynced for seven hours behind one such call.
#
# A FLOOR, not the whole answer. One exchange is one provider call, but on
# several providers one provider call is a sequential
# walk of slots, one exchange with the lock per slot -- so the honest budget
# is per exchange, times the exchanges THAT call walks. Those providers
# declare `per_exchange_budget`, and `_operation_budget` scales this floor by
# the count each call site passes. Ten flaky slots on ZHA legitimately reach
# ~840s; cutting that off would be the misdiagnosis this constant exists to
# avoid, and giving a one-slot write those ten slots' patience is the other
# half of issue #1528.
OPERATION_TIMEOUT = 600.0


class ProviderCoordinator(Protocol):
    """
    What a provider asks of the coordinator it is set up with.

    The core's adapter answers: pushes go to the lock's worker, refresh
    requests become its reads.
    """

    last_update_success: bool
    last_exception: Exception | None

    def push_update(self, updates: dict[int, SlotCredential]) -> None:
        """Hand pushed slots on."""

    def observe_push(
        self, address: CredentialAddress, observed: SlotCredential
    ) -> None:
        """Hand a push about one slot on."""

    def desired_credential(
        self, address: CredentialAddress, *, owner: Any = None
    ) -> SlotCredential:
        """Say whether a PIN is wanted at ``address``."""

    async def async_request_refresh(self) -> None:
        """Read the lock again."""

    async def async_refresh(self) -> None:
        """Take the first read."""

    async def async_in_turn[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Run ``call`` against the lock in its turn; return its result."""

    def async_set_update_error(self, err: Exception) -> None:
        """Note that the first read failed."""

    def note_push_capability(self) -> None:
        """Note that the provider has learned it can push."""


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
       - Periodic async_internal_is_reachable() at connection_check_interval
       - Helps detect reconnects for integrations without config entry state signals
       - Set connection_check_interval = None to disable

    Configuration Examples
    ----------------------
    Poll-only (default):
        supports_push = False
        usercode_scan_interval = timedelta(minutes=1)
        hard_refresh_interval = None
        connection_check_interval = timedelta(seconds=30)

    Push only (e.g. Z-Wave JS), trusting the event stream without a drift poll:
        supports_push = True
        hard_refresh_interval = None
        connection_check_interval = None
        # Override subscribe_push_updates() to handle value events

    Push with drift detection (e.g. Matter):
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
    coordinator: ProviderCoordinator | None = field(default=None, init=False)
    # Sequence lock for a provider's read-modify-write operations that need
    # atomicity across several of its own calls (Schlage, Akuvox).
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
    # The pause the lock needs between exchanges: the worker's gap.
    _min_operation_delay: float = field(default=MIN_OPERATION_DELAY, init=False)
    _last_connection_up: bool | None = field(default=None, init=False)
    _config_entry_state_unsub: Callable[[], None] | None = field(
        default=None, init=False
    )
    _last_entry_state: ConfigEntryState | None = field(default=None, init=False)
    _setup_complete: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _setup_succeeded: bool = field(default=False, init=False)
    _setup_running: bool = field(default=False, init=False)
    # Provider setup was skipped altogether because the integration was not
    # connected yet, and has not run since. Distinct from
    # ``_setup_succeeded``, which is also False for a lock whose setup ran
    # and failed -- that one has a diagnosis, this one has never been asked.
    _setup_deferred: bool = field(default=False, init=False)
    # The last capability probe found no PIN support: the one setup failure
    # that means no code can be on the lock. Kept apart from the setup flags,
    # which read "failed" through every recoverable failure and retry too.
    pin_unsupported: bool = field(default=False, init=False)
    _lcm_config_entry: ConfigEntry | None = field(default=None, init=False)
    # Reconnect task spawned by the config-entry state listener when the lock
    # integration transitions to LOADED. Tracked so async_unload can cancel it
    # before teardown -- otherwise a late reconnect can call
    # coordinator.async_request_refresh() against an already-shutdown
    # coordinator.
    _reconnect_task: asyncio.Task[None] | None = field(default=None, init=False)

    @final
    def _serialize_sequence(self) -> contextlib.AbstractAsyncContextManager[None]:
        """
        Return an async context manager that serializes a multi-step operation.

        Use around read-modify-write sequences (e.g. get-codes/delete/add)
        so concurrent callers see each sequence as atomic.
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
    @override
    def __repr__(self) -> str:
        """Return a string representation."""
        return f"{self.__class__.__name__}(domain={self.domain}, lock={self.lock.entity_id})"

    @final
    @override
    def __hash__(self) -> int:
        """Hash by lock entity ID (one BaseLock instance per physical lock)."""
        return hash(self.lock.entity_id)

    @final
    @override
    def __eq__(self, other: Any) -> bool:
        """Two BaseLock instances are equal when they wrap the same lock entity."""
        if not isinstance(other, BaseLock):
            return False
        return self.lock.entity_id == other.lock.entity_id

    @final
    def _raise_not_implemented(self, method_name: str, guidance: str = "") -> NoReturn:
        """Raise ProviderNotImplementedError for unimplemented methods."""
        raise ProviderNotImplementedError(self, method_name, guidance)

    # What one exchange with the lock may cost on this transport, for providers
    # whose reads walk the slots one exchange at a time. ``None`` for a provider
    # that reads the whole device in one call: its budget is the flat floor.
    # Strictly above any bound the provider puts on the exchange itself, so the
    # provider's own timeout always claims a silent slot first and the outer
    # deadline only ever claims a transport that has stopped answering.
    per_exchange_budget: ClassVar[float | None] = None

    @property
    def operation_timeout_seconds(self) -> float:
        """
        The floor: how long a single exchange may run before the lock is treated as gone.

        Providers do not override this to make a walk fit; they declare
        ``per_exchange_budget`` and every call site says how many exchanges
        it walks (see ``_operation_budget``).
        """
        return OPERATION_TIMEOUT

    @final
    def _operation_budget(self, exchanges: int) -> float:
        """
        Return the deadline for a call that walks ``exchanges`` exchanges.

        The floor, or the declared per-exchange budget times the walk,
        whichever is longer: a lock that answers every command must never be
        cut off partway through a legitimate walk, and a one-slot write must
        not inherit a thirty-slot entry's patience.
        """
        if self.per_exchange_budget is None:
            return self.operation_timeout_seconds
        return max(self.operation_timeout_seconds, self.per_exchange_budget * exchanges)

    @property
    def display_name(self) -> str:
        """Return a human-readable name for this lock."""
        return lock_display_name(self.hass, self.lock.entity_id)

    def mask_pin(self, pin: str | None, code_slot: int | str = 0) -> str:
        """Return a masked representation of a PIN for logging."""
        return mask_pin(
            pin,
            code_slot,
            self.hass.data.get(DATA, LockCodeManagerData()).get("instance_id", ""),
        )

    @final
    @callback
    def _push_credential_update(
        self, code_slot: int, credential: SlotCredential
    ) -> None:
        """Push a coordinator credential update; no-op when no coordinator is attached."""
        if self.coordinator is None:
            return
        self.coordinator.push_update({code_slot: credential})

    @callback
    def _confirm_slot(self, code_slot: int, observed: SlotCredential) -> None:
        """
        Resolve a push event for a slot against any write pending on it.

        The coordinator owns pending writes and their resolution; this is the
        provider-facing name for handing it an observation. No-op without a
        coordinator, when there is nothing to resolve against or update.
        """
        if self.coordinator is None:
            return
        self.coordinator.observe_push(pin_address(code_slot), observed)

    @final
    def _expects_pin(self, code_slot: int) -> bool:
        """
        Return whether Lock Code Manager wants a PIN on ``code_slot``.

        Some locks announce a slot as Available after a PIN lands on it; a
        provider asks this before believing such an announcement.
        """
        return (
            self.coordinator is not None
            and self.coordinator.desired_credential(pin_address(code_slot)).is_present
        )

    @final
    def is_slot_managed(self, code_slot: int) -> bool:
        """Return whether a code slot is managed by any LCM config entry for this lock."""
        return configured_owner(self.hass, self.lock.entity_id, code_slot) is not None

    @property
    def domain(self) -> str:
        """Return integration domain."""
        raise NotImplementedError()

    # The core reads through ``async_hard_refresh_codes`` instead of
    # ``async_get_usercodes`` when only it asks the lock now: a read the core
    # checks a write against must never come from a cache (#873, #1090).
    fresh_read_is_hard_refresh: ClassVar[bool] = False
    # Ask whether the device is available before every exchange, and send
    # nothing to one that is not: Z-Wave queues commands for a dead node and
    # delivers them whenever it comes back (#139).
    check_available_before_exchange: ClassVar[bool] = False
    # Seconds a read can lag behind a write (a cloud still serving the table
    # from before it). Until then a slot just written reads as silent: the
    # write is trusted, and the first read after decides.
    read_lag: ClassVar[float] = 0.0
    # Slots the last read asked about that the lock said nothing about, for
    # providers that can tell: the core reads them as silent, not unreadable.
    last_silent_slots: frozenset[int] = field(default=frozenset(), init=False)

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

    def describe_link_health(self) -> str | None:
        """
        Describe how reliably the integration is reaching this lock, if it can.

        Purely descriptive: the returned sentence is appended to the repair
        raised when a slot suspends and to the abort shown when an
        unmanaged-code clear fails, and nothing reads it to make a decision.
        A provider that cannot measure its transport returns None and the
        repair keeps its generic wording.

        The point is to state measurements, not conclusions. "Failed to sync"
        cannot distinguish a lock that refuses a code from a lock whose
        replies never arrive, and the integration has no reliable way to tell
        those apart -- but a transport that reports how much traffic it is
        losing lets the person reading the repair tell instantly. So report
        counters and leave the diagnosis to the reader (issue #1397, where a
        lock dropping roughly half its responses looked identical to a lock
        rejecting the code).
        """
        return None

    @final
    @property
    def provider_setup_succeeded(self) -> bool:
        """
        Return whether provider setup (including capability validation) succeeded.

        False both while setup is deferred (provider integration still
        loading) and after a structural validation failure (e.g. the lock
        reports zero usable PIN slots). Sync managers gate write attempts
        on this so a degraded lock is not hammered with operations that
        fail on the capability probe; the LOADED-transition revalidation
        flips it back and reconciliation resumes.
        """
        return self._setup_succeeded

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
    def credential_index_follows_slot(self) -> bool:
        """
        Return whether the Lock Code Manager slot number IS the lock's credential index.

        True (the default) for providers that address a credential by the
        slot number verbatim: the Z-Wave unified access control surface
        passes it as ``credential_slot``, and every slot-only provider
        keys its single code by slot. On those locks a slot number above
        the advertised slot count can never be written, so
        ``_assert_slot_within_capacity`` rejects it before the write.

        False for providers that let the lock allocate the index (Matter
        picks the next free credential index). There the slot number is a
        Lock Code Manager-side label with no device-side bound, so slot
        50 is legal on a 30-credential lock as long as no more than 30
        slots are configured. Capacity on those locks is a limit on the
        NUMBER of slots, which this check does not police.
        """
        return True

    @property
    def supports_code_slot_events(self) -> bool:
        """
        Return whether this lock reports which code slot was used.

        Diagnostics only. Nothing gates on it: a use is recorded against the
        user whose credential it was, and where it happened is payload rather
        than capability, so a lock that never reports one costs nothing
        anywhere else.
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
    async def async_setup_internal(
        self,
        config_entry: ConfigEntry,
        coordinator: ProviderCoordinator,
    ) -> None:
        """
        Set up lock and coordinator, signaling completion to waiters.

        Validates the lock advertises PIN credential support for
        native-user providers; structural failures
        (``LockCodeManagerProviderError``) are logged and the lock is
        kept degraded — the same outcome as the reconnect path — so the
        LOADED transition can revalidate once the underlying condition is
        fixed (e.g. a re-interview repopulating a Z-Wave lock's slot
        count). Dropping the lock here would leave it invisible with no
        retry path until a full reload.
        ``supports_user_management`` is deliberately NOT required: a
        native-user provider can serve a slot-only lock (e.g. a Z-Wave
        User Code CC fallback), in which case the seam's
        ``_supports_user_records`` gate skips the user lifecycle and
        routes through the credential primitives directly.
        Transport-level failures
        (``LockDisconnected``/``LockOperationFailed``) during the
        capability probe OR the provider's own ``async_setup`` are logged
        and the coordinator is created anyway so the integration retries
        once the lock comes online.

        When the provider integration is not connected yet (e.g. zwave_js
        still loading during a slow Home Assistant boot, issue #1321), no
        provider I/O is attempted at all: the capability probe and
        ``async_setup`` are deferred to the integration's LOADED
        transition, which re-runs them via
        ``_async_on_integration_loaded``. This keeps an untranslated
        provider exception during startup from dropping the lock
        entirely.
        """
        self._lcm_config_entry = config_entry
        try:
            try:
                if not await self.async_is_integration_connected():
                    # Expected during a cold start (the provider integration
                    # simply hasn't finished loading); recovery is automatic,
                    # so this is informational, not a warning.
                    self._setup_deferred = True
                    LOGGER.info(
                        "Provider integration for %s is not ready yet. "
                        "Provider setup is deferred until it finishes "
                        "loading; entities will be created but unavailable "
                        "until then.",
                        self.lock.entity_id,
                    )
                else:
                    await self._async_run_provider_setup(config_entry)
                    self._setup_succeeded = True
                    self._clear_setup_validation_issue()
            except TimeoutError:
                # Distinct from the transport failures below, and warned
                # rather than logged at info: those mean the lock answered
                # "no", this means it did not answer at all. A lock that is
                # merely still coming up raises LockDisconnected instead of
                # going quiet, so silence here is a wedge, not a slow boot.
                # Re-armed, or nothing ever retries. `_async_run_provider_setup`
                # clears this on the way in, and it is the only thing
                # `_handle_connection_transition` checks -- while the LOADED
                # transition the state listener waits for never comes for an
                # integration that was already loaded and then wedged. Without
                # it the lock is permanently un-set-up: sync stays gated on
                # `provider_setup_succeeded` and refuses to write anything
                # until somebody reloads by hand.
                self._setup_deferred = True
                LOGGER.warning(
                    "Capability probe for %s got no answer within %.0fs; its "
                    "integration is likely wedged rather than slow. Entities "
                    "will be created but unavailable, and setup is retried "
                    "once that integration answers again.",
                    self.lock.entity_id,
                    self.operation_timeout_seconds,
                )
            except (LockDisconnected, LockOperationFailed) as err:
                # Re-armed like the reconnect path does: an integration that
                # stays loaded (a Z-Wave node dead at boot) never brings the
                # LOADED transition this would otherwise wait for.
                self._setup_deferred = True
                LOGGER.warning(
                    "Provider setup failed for %s: %s. Coordinator will be "
                    "created but data will be unavailable until the lock "
                    "comes online. Setup will be retried when the lock "
                    "integration reconnects.",
                    self.lock.entity_id,
                    err,
                )
            except LockCodeManagerProviderError as err:
                LOGGER.error(
                    "Provider setup failed validation for %s: %s. Entities "
                    "will be created but unavailable; validation is retried "
                    "when the lock's integration reloads or reconnects.",
                    self.lock.entity_id,
                    err,
                )
                self._report_setup_validation_failure(err)

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
                self.coordinator = coordinator
                # Bounded, unlike the write path. Setup runs while Home
                # Assistant holds this entry's lock, so a read that never
                # returns parks it in `setup_in_progress` and takes reload and
                # remove down with it -- the same trap `async_stop`'s bounded
                # wait exists for, at the other end of the lifecycle.
                #
                # Cancelling a READ is safe: it mutates nothing, so there is no
                # half-written sequence to leave behind. That is what makes a
                # deadline right here and wrong around a write.
                #
                # Sized to a walk of every managed slot because that is what
                # the first read is, and a second, smaller number would give up
                # on a slow-but-working lock.
                refresh_budget = self._operation_budget(max(len(self.managed_slots), 1))
                try:
                    async with asyncio.timeout(refresh_budget):
                        # Records a failure rather than raising it; reported
                        # by the check below.
                        await self.coordinator.async_refresh()
                except TimeoutError:
                    # The coordinator already recorded the cancellation this
                    # timeout raised through it -- but it recorded a bare
                    # CancelledError, which formats as the empty string and
                    # would leave the report below saying nothing at all.
                    self.coordinator.async_set_update_error(
                        TimeoutError(f"no answer within {refresh_budget:.0f}s")
                    )

                if not self.coordinator.last_update_success:
                    LOGGER.info(
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

    async def _async_run_provider_setup(self, config_entry: ConfigEntry) -> None:
        """
        Validate lock capabilities and run provider-specific setup.

        Shared by initial setup and the integration-reconnect path so a
        lock whose integration was still loading at boot gets the same
        PIN-support validation once the integration comes up.
        """
        self._setup_deferred = False
        # One deadline over the whole method: the probe AND `async_setup`,
        # which is device I/O too -- akuvox and schlage run an unmanaged-code
        # tagging pass in it, zwave-js-ui subscribes. Setup runs while Home
        # Assistant holds the entry's lock, so anything unbounded here parks
        # the entry in `setup_in_progress` and takes reload and remove with
        # it. A provider that does none of this just returns first.
        async with asyncio.timeout(self.operation_timeout_seconds):
            await self._run_provider_setup_bounded(config_entry)

    @final
    async def _run_provider_setup_bounded(self, config_entry: ConfigEntry) -> None:
        """Validate capabilities and run provider setup, under a deadline."""
        if self.supports_native_users:
            # Bounded like the initial read, and for the same reason: setup
            # runs while Home Assistant holds the entry's lock, and this probe
            # goes through neither `_execute_rate_limited` nor any budget of
            # the provider's own -- `MatterLock.async_get_capabilities` awaits
            # `get_lock_info` bare. A probe is a read, so cancelling it is
            # safe.
            caps = await self._get_cached_capabilities()
            self.pin_unsupported = CredentialType.PIN not in caps.credential_types
            if self.pin_unsupported:
                # A degenerate-but-successful probe was just cached;
                # invalidate it so the reconnect-path revalidation
                # re-probes instead of serving the failed read forever.
                self._capabilities_cache = None
                raise LockCodeManagerProviderError(
                    f"{self.lock.entity_id}: lock does not advertise PIN credential support"
                )
        await self.async_setup(config_entry)

    @final
    @callback
    def _report_setup_validation_failure(self, err: Exception) -> None:
        """
        Raise a repair issue so the structural failure is visible in the UI.

        The actionable error message (e.g. "re-interview the lock") would
        otherwise land only in the log. Persistent so it survives restarts:
        the underlying condition does too.
        """
        async_create_issue(
            self.hass,
            DOMAIN,
            per_lock_issue_id("lock_setup_failed", self.lock.entity_id),
            is_fixable=False,
            is_persistent=True,
            severity=IssueSeverity.ERROR,
            translation_key="lock_setup_failed",
            translation_placeholders={
                "lock_entity_id": self.lock.entity_id,
                "error": str(err),
            },
        )

    @final
    @callback
    def _clear_setup_validation_issue(self) -> None:
        """Dismiss the setup-failed repair issue after validation succeeds."""
        async_delete_issue(
            self.hass,
            DOMAIN,
            per_lock_issue_id("lock_setup_failed", self.lock.entity_id),
        )

    async def _async_on_integration_loaded(self) -> None:
        """
        Handle provider integration LOADED transition.

        Re-runs the capability validation and ``async_setup`` to
        re-initialize provider state (e.g. re-register event listeners
        after an integration reload), then refreshes the coordinator and
        subscribes to push updates. The validation matters for locks
        whose initial setup was deferred because the integration was
        still loading (issue #1321) -- this is the first time it can run.

        Operations are chained sequentially so setup completes before
        the coordinator refresh or push subscription begins.

        A connectivity failure here re-arms the deferred flag. That flag is
        the only thing that arms another attempt for a lock whose integration
        was loaded all along, and ``_async_run_provider_setup`` clears it on
        the way in -- so a blip during the retry spent the retry, and the lock
        stayed un-set-up with sync, gated on that, refusing to write anything
        until the integration was reloaded by hand. A validation failure
        deliberately does not re-arm: that lock is known-degraded with a
        diagnosis and its own revalidation path, and re-probing it from every
        connection check would put a capability read on the wire every thirty
        seconds to learn what it already knows.
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
            await self._async_run_provider_setup(self._lcm_config_entry)
        except LockDisconnected, LockOperationFailed, TimeoutError:
            # TimeoutError because the capability probe is bounded: a lock that
            # answers nothing is a connectivity failure like any other, and it
            # has to re-arm the deferred flag. Letting it escape instead would
            # skip the coordinator refresh and push subscription below, leaving
            # a push provider unsubscribed for the life of the entry.
            self._setup_deferred = True
            LOGGER.debug(
                "Provider setup failed for %s, will retry on next reconnect",
                self.lock.entity_id,
                exc_info=True,
            )
        except LockCodeManagerProviderError as err:
            LOGGER.error(
                "Provider setup failed validation for %s: %s",
                self.lock.entity_id,
                err,
            )
            # A previously-validated lock can fail revalidation (e.g. a
            # re-interview zeroed its slot count); reset the flag so sync
            # stays gated and the eventual recovery is observable.
            self._setup_succeeded = False
            self._report_setup_validation_failure(err)
        else:
            if not self._setup_succeeded:
                LOGGER.info(
                    "Provider setup succeeded for %s",
                    self.lock.entity_id,
                )
            self._setup_succeeded = True
            self._clear_setup_validation_issue()
            if self.coordinator:
                # A bridged provider only learns it can push once its
                # discovery data lands, which for a deferred setup is after
                # the coordinator picked its cadence.
                self.coordinator.note_push_capability()
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
        if remove_permanently:
            # The lock is leaving Lock Code Manager entirely; a persistent
            # setup-failed repair would otherwise outlive it. Non-permanent
            # unloads (reload, restart) keep it — the condition does too.
            self._clear_setup_validation_issue()

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
                self._async_start_reconnect()
            elif (
                self.supports_push and self._last_entry_state == ConfigEntryState.LOADED
            ):
                self.unsubscribe_push_updates()

            self._last_entry_state = to_state

        self._config_entry_state_unsub = lock_entry.async_on_state_change(
            _handle_state_change
        )

    @final
    @callback
    def _async_start_reconnect(self) -> None:
        """
        Run the integration-loaded path, superseding any reconnect in flight.

        Two things ask for it -- the provider's config entry reaching LOADED,
        and reachability returning for a lock whose setup never ran -- and
        both land in the same task slot so ``async_unload`` can cancel
        whichever one started it.

        A provider can pass through LOADED twice in quick succession (a
        reload during a reconnect), so a prior task is cancelled and its
        exception drained: an already-failed task that nobody retrieves
        leaks a warning at collection time.
        """
        if self._reconnect_task is not None:
            self._reconnect_task.add_done_callback(self._drain_superseded_reconnect)
            if not self._reconnect_task.done():
                self._reconnect_task.cancel()
        self._reconnect_task = self.hass.async_create_task(
            self._async_on_integration_loaded(),
            f"Provider reconnect for {self.lock.entity_id}",
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
    async def async_internal_is_reachable(self) -> bool:
        """
        Return whether the lock is reachable end-to-end right now.

        Combines the integration/transport signal
        (``async_is_integration_connected``) with the device/node signal
        (``async_is_device_available``): the lock is reachable only when both
        layers are up. Recovery is therefore detected uniformly whether the
        outage was at the network or the device layer -- a node that comes
        back while the integration stayed connected drives the same
        resubscribe/refresh transition as an integration reconnect, instead of
        waiting out the connectivity breaker's backoff probe (issue #1257).
        """
        is_up = (
            await self.async_is_integration_connected()
            and await self.async_is_device_available()
        )
        self._note_reachability(is_up)
        return is_up

    @final
    @callback
    def _note_reachability(self, is_up: bool) -> None:
        """Feed a reachability observation to the transition handler and remember it."""
        self._handle_connection_transition(is_up)
        self._last_connection_up = is_up

    @final
    @callback
    def _handle_connection_transition(self, is_up: bool) -> None:
        """Handle setup retry and push subscribe/unsubscribe on state transitions."""
        lock_entry = self.lock_config_entry
        if not lock_entry:
            return
        # Skip during SETUP_IN_PROGRESS: the setup path handles the initial
        # subscription, and a parallel subscribe here would race with
        # coordinator creation in async_setup_internal.
        if lock_entry.state != ConfigEntryState.LOADED:
            return
        if is_up and self._setup_deferred:
            # Setup was skipped entirely because the provider integration was
            # not ready, and the LOADED transition that would re-run it never
            # comes for an integration that was already loaded -- an MQTT
            # bridge whose discovery data simply had not been replayed yet at
            # boot. Left alone the lock stays permanently un-set-up: no
            # capability validation, no provider setup, and sync, gated on
            # that, refusing to write anything ever again.
            #
            # Any reachable observation is the retry, not a transition up:
            # the very first connection check can already find the lock
            # reachable, and then there is no transition to wait for. Setup
            # running is what clears the flag, so this fires once even if
            # what it finds is a lock that fails validation.
            self._async_start_reconnect()
            return
        if not self.supports_push:
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

    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """
        Re-read from the lock device and return the same shape as async_get_usercodes().

        ``slots`` names the slots whose device state must be re-read; ``None``
        means everything. The returned projection is never narrowed by it:
        the coordinator replaces its data with what comes back, so a read
        must still name every managed slot. A provider that reads the whole
        device in one call may ignore ``slots``. One that keeps a cache
        re-reads only ``slots`` and projects the rest from the cache: the
        coordinator's confirmation read asks about the one or two slots with
        a write pending, and on a marginal radio link the difference between
        one command and a walk of the lock is the difference between
        confirming the write and never confirming it. One with no cache
        re-reads every managed slot plus ``slots``.
        """
        self._raise_not_implemented(
            "async_hard_refresh_codes",
            "Override this method to re-fetch codes from the lock device.",
        )

    async def async_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> WriteResult:
        """
        Set a usercode on a code slot via the User->Credential primitives.

        Projects the slot to a single Personal Identification Number
        credential. Native-user providers run the create-on-first user
        lifecycle via ``_set_credential``; slot-only providers write the
        credential directly, addressing it by slot. Returns the provider's
        ``WriteResult`` (NO_CHANGE / CONFIRMED / OPTIMISTIC).
        """
        state = SlotCredential.known(usercode)
        credential = credential_from_slot(code_slot, state)
        pin = self._require_readable_pin(credential)
        if not self.supports_native_users:
            return await self.async_set_credential(
                code_slot, credential, pin, name=name, source=source
            )
        return await self._set_credential(
            user_from_slot(code_slot, state, name),
            credential,
            pin,
            name=name,
            source=source,
        )

    async def async_clear_usercode(
        self, code_slot: int, *, adopt_untagged: bool = True
    ) -> bool:
        """
        Clear a usercode on a code slot via the User->Credential primitives.

        Slot-only providers address the credential by slot and delete it
        directly. Native-user providers must target the credential's actual
        owning user, which is not assumed to equal the slot index: a
        credential may have been created on the lock by another controller,
        or the integration may have allocated a user identifier that differs
        from the slot. The owning ``user_id`` is resolved from the lock's
        current users and threaded through the ``CredentialRef`` so the
        provider's delete primitive can address the credential precisely.
        The lock-side user stays put; it is torn down only when the slot
        is removed from LCM config (see ``async_release_managed_slot``).
        Returns True if the value changed, False if it was already cleared
        -- and True when the provider cannot determine whether a change
        occurred, so the coordinator refreshes and verifies the actual
        state.

        ``adopt_untagged`` is what separates a clear from a release. Clearing
        a slot LCM still manages may adopt an untagged owner: the slot is
        ours, adoption is how a pre-tag install is taken over, and the write
        that follows corrects a wrong guess. Releasing a slot LCM no longer
        manages may not -- the premise has inverted, nothing follows to
        correct anything, and the untagged user at that index is as likely to
        be somebody else's as ours.
        """
        if not self.supports_native_users:
            ref = CredentialRef(
                user_id=code_slot, type=CredentialType.PIN, slot=code_slot
            )
            return await self.async_delete_credential(ref)

        # Owner resolution is two-pass to match the same identity rule the
        # set path uses (see Matter's _find_user_index_for_slot). The
        # canonical pass matches by the ``lcm:<slot>:`` tag in user.name;
        # the fallback adopts installs from before user-tag matching,
        # where ``credential.slot`` was pinned to the LCM slot. Matching
        # by ``credential.slot == code_slot`` alone is unsafe once
        # providers let the lock auto-allocate the credential index -- a
        # tagged user for slot A whose credential lands at index B would
        # be mis-matched when clearing slot B.
        users = await self.async_get_users()
        # Both lookups require the user to actually own a PIN credential
        # at the slot we're clearing. Under the persistent-user-anchor
        # lifecycle a tagged user can exist without a PIN (between
        # writes); resolving such a user as the owner would drive a
        # spurious ``async_delete_credential`` call whose provider-
        # specific return value can incorrectly report changed=True.
        owner_user_id: int | None
        try:
            owner_user_id = next(
                user.user_id
                for user in users
                if user.name and parse_tag(user.name)[0] == code_slot
                for credential in user.pin_credentials
                if credential.slot == code_slot
            )
        except StopIteration:
            owner_user_id = (
                next(
                    (
                        user.user_id
                        for user in users
                        if parse_tag(user.name or "")[0] is None
                        for credential in user.pin_credentials
                        if credential.slot == code_slot
                    ),
                    None,
                )
                if adopt_untagged
                else None
            )
        if owner_user_id is None:
            # No user owns this slot's credential -- nothing to clear.
            return False

        ref = CredentialRef(
            user_id=owner_user_id, type=CredentialType.PIN, slot=code_slot
        )
        return await self._delete_credential(ref)

    async def _project_users_to_slots(
        self,
        credential_type: CredentialType,
        slots: Collection[int] | None = None,
    ) -> dict[int, SlotCredential]:
        """
        Project the lock's users to a slot -> ``SlotCredential`` map.

        Every slot in scope is present even when empty: the projection
        starts from ``slots`` -- or ``managed_slots`` when the caller names
        none -- mapped to ``SlotCredential.empty()``
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
        scope = self.managed_slots if slots is None else slots
        codes = {slot: SlotCredential.empty() for slot in scope}
        codes.update(
            {
                credential.slot: credential.state
                for user in await self.async_get_users(slots)
                for credential in user.credentials_of_type(credential_type)
            }
        )
        return codes

    async def async_get_usercodes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """
        Return the slot -> ``SlotCredential`` map for Personal Identification Numbers.

        Thin Personal-Identification-Number-shaped wrapper over
        ``_project_users_to_slots``; preserved as a stable name because
        the coordinator, sync manager, and slot entities are all
        Personal Identification Number-scoped today.

        ``slots`` replaces the set of slots this integration manages, for
        callers asking what the lock holds rather than what this integration
        put there. It can be wider or narrower, so a scoped result is not
        the coordinator's contract and must not be handed to it: a managed
        slot missing from a narrow read reads as unavailable, not empty. Everything read-repairing happens on
        this side of the seam, so a caller asking a wider question gets the
        repairs too -- which is why occupancy is derived from here and not
        read separately.
        """
        return await self._project_users_to_slots(CredentialType.PIN, slots)

    @final
    @property
    def cached_capabilities(self) -> LockCapabilities | None:
        """
        Return the already-probed capabilities, or ``None``. Never performs I/O.

        Synchronous read of the same cache ``_get_cached_capabilities``
        populates. Lets synchronous callers (e.g. the PIN text entity sizing
        its length bounds) consult capabilities without awaiting; an unprobed
        or disconnected lock reads ``None`` and contributes no constraint.
        """
        return self._capabilities_cache

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

    async def _supports_user_records(self) -> bool:
        """
        Return whether the lock exposes a separate user-record write path.

        False covers both no-user-management locks and the implicit-user
        case (e.g. Z-Wave User Code CC: the user IS the credential).
        """
        caps = await self._get_cached_capabilities()
        return caps.supports_user_management and caps.max_user_name_length > 0

    async def _build_tagged_user_name(
        self, slot: int, display: str | None
    ) -> str | None:
        """
        Build the LCM-tagged user name for a slot, fitting the lock's length.

        Emits ``lcm:{slot}:{display}`` (canonical format from
        :func:`._util.make_tagged_name`) and truncates the display portion
        so the overall length fits ``max_user_name_length``. The tag prefix
        is sacred -- it's how :func:`._util.parse_tag` recovers the slot
        binding on subsequent reads -- so when the full prefix fits,
        truncation only ever shortens the user-supplied display.

        When the lock's limit can't even fit the canonical prefix
        (``len("lcm:{slot}:") > max_user_name_length``), the helper
        falls back to writing just the slot number as the user name
        (``str(slot)``). :func:`._util.parse_tag` recognizes
        digit-only names as a length-constrained encoding of the slot
        binding. If even the slot digits don't fit
        (``len(str(slot)) > max_user_name_length``, e.g. slot 255 on a
        2-char-limit lock), the helper returns ``None`` rather than
        truncating the slot to a different number and silently
        mis-binding the user. The seam treats ``None`` as "leave name
        alone," which means LCM cannot identify its own user on such a
        lock -- those locks are effectively unmanaged at the user-name
        level.

        Returns ``None`` when the lock has no concept of named users
        (``supports_user_management`` is False or
        ``max_user_name_length <= 0``); the seam's
        ``_supports_user_records`` gate already short-circuits the
        user-write path on such locks, but the guard here is
        defensive in case the helper is called outside that gate. A
        best-effort capabilities-fetch failure also returns ``None``
        rather than blocking the write -- the cache stays unset so the
        next call retries.

        Worst-case canonical prefix overhead is 8 characters
        (``lcm:255:``); on a 10-character-limit lock that leaves 2
        characters of display, which is degenerate but functional.
        Locks advertising a length below that (rare) hit the slot-only
        fallback or the ``None`` return.
        """
        try:
            caps = await self._get_cached_capabilities()
        except LockDisconnected, LockOperationFailed:
            return None
        if not caps.supports_user_management or caps.max_user_name_length <= 0:
            return None
        tagged = make_tagged_name(slot, display)
        if len(tagged) <= caps.max_user_name_length:
            return tagged
        if len(f"lcm:{slot}:") > caps.max_user_name_length:
            # Canonical prefix doesn't fit. Try the slot-only fallback so
            # the slot binding survives the read; if the slot digits
            # themselves can't fit either, return ``None`` rather than
            # truncate the slot number to a different one and mis-bind
            # the user.
            slot_str = str(slot)
            if len(slot_str) > caps.max_user_name_length:
                return None
            return slot_str
        # Canonical prefix fits; truncate only the display portion.
        return tagged[: caps.max_user_name_length]

    async def _assert_credential_type_supported(self, credential: Credential) -> None:
        """
        Raise ``CodeRejectedError`` if the lock doesn't advertise the type.

        Capability-driven defense for ``async_set_credential``. Picks up
        new types (e.g. PASSWORD) automatically once a lock starts
        advertising them in ``credential_types`` -- no provider-side
        change needed.
        """
        caps = await self._get_cached_capabilities()
        if credential.type not in caps.credential_types:
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason=f"unsupported credential type: {credential.type}",
            )

    async def _assert_slot_within_capacity(self, code_slot: int) -> None:
        """
        Raise ``LockOperationUnsupported`` if the slot exceeds the lock's capacity.

        Only meaningful when ``credential_index_follows_slot`` is True; see
        that property for why Matter is exempt. Without this the write goes
        out and the lock rejects it deterministically, which providers have
        no reliable way to tell apart from a transport failure — that
        misclassification is what made an out-of-range slot look like an
        unreachable lock (issue #1398).

        A capabilities read that fails, or one whose slot count is unknown
        (see ``LockCapabilities.bounded_slot_count``), skips the check
        rather than blocking the write, preserving the previous behavior
        for those cases instead of inventing a new failure.

        The advertised count is the lock's own claim and is not always
        right -- a Z-Wave node hitting the User Credential CC dispatch
        defect can report the wrong command class's slot count -- so the
        error names the stale-interview remedy alongside the renumber one.
        """
        if not self.credential_index_follows_slot:
            return
        try:
            caps = await self._get_cached_capabilities()
        except LockCodeManagerError:
            # Covers the unreachable lock, the failed probe, and the provider
            # that never implemented the read (ProviderNotImplementedError).
            return
        num_slots = caps.bounded_slot_count(CredentialType.PIN)
        if num_slots is None or 1 <= code_slot <= num_slots:
            return
        raise LockOperationUnsupported(
            f"Lock {self.lock.entity_id} reports {num_slots} PIN code slots, so "
            f"slot {code_slot} is out of range. Either renumber the slot to fall "
            f"within 1-{num_slots} (or drop this lock from the slot), or, if the "
            f"lock really does have more slots than that, re-interview it -- a "
            f"stale or incomplete interview makes it under-report its capacity."
        )

    async def _assert_credential_ref_supported(self, ref: CredentialRef) -> None:
        """
        Raise ``CodeRejectedError`` if the lock doesn't advertise the type.

        Delete-path sibling of ``_assert_credential_type_supported``.
        """
        caps = await self._get_cached_capabilities()
        if ref.type not in caps.credential_types:
            raise CodeRejectedError(
                code_slot=ref.slot,
                lock_entity_id=self.lock.entity_id,
                reason=f"unsupported credential type: {ref.type}",
            )

    def _require_readable_pin(self, credential: Credential) -> str:
        """
        Return the credential's readable PIN, or raise ``CodeRejectedError``.

        Called by the seam (``async_set_usercode`` and ``_set_credential``)
        before dispatching to the provider's ``async_set_credential``
        override; the resolved ``pin: str`` is threaded through as a
        positional argument so the provider receives a guaranteed string
        and does not need its own defensive check.

        The seam already builds credentials from a string usercode so
        ``readable_pin`` is non-None by construction along that path -- the
        guard catches future regressions (a credential constructed outside
        the seam, a refactor that loses the invariant) and never the
        normal flow. Providers MUST NOT re-add their own readable-pin
        check; doing so duplicates the contract and re-introduces the
        ``or ""`` silent coercion pattern this helper exists to eliminate.
        """
        pin = credential.readable_pin
        if pin is None:
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason="cannot write an unreadable credential",
            )
        return pin

    @final
    async def _set_credential(
        self,
        user: User,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Run the create-on-first user lifecycle around a credential write.

        Native-user only (the slot adapters call the credential primitive
        directly). Asserts the lock advertises ``credential.type`` and
        replaces ``user.name`` with the LCM-tagged name built via
        ``_build_tagged_user_name(credential.slot, user.name)`` before
        handing the user to the provider, so each provider's
        ``async_set_user`` can write the tagged name verbatim. The tag
        carries the slot binding the find-or-create-by-tag lookup
        recovers on subsequent operations. Rolls back a newly-created
        user when the credential write fails so the lock isn't left
        with a credential-less user the slot-keyed coordinator
        can't reconcile. Returns True if the value changed.

        ``pin`` is the resolved readable PIN that the caller (the seam in
        ``async_set_usercode``) has already validated via
        ``_require_readable_pin``; threading it through avoids a second
        validation pass in the provider.
        """
        await self._assert_credential_type_supported(credential)
        if await self._supports_user_records():
            tagged = await self._build_tagged_user_name(credential.slot, user.name)
            if tagged is None:
                # No stable slot tag fits the lock's max_user_name_length --
                # writing a user without one (or with name=None) would break
                # the find-or-create-by-tag lookup the next operation needs.
                # Fail loudly rather than create an unrecoverable user.
                raise LockOperationFailed(
                    f"Lock {self.lock.entity_id} cannot encode a stable slot "
                    f"tag for slot {credential.slot}; refusing to write a "
                    "credential whose owning user could not be re-identified"
                )
            user_for_write = user if tagged == user.name else replace(user, name=tagged)
            result = await self.async_set_user(user_for_write)
            credential_user_id = result.user_id
            rollback_user_id = result.user_id if result.created else None
        else:
            credential_user_id = user.user_id
            rollback_user_id = None
        try:
            return await self.async_set_credential(
                credential_user_id, credential, pin, name=name, source=source
            )
        except Exception:
            # Not on cancellation: the rollback is one more exchange with a
            # lock that has just been presumed wedged, taken while still
            # holding the lock's turn. The user it would delete is found by
            # tag and reused on the next write instead.
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
    async def _delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete a credential without touching its owning user.

        Native-user only. Asserts the lock advertises ``ref.type``, then
        deletes the credential. The lock-side user is now an LCM-managed
        slot anchor (see the user-tag idempotency design) -- created on
        first slot-configured write and removed only on slot removal
        from LCM config via ``async_release_managed_slot``. Clear and
        rewrite cycles preserve the slot's lock-side user, so this
        helper is now a thin guard around ``async_delete_credential``.
        Returns True if changed.
        """
        await self._assert_credential_ref_supported(ref)
        return await self.async_delete_credential(ref)

    async def async_release_managed_slot(self, slot: int) -> None:
        """
        Release any lock-side state LCM owns for ``slot``.

        Called by ``__init__.py``'s teardown path once a slot is removed
        from LCM config (per the user-tag idempotency design's lifecycle
        decoupling: a lock-side user is the slot's persistent anchor and
        survives PIN clear/rewrite cycles, so it can only be torn down
        when the slot itself is removed from LCM management).

        Clears the credential. A slot-only provider has no user record,
        but the credential IS lock-side state this integration owns, and
        leaving it programmed means a deleted user's PIN still opens the
        door -- and that the slot number can never be reissued, because
        allocation refuses to write over a code it can still read.

        Providers that anchor a slot to a lock-side user (Matter,
        eventually Z-Wave User Credential CC) override this to delete
        that user instead; the cascade defined by the lock's protocol
        removes its credentials, so they must not also clear here.
        """
        # Adopts no untagged user (see ``async_clear_usercode``): a slot LCM
        # no longer manages has no claim on whoever owns the credential at
        # that index. Unless the lock keeps no user records (a Z-Wave User
        # Code lock): no user there can carry a tag, the slot is the
        # identity, and it was this integration's until now.
        await self.async_clear_usercode(
            slot,
            adopt_untagged=self.supports_native_users
            and not await self._supports_user_records(),
        )

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

        Native-user providers only. Under the user-tag idempotency design
        the lock-side user is an LCM-managed slot anchor: it persists
        through PIN clear / rewrite cycles and is removed only when the
        slot is dropped from LCM config (the base calls this from a
        provider's ``async_release_managed_slot`` override). Also called
        as a rollback path when a credential write fails for a freshly
        created user.
        """
        self._raise_not_implemented(
            "async_delete_user",
            "Override on native-user providers to delete a lock user.",
        )

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set or update one credential, returning the write outcome.

        Every migrated provider implements this. ``user_id`` identifies the
        owning user for native-user providers; slot-only providers ignore it
        and address the credential by ``credential.slot``. ``pin`` is the
        resolved readable PIN string; the base seam validates that
        ``credential.readable_pin`` is non-None and passes it through so
        providers receive a guaranteed string and don't need their own
        defensive checks. Providers raise ``DuplicateCodeError`` when the
        lock rejects the value as a duplicate. Native-user providers carry
        the user's name on the user record, so they may treat ``name`` here
        as advisory; slot-only providers use it (for example as a tagged
        code name). A ``name`` of ``None`` means leave any existing name
        unchanged, never clear it.

        Return ``WriteResult.NO_CHANGE`` if the value was already set,
        ``WriteResult.CONFIRMED`` when the lock acknowledged the write, and
        ``WriteResult.OPTIMISTIC`` when the result is ambiguous (the write may
        have landed but is unconfirmed -- e.g. a write-only/masked lock). The
        outcome drives the coordinator refresh and the verified/unverified
        slot lifecycle.
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

    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Read every user and their credentials from the lock.

        Backs the default ``async_get_usercodes`` projection. Native-user
        providers map their integration's user list; slot-only providers
        project each occupied slot to a single-credential user via
        ``user_from_slot``.

        ``slots`` names which credential indices to report on; ``None``
        means the slots this integration manages. Providers that must ask
        the lock about one index at a time MUST honour it, or a caller
        widening the question walks the lock's entire advertised capacity
        one round trip at a time. Providers that read the whole lock in one
        call may ignore it, because the result is allowed to EXCEED the
        scope -- the projection seeds the scope but overlays everything the
        provider reports, so an unmanaged occupied slot stays visible on the
        default path. A caller that needs the answer bounded must bound it.

        A wide scope is the caller's to justify. The whole read is one
        rate-limited operation, so on a per-index provider it holds the
        operation lock for every round trip it makes -- blocking the poll
        and any set or clear behind it for as long as that takes.
        """
        self._raise_not_implemented(
            "async_get_users",
            "Override to read users and credentials from the lock.",
        )

    async def async_get_max_slot(self) -> int | None:
        """
        Return the highest slot number worth asking this lock about.

        A search for free slot numbers has to stop somewhere, and only the
        lock knows where. Past the end of its range a lock cannot report a
        slot it does not have, so every index beyond it comes back as
        occupied -- a searcher that does not know where to stop walks upward
        forever finding nothing free.

        ``None`` means this lock has no opinion, not that it has no slots.
        The caller supplies the limit then, and knows the number came from
        nowhere -- which matters, because telling a user their lock reported
        a capacity it never reported sends them to re-interview it over a
        number it never said.

        Distinct from ``bounded_slot_count``, whose ``None`` means "I could
        not read a capacity, so do not refuse a write over it". That answer
        is deliberately permissive about WRITING; this one is about how far
        to SEARCH, and a caller must not read one as the other.

        Where the answer comes from differs by provider: an advertised
        capacity, a protocol attribute, or nothing at all -- where the slot
        number is this integration's own bookkeeping rather than an index on
        the lock, there is no device range to report.

        Note this is about the END OF THE RANGE, not about which numbers
        inside it are usable. A slot the lock reports as unsupported or
        disabled is a fact about that slot, and is still worth asking about.
        """
        try:
            capabilities = await self._get_cached_capabilities()
        except Exception:
            # Including whatever a provider raises that is not one of this
            # integration's own errors. Having no opinion is an answer; an
            # exception escaping here is not.
            _LOGGER.debug(
                "Could not ask %s how far its slot numbers go",
                self.lock.entity_id,
                exc_info=True,
            )
            return None
        return capabilities.bounded_slot_count(CredentialType.PIN)

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

    @property
    def managed_slots(self) -> set[int]:
        """Return the slot numbers any entry's users hold on this lock, disabled included."""
        return slots_configured_on(self.hass, self.lock.entity_id)

    @final
    @callback
    def async_fire_code_slot_event(
        self,
        code_slot: int | None = None,
        to_locked: bool | None = None,
    ) -> None:
        """
        Record that a credential on this lock was used.

        Sub-classes should call this whenever a code slot is used.

        The single funnel every provider goes through, so it is also where
        ``BUS_EVENT_CREDENTIAL_USED`` is fired: every provider gets it
        without a provider-side change. A lock that observed a use is both
        ends of the event's attribution, and only a lock reaches here -- a
        use reported from outside is announced by the ``use_credential``
        action itself.
        """
        lock_entity_id = self.lock.entity_id
        config_entry: ConfigEntry | None = None
        credential_user: str | None = None

        if code_slot is not None and (
            config_entry := configured_owner(self.hass, lock_entity_id, int(code_slot))
        ):
            # The user comes from the configuration rather than from the name
            # text entity: that entity can be disabled, and it reads
            # ``unknown`` until it restores after a reload, either of which
            # would announce a name no consumer can match a user to.
            credential_user = get_entry_config(config_entry).name_for(int(code_slot))

        # Only PIN is exercised today, but a consumer that reads it now keeps
        # working when another kind arrives.
        credential_type = CredentialType.PIN

        # Attribution is the whole content of this event, and every field of
        # it is required, so a use this integration cannot pin to an entry --
        # a code in a slot no entry manages -- announces nothing at all. The
        # two conditions move together: an entry only matches a slot somebody
        # occupies, so a matched entry always names its user.
        if config_entry is not None and credential_user is not None:
            async_fire_credential_used(
                self.hass,
                config_entry,
                name=credential_user,
                # A lock that observed the use is both where the credential
                # was entered and what it acted on.
                source=lock_entity_id,
                target=lock_entity_id,
                credential_type=credential_type,
                # The direction the lock reported moving, which every
                # provider already hands this method. A provider that could
                # not classify the notification passes None and says so.
                operation=CredentialOperation.from_to_locked(to_locked),
            )
