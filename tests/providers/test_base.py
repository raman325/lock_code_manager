"""Test base class."""

import asyncio
from datetime import timedelta
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_NAME,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import REQUEST_REFRESH_DEFAULT_COOLDOWN

from custom_components.lock_code_manager.const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_OPERATION,
    ATTR_SOURCE,
    ATTR_SYNC_STATUS,
    ATTR_TARGET,
    BACKOFF_INITIAL_SECONDS,
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.core.adapter import (
    ProviderPort,
    _Coordinator,
)
from custom_components.lock_code_manager.core.slot import LIMIT
from custom_components.lock_code_manager.domain.allocation import async_read_occupancy
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    User,
)
from custom_components.lock_code_manager.domain.events import CredentialOperation
from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.domain.lock_runtime import lock_registry
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.domain.util import per_lock_issue_id
from custom_components.lock_code_manager.providers import _base as base_module
from custom_components.lock_code_manager.providers._base import BaseLock
from custom_components.lock_code_manager.providers._util import (
    make_tagged_name,
)
from tests.common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_DEVICE_DOMAIN,
    NO_PIN_CAPABILITIES,
    PIN_CAPABILITIES,
    MockLCMLock,
    MockLCMPushLock,
    async_advance_across_deadline,
    async_advance_clock,
    async_advance_sync_ticks,
    async_run_until_quiet,
    async_set_slot_pin,
    async_tick_until,
    code_entity_id,
    current_entry,
    in_sync_entity_id,
    mock_lock,
    sync_status,
    when_mock_lock_built,
)

from ..common import TICK_INTERVAL

TEST_OPERATION_DELAY = 0.01


def _stand_in(lock: BaseLock) -> _Coordinator:
    """
    Return the coordinator stand-in the provider port hands setup.

    Its port is closed, so nothing the provider asks of it arms a poll that
    would outlive the test: these tests drive the provider alone.
    """
    port = ProviderPort(lock)
    port._closed = True
    return _Coordinator(port)


class MockLCMLockWithPush(MockLCMLock):
    """Mock lock with push subscription tracking."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize mock lock."""
        super().__init__(*args, **kwargs)
        self.subscribe_calls = 0
        self.unsubscribe_calls = 0

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates."""
        self.subscribe_calls += 1

    def teardown_push_subscription(self) -> None:
        """Unsubscribe from push-based value updates."""
        self.unsubscribe_calls += 1


class _CapsLock(MockLCMLock):
    """Mock lock that advertises PIN capabilities."""

    async def async_get_capabilities(self) -> LockCapabilities:
        """Report a single PIN credential type with a 4-8 length range."""
        return LockCapabilities(
            supports_user_management=True,
            max_users=30,
            credential_types={
                CredentialType.PIN: CredentialTypeCapability(
                    num_slots=30, min_length=4, max_length=8, supports_learn=False
                )
            },
        )


async def test_cached_capabilities_exposes_warmed_cache(hass: HomeAssistant):
    """cached_capabilities is None until probed, then returns the cached snapshot."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "caps_lock", config_entry=config_entry
    )
    lock = _CapsLock(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)

    assert lock.cached_capabilities is None

    caps = await lock._get_cached_capabilities()
    assert lock.cached_capabilities is caps
    assert lock.cached_capabilities.length_bounds(CredentialType.PIN) == (4, 8)


async def test_base(hass: HomeAssistant):
    """Test base class."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create a proper registry entry for the mock lock
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    # The stand-in the provider port hands setup: nothing is read here, the
    # worker's first read comes after.
    assert await lock.async_setup_internal(config_entry, _stand_in(lock)) is None
    assert lock.coordinator is not None
    assert await lock.async_unload(False) is None
    assert lock.usercode_scan_interval == timedelta(minutes=1)
    with pytest.raises(NotImplementedError):
        assert lock.domain
    # async_internal_is_reachable combines the integration and device
    # signals — it returns False here because the test config entry isn't in
    # the LOADED state (integration down short-circuits the combined check).
    assert await lock.async_internal_is_reachable() is False
    # The raw hooks the provider port calls have no connection gate of their
    # own: each reaches the primitive a provider has to supply.
    with pytest.raises(NotImplementedError):
        await lock.async_hard_refresh_codes()
    with pytest.raises(NotImplementedError):
        await lock.async_clear_usercode(1)
    with pytest.raises(NotImplementedError):
        await lock.async_set_usercode(1, "1234")
    with pytest.raises(NotImplementedError):
        await lock.async_get_usercodes()


async def test_unsubscribe_push_updates_suppresses_not_implemented(
    hass: HomeAssistant,
) -> None:
    """Test unsubscribe_push_updates swallows ProviderNotImplementedError.

    Providers that don't override teardown_push_subscription raise
    ProviderNotImplementedError from the base default.  The public
    unsubscribe_push_updates() wrapper must suppress that error so callers
    (e.g. async_setup teardown on reconnect) never see it propagate.
    """
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_no_push",
        config_entry=config_entry,
    )
    lock = BaseLock(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)

    # teardown_push_subscription raises ProviderNotImplementedError by default
    with pytest.raises(ProviderNotImplementedError):
        lock.teardown_push_subscription()

    # The public wrapper must not propagate it
    lock.unsubscribe_push_updates()  # must not raise


async def test_describe_link_health_defaults_to_none(hass: HomeAssistant) -> None:
    """A provider that cannot measure its transport describes nothing."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_link_health",
        config_entry=config_entry,
    )
    lock = BaseLock(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)

    assert lock.describe_link_health() is None


def _lock_args(hass: HomeAssistant, unique_id: str) -> tuple:
    """Build the positional arguments every provider is constructed with."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", unique_id, config_entry=config_entry
    )
    return (hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)


def _bare_lock(hass: HomeAssistant, unique_id: str) -> MockLCMLock:
    """Build a reachable lock whose read can be stubbed."""
    return MockLCMLock(*_lock_args(hass, unique_id))


async def test_occupancy_counts_everything_the_lock_holds(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Occupancy is derived from a read in the lock's turn, so every masked code counts.

    An index whose value could not be read still holds something. Reserving
    it costs a user a slot number; handing it out costs them the code on
    their door. What the lock says about indices outside the window asked
    about is not an answer to this question.
    """
    asked: list[list[int]] = []

    async def _read(self, slots=None):
        asked.append(sorted(slots))
        return {
            0: SlotCredential.known("0000"),
            1: SlotCredential.known("1234"),
            2: SlotCredential.empty(),
            3: SlotCredential.unreadable(),
            12: SlotCredential.known("9999"),
        }

    with patch.object(MockLCMLock, "async_get_usercodes", _read):
        occupancy = await async_read_occupancy(
            hass, None, [LOCK_1_ENTITY_ID], range(1, 11)
        )

    [lock] = occupancy.locks
    assert lock.occupied == frozenset({1, 3})
    # The read is scoped, so a per-index lock is not walked end to end.
    assert asked == [list(range(1, 11))]


async def test_occupancy_asks_nothing_when_there_is_nothing_to_ask(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """An empty range costs no round trip.

    A caller widening its search hands over only the indices it has not
    covered, and on the pass where that set is empty there is nothing to
    learn -- reaching the lock would be a round trip for an answer already
    held.
    """
    read = AsyncMock(return_value={})
    with patch.object(MockLCMLock, "async_get_usercodes", read):
        occupancy = await async_read_occupancy(hass, None, [LOCK_1_ENTITY_ID], [])

    [lock] = occupancy.locks
    assert lock.occupied == frozenset()
    read.assert_not_awaited()


class _PushSetupRaisesLock(MockLCMLock):
    """Mock lock whose setup_push_subscription raises a configurable error."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize mock lock."""
        super().__init__(*args, **kwargs)
        self.push_setup_error: Exception | None = None

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates, raising if configured to."""
        if self.push_setup_error is not None:
            raise self.push_setup_error


async def test_subscribe_push_updates_reraises_provider_not_implemented(
    hass: HomeAssistant,
) -> None:
    """subscribe_push_updates re-raises ProviderNotImplementedError, not swallowing it.

    Every other setup_push_subscription failure is logged and absorbed (the
    reconnect paths retry later), but ProviderNotImplementedError signals a
    programming error -- a provider claiming supports_push without
    overriding setup_push_subscription -- and must surface loudly instead
    of silently disabling push updates.
    """
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_push_not_implemented",
        config_entry=config_entry,
    )
    lock = _PushSetupRaisesLock(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    lock.push_setup_error = ProviderNotImplementedError(lock, "setup_push_subscription")

    with pytest.raises(ProviderNotImplementedError):
        lock.subscribe_push_updates()


async def test_subscribe_push_updates_logs_warning_on_unexpected_error(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """subscribe_push_updates logs and swallows an unexpected setup exception.

    Unlike LockDisconnected (expected, debug-logged, retried automatically)
    or ProviderNotImplementedError (re-raised), an unanticipated exception
    from setup_push_subscription must not crash the caller -- it is logged
    at warning level and the existing reconnect paths retry later.
    """
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_push_unexpected_error",
        config_entry=config_entry,
    )
    lock = _PushSetupRaisesLock(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    lock.push_setup_error = RuntimeError("unexpected driver failure")

    with caplog.at_level(logging.WARNING):
        lock.subscribe_push_updates()  # must not raise

    assert any(
        record.levelname == "WARNING"
        and "push subscription failed unexpectedly" in record.message
        for record in caplog.records
    )


async def test_config_entry_state_change_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Resubscribe and refresh when lock config entry reloads."""
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0
        lock.coordinator.async_request_refresh = AsyncMock()

        await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert lock.unsubscribe_calls == 1
        assert lock.subscribe_calls == 1
        lock.coordinator.async_request_refresh.assert_awaited()

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_connection_transition_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Resubscribe on reconnect and unsubscribe on disconnect."""
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0

        # The connection check the provider port runs on the provider's
        # cadence: each one lets the provider see its integration come and go.
        await lock.async_internal_is_reachable()

        lock.set_connected(False)
        await lock.async_internal_is_reachable()
        assert lock.unsubscribe_calls == 1

        lock.set_connected(True)
        await lock.async_internal_is_reachable()
        assert lock.subscribe_calls == 1

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_connection_transition_on_device_availability(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A device-level (node) outage drives the same resubscribe/refresh path.

    Recovery must be detected uniformly whether the outage was at the
    integration/transport layer or the device/node layer: with the
    integration still connected, toggling device availability alone must
    unsubscribe on the drop and resubscribe + refresh on recovery
    (issue #1257 recovery latency).
    """
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0

        # The connection check the provider port runs on the provider's
        # cadence: each one lets the provider see its integration come and go.
        await lock.async_internal_is_reachable()

        # Integration stays connected; only the node drops.
        lock.set_device_available(False)
        await lock.async_internal_is_reachable()
        assert lock.unsubscribe_calls == 1

        lock.set_device_available(True)
        lock.coordinator.async_request_refresh = AsyncMock()
        await lock.async_internal_is_reachable()
        await hass.async_block_till_done()
        assert lock.subscribe_calls == 1
        # The refresh asked for is a read of the lock by its worker.
        lock.coordinator.async_request_refresh.assert_awaited()

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_a_lock_back_from_a_device_outage_gets_its_new_pin_promptly(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for PR #1289: a lock back from a device-only outage is written within a connection check.

    The lock's device dropped off while its integration stayed loaded, as a
    Matter node does behind a running Matter server. Only the integration
    was watched for coming back, so recovery waited on the backoff's next
    probe -- minutes, doubling up to half an hour -- before the PIN changed
    meanwhile reached the lock, and the lock stayed unsubscribed until then.
    """
    with patch.dict(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {LOCK_DEVICE_DOMAIN: MockLCMPushLock},
    ):
        entry = current_entry("Device Outage", {1: "1234"})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_ON

    # A connection check sees the lock up before the outage.
    await async_advance_clock(
        hass, freezer, lock.connection_check_interval.total_seconds()
    )

    # The device drops off: the provider's own exchanges go unanswered, and
    # its availability check says so to the connection check.
    lock.set_device_available(False)
    outage = lock.fail(None, error=LockDisconnected)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_tick_until(
        hass,
        freezer,
        lambda: not worker.reachable,
        max_ticks=LIMIT + 3,
        what="the worker backing off the unreachable lock",
    )
    # Long enough for the probes to fail too, each doubling the backoff: the
    # next one is minutes away when the device comes back.
    await async_advance_clock(hass, freezer, 3 * BACKOFF_INITIAL_SECONDS)
    assert lock.codes[1] == "1234"
    assert lock.unsubscribe_calls == 1
    subscribed = lock.subscribe_calls

    outage.clear()
    lock.set_device_available(True)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        # A connection check, and a tick to write and one to confirm.
        max_ticks=int(lock.connection_check_interval / TICK_INTERVAL) + 3,
        what="slot 1 in sync once the device is back",
    )
    assert lock.codes[1] == "4321"
    assert lock.subscribe_calls > subscribed
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_async_call_service_raises_operation_failed_on_error(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_call_service wraps HA service errors as LockOperationFailed."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    async def failing_service(call):
        raise HomeAssistantError("Service failed")

    hass.services.async_register("test_domain", "failing_service", failing_service)

    with pytest.raises(
        LockOperationFailed, match="Service call test_domain.failing_service failed"
    ):
        await lock_provider.async_call_service("test_domain", "failing_service", {})

    hass.services.async_remove("test_domain", "failing_service")


async def test_async_call_service_propagates_non_ha_exceptions(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Non-HomeAssistantError exceptions must propagate, not become LockDisconnected.

    Wrapping programming errors (TypeError) or shutdown signals
    (asyncio.CancelledError) as LockDisconnected would trigger false
    "lock offline" issues, drift backoff, and push-resub loops.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    async def buggy_service(call):
        raise TypeError("programmer made a mistake")

    hass.services.async_register("test_domain", "buggy_service", buggy_service)

    with pytest.raises(TypeError, match="programmer made a mistake"):
        await lock_provider.async_call_service("test_domain", "buggy_service", {})

    hass.services.async_remove("test_domain", "buggy_service")


async def test_async_call_service_wraps_os_error_as_lock_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Test that async_call_service wraps OSError (e.g. ReadTimeout) as LockDisconnected.

    Integrations that don't wrap network errors in HomeAssistantError raise raw
    OSError subclasses (TimeoutError, ConnectionError).  These are transient and
    should be routed through the retry/backoff path, not treated as programming
    errors.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    async def timeout_service(call):
        raise TimeoutError("Read timed out")

    hass.services.async_register("test_domain", "timeout_service", timeout_service)

    with pytest.raises(
        LockDisconnected, match="Service call test_domain.timeout_service failed"
    ):
        await lock_provider.async_call_service("test_domain", "timeout_service", {})

    hass.services.async_remove("test_domain", "timeout_service")


async def test_lock_equality_with_non_baselock(hass: HomeAssistant):
    """Test that __eq__ returns False when comparing with non-BaseLock object."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_eq",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Comparing to non-BaseLock objects should return False
    assert lock != "not a lock"
    assert lock != 123
    assert (lock == None) is False  # noqa: E711 - Intentionally testing __eq__ with None
    assert lock != {"entity_id": lock_entity.entity_id}


async def test_lock_equality_with_same_entity_id(hass: HomeAssistant):
    """Two BaseLock instances wrapping the same lock entity are equal.

    __hash__ is defined by entity ID alone, so __eq__ must agree: any two
    BaseLock objects (even different provider classes or instances) that
    wrap the same lock.entity_id are the same logical lock.
    """
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_eq_same",
        config_entry=config_entry,
    )

    lock_a = BaseLock(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)
    lock_b = BaseLock(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)

    assert lock_a is not lock_b
    assert lock_a == lock_b
    assert lock_a == lock_a  # noqa: PLR0124 - Intentionally testing reflexive __eq__


async def test_fire_code_slot_event_fires_the_unified_event(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A provider-observed use fires the unified event.

    Every provider funnels through this one method, so this is the whole
    proof that the unified event reaches all of them.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)

    lock_provider.async_fire_code_slot_event(
        code_slot=1,
        to_locked=False,
    )
    await hass.async_block_till_done()

    assert len(unified) == 1
    # The whole payload, so a key added or dropped has to be deliberate. The
    # lock that observed the use is both source and target of it.
    assert unified[0].data == {
        ATTR_NAME: "test1",
        ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
        ATTR_CONFIG_ENTRY_TITLE: lock_code_manager_config_entry.title,
        ATTR_SOURCE: LOCK_1_ENTITY_ID,
        ATTR_TARGET: LOCK_1_ENTITY_ID,
        ATTR_CREDENTIAL_TYPE: CredentialType.PIN,
        # ``to_locked=False`` is the lock reporting that it opened.
        ATTR_OPERATION: CredentialOperation.UNLOCK,
    }


@pytest.mark.parametrize("wipe", ["remove", "unknown"])
async def test_fire_code_slot_event_names_the_user_from_the_configuration(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    wipe: str,
):
    """The unified event names the user even with the name entity gone.

    The name used to be read off the slot's name text entity, so disabling
    that entity announced an empty name on every use on every provider, and
    a reload announced ``unknown`` until the entity restored. The
    configuration is the thing that actually knows who holds the slot.

    The deprecated event keeps deriving ``code_slot_name`` from the entity:
    that payload is frozen.
    """
    name_entity_id = er.async_get(hass).async_get_entity_id(
        TEXT_DOMAIN,
        DOMAIN,
        build_slot_unique_id(lock_code_manager_config_entry.entry_id, 1, CONF_NAME),
    )
    assert name_entity_id
    if wipe == "remove":
        hass.states.async_remove(name_entity_id)
    else:
        hass.states.async_set(name_entity_id, STATE_UNKNOWN)

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)

    lock_provider.async_fire_code_slot_event(code_slot=1, to_locked=False)
    await hass.async_block_till_done()

    assert unified[0].data[ATTR_NAME] == "test1"


async def test_fire_code_slot_event_takes_no_attribution_arguments(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Attribution is not something a caller of this may override.

    A lock that observed a use is both ends of it. The ``use_credential``
    action fires the unified event itself rather than borrowing a lock's
    funnel, so nothing needs to redirect the attribution here.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    with pytest.raises(TypeError):
        lock_provider.async_fire_code_slot_event(
            code_slot=1,
            to_locked=False,
            source="sensor.keypad",
            target="cover.garage",
        )


async def test_fire_code_slot_event_announces_nothing_when_unattributed(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A use on a slot no entry manages announces nothing.

    Providers fire for every slot the lock reports, managed or not. The
    unified event says whose credential was used, and there is no user here
    to name, so nothing is announced. The lock-shaped event that used to
    carry these was removed in 6.0; the lock's own integration still
    reports them.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    unified = async_capture_events(hass, BUS_EVENT_CREDENTIAL_USED)

    lock_provider.async_fire_code_slot_event(
        code_slot=99,
        to_locked=False,
    )
    await hass.async_block_till_done()

    assert unified == []


async def test_setup_defers_push_subscription_when_entry_not_loaded(
    hass: HomeAssistant,
):
    """Test that async_setup defers push subscription when lock config entry isn't loaded."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # Create a mock lock config entry that's NOT loaded
    lock_config_entry = MockConfigEntry(domain="test", state=None)
    lock_config_entry.add_to_hass(hass)
    # Manually set state to NOT_LOADED
    object.__setattr__(lock_config_entry, "state", "not_loaded")

    lcm_config_entry = MockConfigEntry(domain=DOMAIN)
    lcm_config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_defer",
        config_entry=lock_config_entry,
    )

    # Create a mock lock with push support
    lock = MockLCMLockWithPush(
        hass,
        dev_reg,
        entity_reg,
        lock_config_entry,
        lock_entity,
    )
    lock.subscribe_calls = 0

    await lock.async_setup_internal(lcm_config_entry, _stand_in(lock))

    # Push subscription should be deferred (not called) since entry not loaded
    assert lock.subscribe_calls == 0

    await lock.async_unload(False)


async def test_async_setup_internal_creates_coordinator_when_setup_fails(
    hass: HomeAssistant,
):
    """Test that coordinator is created even when async_setup raises."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_setup_fail",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dev_reg,
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Make async_setup raise an exception
    with (
        patch.object(
            lock,
            "async_setup",
            side_effect=LockDisconnected("provider unavailable"),
        ),
    ):
        # Should not raise even though async_setup failed
        await lock.async_setup_internal(config_entry, _stand_in(lock))

    # Coordinator should still have been created
    assert lock.coordinator is not None
    # Setup complete should be signaled
    assert lock._setup_complete.is_set()
    # Setup should be marked as failed
    assert lock._setup_succeeded is False

    # Simulate reconnect: mock lock_config_entry as LOADED, async_setup succeeds
    mock_entry = MagicMock()
    mock_entry.state = ConfigEntryState.LOADED
    lock.lock_config_entry = mock_entry
    with patch.object(lock, "async_setup", return_value=None):
        await lock._async_on_integration_loaded()

    assert lock._setup_succeeded is True


async def test_a_lock_whose_setup_raised_comes_up_with_its_integration(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for PR #1048: a lock whose setup raised comes up with its integration.

    The lock's integration was down at startup, and the provider's setup
    raised. That ended the lock's setup before its coordinator existed, so
    its entities stayed unavailable after the integration came back, until
    the entry was reloaded by hand.
    """
    entry = current_entry("Setup Raised", {1: "4321"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(
        lambda lock: (
            lock.fail("setup", error=LockDisconnected),
            lock.fail("get", error=LockDisconnected),
        )
    ) as built:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    [down] = built
    per_lock = [in_sync_entity_id(hass, entry, 1), code_entity_id(hass, entry, 1)]
    await async_advance_sync_ticks(hass, freezer, 5)
    assert [hass.states.get(entity_id).state for entity_id in per_lock] == [
        STATE_UNAVAILABLE
    ] * 2
    lock = mock_lock(entry)
    assert "set_usercode" not in lock.service_calls

    for fault in down:
        fault.clear()
    assert await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(per_lock[0]).state == STATE_ON,
        # The refresh the lock's setup asks for waits out the debounce, then
        # the slot is compared, written and confirmed.
        max_ticks=int(REQUEST_REFRESH_DEFAULT_COOLDOWN / TICK_INTERVAL.total_seconds())
        + 5,
        what="slot 1 in sync once the lock's integration is back",
    )
    assert hass.states.get(per_lock[1]).state == "4321"
    assert lock.codes[1] == "4321"
    # Retried on the connection check while down, and again on the reload.
    assert len(lock.service_calls["setup"]) >= 2
    assert set(lock.service_calls["setup"]) == {(entry.entry_id,)}
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_on_integration_loaded_skips_when_no_config_entry(
    hass: HomeAssistant,
):
    """Test that _async_on_integration_loaded is a no-op when _lcm_config_entry is None."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock_no_entry", config_entry=config_entry
    )

    lock = BaseLock(hass, dev_reg, entity_reg, config_entry, lock_entity)
    # _lcm_config_entry is None (async_setup_internal never called)
    assert lock._lcm_config_entry is None
    # Should be a no-op, not raise
    await lock._async_on_integration_loaded()
    assert lock._setup_succeeded is False


async def test_on_integration_loaded_skips_when_entry_not_loaded(
    hass: HomeAssistant,
):
    """
    A not-yet-loaded provider entry short circuits before setup is attempted.

    The handler fires on any config entry state change, so it has to ignore
    the transitions that are not "the integration finished loading" -- probing
    a half-loaded integration would fail on the capability read and churn the
    degraded-lock machinery for nothing.
    """
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock_retry", config_entry=config_entry
    )

    lock = BaseLock(hass, dev_reg, entity_reg, config_entry, lock_entity)
    lock._lcm_config_entry = config_entry
    assert lock.lock_config_entry.state is not ConfigEntryState.LOADED

    with patch.object(lock, "async_setup") as setup:
        await lock._async_on_integration_loaded()

    # The point of the guard: no provider I/O was attempted at all.
    setup.assert_not_called()
    assert lock._setup_succeeded is False


async def test_on_integration_loaded_logs_debug_on_transient_reconnect_failure(
    hass: HomeAssistant,
) -> None:
    """A transient LockDisconnected/LockOperationFailed during reconnect setup is quiet.

    Unlike a structural LockCodeManagerProviderError (which reports a repair
    issue), a transport failure while re-running provider setup on the
    LOADED transition is expected and logged at debug only -- the next
    reconnect event tries again automatically. This requires the config
    entry to actually be in the LOADED state; a not-yet-loaded entry short
    circuits before the retry logic runs at all.
    """
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock_retry_transient", config_entry=config_entry
    )

    lock = BaseLock(hass, dev_reg, entity_reg, config_entry, lock_entity)
    lock._lcm_config_entry = config_entry

    loaded_entry = MagicMock()
    loaded_entry.state = ConfigEntryState.LOADED
    lock.lock_config_entry = loaded_entry

    with patch.object(
        lock, "async_setup", side_effect=LockOperationFailed("transient failure")
    ):
        await lock._async_on_integration_loaded()

    # The transient failure is absorbed: setup did not succeed, but it also
    # did not escalate to a repair issue or leave _setup_running stuck.
    assert lock._setup_succeeded is False
    assert lock._setup_running is False


async def test_a_reconnect_whose_probe_times_out_rearms_the_retry(
    hass: HomeAssistant,
) -> None:
    """
    A bounded probe that times out on reconnect is a connectivity failure.

    The deferred flag is the only thing that arms another attempt, and
    `_async_run_provider_setup` clears it on the way in -- so an escaping
    TimeoutError would spend the retry and leave the lock un-set-up until
    someone reloaded the integration by hand. It would also skip the
    coordinator refresh and push subscription that run after the try, leaving
    a push provider unsubscribed for the life of the entry.
    """
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "reconnect_probe_timeout", config_entry=config_entry
    )
    lock = MockNativeUserLock(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    lock._lcm_config_entry = config_entry
    lock._min_operation_delay = 0

    loaded_entry = MagicMock()
    loaded_entry.state = ConfigEntryState.LOADED
    lock.lock_config_entry = loaded_entry

    async def _never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(
            type(lock), "operation_timeout_seconds", property(lambda _s: 0.05)
        ),
        patch.object(lock, "async_get_capabilities", _never_returns),
        patch.object(lock, "subscribe_push_updates") as subscribed,
        patch.object(type(lock), "supports_push", property(lambda _s: True)),
    ):
        # Returns rather than raising out of the reconnect handler.
        await asyncio.wait_for(lock._async_on_integration_loaded(), timeout=5)

    assert lock._setup_deferred is True
    assert lock._setup_running is False
    assert lock._setup_succeeded is False
    # The work after the try still ran.
    subscribed.assert_called_once()


class MockNativeUserLock(MockLCMLock):
    """Mock lock that opts into the native-user capability validation."""

    @property
    def supports_native_users(self) -> bool:
        """Return True so async_setup_internal runs the capability probe."""
        return True


def _pin_capabilities() -> LockCapabilities:
    """Return capabilities advertising PIN support."""
    return LockCapabilities(
        supports_user_management=True,
        max_users=10,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=10, min_length=4, max_length=8, supports_learn=False
            )
        },
        max_user_name_length=10,
    )


def _make_base_test_lock(
    hass: HomeAssistant, unique_id: str, lock_cls: type[MockLCMLock] = MockLCMLock
) -> MockLCMLock:
    """Create a mock lock wired to a registry entry and config entry."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", unique_id, config_entry=config_entry
    )
    return lock_cls(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)


async def test_setup_internal_defers_provider_io_when_integration_not_connected(
    hass: HomeAssistant,
):
    """Setup skips the capability probe and async_setup while the integration is down.

    Regression test for issue #1321: the base class must not touch the
    provider integration before it is connected — a provider whose probe
    raises an untranslated error (e.g. zwave_js's node lookup ValueError)
    would otherwise crash setup and permanently drop the lock.
    """
    lock = _make_base_test_lock(hass, "test_lock_defer_io", MockNativeUserLock)
    lock.set_connected(False)

    with (
        patch.object(lock, "async_get_capabilities") as mock_caps,
        patch.object(lock, "async_setup") as mock_setup,
    ):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))

    mock_caps.assert_not_called()
    mock_setup.assert_not_called()
    assert lock._setup_succeeded is False
    # Degraded, not dropped: coordinator and recovery listener exist.
    assert lock.coordinator is not None
    assert lock._config_entry_state_unsub is not None
    assert lock._setup_complete.is_set()

    await lock.async_unload(False)


async def test_a_lock_not_connected_at_setup_gets_entities_that_come_up_with_it(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #225: a lock not connected at setup has entities, which come up with it.

    Setup waited for the lock's integration to connect ("not connected yet,
    waiting N s") before creating anything, so an entry set up before its
    lock was ready had no entities at all, and the dashboards built on them
    failed. They exist from the start, unavailable, and come up -- and the
    slot is written -- once the lock connects.
    """
    entry = current_entry("Not Connected", {1: "4321"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(lambda lock: lock.set_connected(False)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = mock_lock(entry)
    per_lock = [in_sync_entity_id(hass, entry, 1), code_entity_id(hass, entry, 1)]
    await async_advance_sync_ticks(hass, freezer, 5)
    assert [hass.states.get(entity_id).state for entity_id in per_lock] == [
        STATE_UNAVAILABLE
    ] * 2
    assert lock.call_order == []

    lock.set_connected(True)
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(per_lock[0]).state == STATE_ON,
        # A connection check, then the refresh it asks for, a write, and its
        # confirmation.
        max_ticks=int(lock.connection_check_interval / TICK_INTERVAL) + 3,
        what="slot 1 in sync once the lock connects",
    )
    assert hass.states.get(per_lock[1]).state == "4321"
    assert lock.codes[1] == "4321"
    assert lock.service_calls["setup"] == [(entry.entry_id,)]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_complete_set_when_setup_raises_unexpectedly(
    hass: HomeAssistant,
):
    """_setup_complete is set even when setup escapes with an unexpected error.

    Waiters in async_wait_for_setup (shared lock instances across config
    entries) must not hang forever when setup fails structurally.
    """
    lock = _make_base_test_lock(hass, "test_lock_setup_complete")

    with patch.object(lock, "async_setup", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))

    assert lock._setup_complete.is_set()


async def test_on_integration_loaded_runs_deferred_capability_validation(
    hass: HomeAssistant,
):
    """The reconnect path runs the capability validation setup deferred.

    When initial setup ran degraded (integration down), the LOADED
    transition must run the same probe + PIN validation as initial setup
    before marking the provider set up.
    """
    lock = _make_base_test_lock(hass, "test_lock_deferred_caps", MockNativeUserLock)
    lock.set_connected(False)

    caps_mock = AsyncMock(return_value=_pin_capabilities())
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))
        assert lock._setup_succeeded is False
        caps_mock.assert_not_called()

        # Integration comes up; simulate the LOADED transition callback.
        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    caps_mock.assert_awaited_once()
    assert lock._setup_succeeded is True

    await lock.async_unload(False)


async def test_setup_internal_structural_failure_degrades_and_recovers_on_reload(
    hass: HomeAssistant,
):
    """Initial-setup validation failure degrades the lock instead of dropping it.

    A structural failure (no PIN support / zero usable slots) during initial
    setup used to propagate, making ``_async_setup_new_locks`` pop the lock
    from runtime data with no retry path and no UI surface — a re-included
    Z-Wave lock whose interview completed asleep stayed invisible until a
    full reload. Initial setup now matches the reconnect path: the failure is
    logged, the coordinator and recovery listener are still created, and the
    LOADED transition re-runs validation so the lock recovers once the
    underlying condition is fixed (e.g. after a re-interview).
    """
    lock = _make_base_test_lock(
        hass, "test_lock_structural_degraded", MockNativeUserLock
    )

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))

        caps_mock.assert_awaited_once()
        assert lock._setup_succeeded is False
        # Degraded, not dropped: coordinator and recovery listener exist.
        assert lock.coordinator is not None
        assert lock._config_entry_state_unsub is not None
        assert lock._setup_complete.is_set()

        # The lock's condition is fixed (e.g. re-interview repopulated the
        # slot count); the next LOADED transition revalidates and recovers.
        caps_mock.return_value = _pin_capabilities()
        # Validation failure must invalidate the capability cache; a
        # degenerate-but-successful read would otherwise be served to every
        # revalidation, making recovery impossible.
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    assert lock._setup_succeeded is True

    await lock.async_unload(False)


async def test_a_lock_failing_validation_at_setup_stays_in_the_entry_and_recovers(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for PR #1344: a lock failing validation at setup stays, visibly, in the entry.

    A lock reporting no room for PIN codes -- a Z-Wave lock re-included and
    interviewed while asleep -- failed the check setup makes and was
    dropped from the entry: no entities, no repair, only a log line, and
    nothing to bring it back short of a reload once the lock was fixed.
    """
    issue_reg = ir.async_get(hass)
    setup_issue = per_lock_issue_id("lock_setup_failed", LOCK_1_ENTITY_ID)
    entry = current_entry("Failed Validation", {1: "4321"})
    entry.add_to_hass(hass)
    with when_mock_lock_built(
        lambda lock: setattr(lock, "capabilities", NO_PIN_CAPABILITIES)
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer, 5)
    assert issue_reg.async_get_issue(DOMAIN, setup_issue) is not None
    # Its entities are there, unavailable: nothing is read from or written to
    # a lock its provider has not set up.
    assert hass.states.get(in_sync).state == STATE_UNAVAILABLE
    assert hass.states.get(code_entity_id(hass, entry, 1)).state == STATE_UNAVAILABLE
    lock = mock_lock(entry)
    assert "set_usercode" not in lock.service_calls

    # Re-interviewed: the lock reports its room again, and its integration
    # reloads. The same lock instance is checked again: the probe that failed
    # was not kept, so this answer is the one the check sees.
    lock.capabilities = PIN_CAPABILITIES
    assert await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
    await hass.async_block_till_done()
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=int(REQUEST_REFRESH_DEFAULT_COOLDOWN / TICK_INTERVAL.total_seconds())
        + 3,
        what="slot 1 in sync once the lock passes its check",
    )
    assert issue_reg.async_get_issue(DOMAIN, setup_issue) is None
    assert mock_lock(entry) is lock
    assert lock.codes[1] == "4321"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_validation_failure_raises_and_clears_repair_issue(
    hass: HomeAssistant,
):
    """A structural setup failure surfaces a repair issue; recovery clears it.

    The actionable error message (e.g. zwave_js's "re-interview the lock")
    used to land only in the log, addressed to a user who never sees it.
    The repair carries it into the UI, and revalidation succeeding on the
    provider integration's LOADED transition dismisses it automatically.
    """
    lock = _make_base_test_lock(hass, "test_lock_repair_issue", MockNativeUserLock)
    issue_reg = ir.async_get(hass)
    issue_id = f"lock_setup_failed_{lock.lock.entity_id}"

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))

        issue = issue_reg.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_placeholders
        assert "PIN credential" in issue.translation_placeholders["error"]

        # The lock's condition is fixed; the LOADED transition revalidates
        # and the repair dismisses itself.
        caps_mock.return_value = _pin_capabilities()
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    await lock.async_unload(False)


async def test_reconnect_validation_failure_raises_repair_issue(
    hass: HomeAssistant,
):
    """Deferred validation failing on the reconnect path raises the repair too."""
    lock = _make_base_test_lock(
        hass, "test_lock_repair_issue_reconnect", MockNativeUserLock
    )
    lock.set_connected(False)
    issue_reg = ir.async_get(hass)
    issue_id = f"lock_setup_failed_{lock.lock.entity_id}"

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))
        # Deferred: no probe ran, so no issue yet.
        assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await lock.async_unload(False)


async def test_unload_permanently_clears_setup_failed_repair_issue(
    hass: HomeAssistant,
):
    """Removing a lock permanently clears its setup-failed repair issue.

    A non-permanent unload (reload, HA restart) must keep the persistent
    issue so it survives until the failure actually resolves.
    """
    lock = _make_base_test_lock(hass, "test_lock_repair_unload", MockNativeUserLock)
    issue_reg = ir.async_get(hass)
    issue_id = f"lock_setup_failed_{lock.lock.entity_id}"

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    with patch.object(
        lock, "async_get_capabilities", AsyncMock(return_value=no_pin_caps)
    ):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await lock.async_unload(False)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await lock.async_unload(True)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


async def test_on_integration_loaded_rejects_lock_without_pin_support(
    hass: HomeAssistant,
):
    """Deferred validation failing (no PIN support) leaves setup unsuccessful.

    The structural failure must not escape into the reconnect task; the
    provider simply never reaches the set-up state.
    """
    lock = _make_base_test_lock(hass, "test_lock_deferred_no_pin", MockNativeUserLock)
    lock.set_connected(False)

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))

        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    caps_mock.assert_awaited_once()
    assert lock._setup_succeeded is False

    await lock.async_unload(False)


async def test_config_entry_state_listener_ignores_same_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that config entry state listener ignores transitions to same state."""
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Same State"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        # Reset counters
        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0

        # Manually trigger the state listener with the same state (LOADED -> LOADED)
        # This simulates the edge case where the state hasn't actually changed
        lock._last_entry_state = mock_lock_config_entry.state
        # The listener should no-op since state didn't change

        # Verify no calls were made
        assert lock.subscribe_calls == 0
        assert lock.unsubscribe_calls == 0

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_is_device_available_default_returns_true(hass: HomeAssistant):
    """Test that base class is_device_available() returns True by default."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_device_available",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Default implementation returns True
    assert await lock.async_is_device_available() is True


async def test_async_unload_cancels_in_flight_reconnect_task(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """async_unload cancels the reconnect task spawned by the state listener."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Plant an in-flight reconnect task that takes a long time. async_unload
    # should cancel it before returning so a late
    # coordinator.async_request_refresh() cannot fire after teardown.
    started = asyncio.Event()

    async def slow_reconnect() -> None:
        started.set()
        await asyncio.sleep(60)

    lock._reconnect_task = hass.async_create_task(slow_reconnect())
    await started.wait()
    assert not lock._reconnect_task.done()

    await lock.async_unload(False)

    assert lock._reconnect_task is None


async def test_async_unload_reraises_cancelled_when_caller_itself_is_cancelled(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """async_unload propagates CancelledError when its own caller is cancelled.

    async_unload cancels its in-flight reconnect task and awaits it, which
    always raises CancelledError for that reason alone -- that case is
    expected and absorbed. But if the *caller's own task* is what's being
    cancelled (e.g. Home Assistant shutting down while a lock is mid
    unload), the CancelledError must propagate rather than being silently
    swallowed, or the outer cancellation would never take effect.
    """
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    started = asyncio.Event()

    async def slow_reconnect() -> None:
        started.set()
        await asyncio.sleep(60)

    lock._reconnect_task = hass.async_create_task(slow_reconnect())
    await started.wait()

    unload_task = hass.async_create_task(lock.async_unload(False))
    # Let unload_task run up to its first await point: it synchronously
    # cancels the reconnect task, then suspends on `await reconnect_task`.
    # Cancelling unload_task itself right there means the CancelledError it
    # receives is attributable to its own cancellation, not just the
    # reconnect task's.
    await asyncio.sleep(0)
    unload_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await unload_task


async def test_handle_state_change_supersedes_prior_reconnect_task(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A second LOADED transition cancels and replaces the prior reconnect task."""
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=BASE_CONFIG,
            unique_id="Mock Title Reconnect Supersede",
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

        # Plant a long-running reconnect task to mimic a slow reconnect that
        # has not yet completed.
        async def slow_reconnect() -> None:
            await asyncio.sleep(60)

        prior_task = hass.async_create_task(slow_reconnect())
        lock._reconnect_task = prior_task

        # Drive a second LOADED transition through the state listener. The
        # listener should cancel the prior task and store the new one.
        lock._last_entry_state = ConfigEntryState.NOT_LOADED
        mock_lock_config_entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()

        assert prior_task.cancelled() or prior_task.done()
        assert lock._reconnect_task is not None
        assert lock._reconnect_task is not prior_task

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_supersede_drains_prior_reconnect_exception(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A superseded reconnect task that failed before cancellation is logged, not orphaned."""
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=BASE_CONFIG,
            unique_id="Mock Title Drain Reconnect",
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

        boom = RuntimeError("simulated prior reconnect failure")

        async def failed_reconnect() -> None:
            raise boom

        prior_task = hass.async_create_task(failed_reconnect())
        # Let the task run to completion with its exception.
        await asyncio.sleep(0)
        assert prior_task.done()
        assert prior_task.exception() is boom

        lock._reconnect_task = prior_task

        # Drive a LOADED transition through the state listener; the supersede
        # path must drain the failed prior task and log its exception.
        lock._last_entry_state = ConfigEntryState.NOT_LOADED
        with caplog.at_level(logging.WARNING):
            mock_lock_config_entry.mock_state(hass, ConfigEntryState.LOADED)
            await hass.async_block_till_done()

        assert any(
            record.exc_info is not None and record.exc_info[1] is boom
            for record in caplog.records
            if record.levelname == "WARNING"
        )

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_async_unload_logs_reconnect_task_exception(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """async_unload logs warnings when the reconnect task raises non-CancelledError on shutdown."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    boom = RuntimeError("simulated reconnect cleanup failure")
    started = asyncio.Event()

    async def stubborn_reconnect() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise boom from None

    lock._reconnect_task = hass.async_create_task(stubborn_reconnect())
    await started.wait()
    assert not lock._reconnect_task.done()

    with caplog.at_level(logging.WARNING):
        await lock.async_unload(False)

    assert lock._reconnect_task is None
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "WARNING"
    )


# =============================================================================
# Push unsub registry
# =============================================================================


async def test_clear_push_unsubs_invokes_and_empties_registry(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """_clear_push_unsubs invokes every registered unsub and clears the list."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    calls: list[str] = []
    lock._register_push_unsub(lambda: calls.append("a"))
    lock._register_push_unsub(lambda: calls.append("b"))
    assert len(lock._push_unsubs) == 2

    lock._clear_push_unsubs()

    assert calls == ["a", "b"]
    assert lock._push_unsubs == []


async def test_clear_push_unsubs_continues_on_individual_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A raising unsub does not prevent later unsubs from running."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    second_called = [False]

    def boom_unsub() -> None:
        raise RuntimeError("unsub failed")

    def second_unsub() -> None:
        second_called[0] = True

    lock._register_push_unsub(boom_unsub)
    lock._register_push_unsub(second_unsub)

    with caplog.at_level(logging.WARNING):
        lock._clear_push_unsubs()

    assert second_called[0]
    assert lock._push_unsubs == []


async def test_clear_push_unsubs_safe_when_unsub_reregisters(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """An unsub that re-registers must not corrupt iteration or loop forever."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    calls: list[str] = []

    def reregistering_unsub() -> None:
        calls.append("first")
        # Re-register self after the original snapshot was taken; the
        # re-registration must survive clear() but must not be called
        # again in this _clear_push_unsubs invocation.
        lock._register_push_unsub(reregistering_unsub)

    lock._register_push_unsub(reregistering_unsub)
    lock._register_push_unsub(lambda: calls.append("second"))

    lock._clear_push_unsubs()

    assert calls == ["first", "second"]
    assert lock._push_unsubs == [reregistering_unsub]


# =============================================================================
# Sequence lock context manager
# =============================================================================


async def test_serialize_sequence_serializes_concurrent_blocks(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """_serialize_sequence forces concurrent multi-step ops to run one at a time."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    in_section = [0]
    max_overlap = [0]
    order: list[int] = []

    async def section(tag: int) -> None:
        async with lock._serialize_sequence():
            in_section[0] += 1
            max_overlap[0] = max(max_overlap[0], in_section[0])
            await asyncio.sleep(0)
            order.append(tag)
            in_section[0] -= 1

    await asyncio.gather(section(1), section(2), section(3))

    assert max_overlap[0] == 1
    assert sorted(order) == [1, 2, 3]


async def test_max_slot_prefers_what_the_lock_advertises(hass: HomeAssistant) -> None:
    """A lock that says how many slots it has is believed."""
    lock = _bare_lock(hass, "test_lock_max_slot_advertised")

    with patch.object(
        lock,
        "async_get_capabilities",
        AsyncMock(
            return_value=LockCapabilities(
                supports_user_management=False,
                max_users=30,
                credential_types={
                    CredentialType.PIN: CredentialTypeCapability(
                        num_slots=30,
                        min_length=4,
                        max_length=8,
                        supports_learn=False,
                    )
                },
            )
        ),
    ):
        assert await lock.async_get_max_slot() == 30


@pytest.mark.parametrize(
    "capabilities",
    [
        LockCapabilities(
            supports_user_management=False, max_users=0, credential_types={}
        ),
        LockCapabilities(
            supports_user_management=False,
            max_users=0,
            credential_types={
                CredentialType.PIN: CredentialTypeCapability(
                    num_slots=0, min_length=4, max_length=8, supports_learn=False
                )
            },
        ),
    ],
)
async def test_max_slot_falls_back_when_the_lock_will_not_say(
    hass: HomeAssistant, capabilities: LockCapabilities
) -> None:
    """A capacity of zero means "I will not say", not "nowhere to write".

    ``bounded_slot_count`` answers None there so a write is never refused
    over an unreadable capability. A search needs to know the difference
    between that and a real range, so this reports having no opinion and
    lets the caller decide how far to look.
    """
    lock = _bare_lock(hass, f"test_lock_max_slot_{id(capabilities)}")

    with patch.object(
        lock, "async_get_capabilities", AsyncMock(return_value=capabilities)
    ):
        assert await lock.async_get_max_slot() is None


async def test_max_slot_falls_back_when_capabilities_cannot_be_read(
    hass: HomeAssistant,
) -> None:
    """A lock that cannot be asked has no opinion, and must not raise."""
    lock = _bare_lock(hass, "test_lock_max_slot_unreadable")

    with patch.object(
        lock,
        "async_get_capabilities",
        AsyncMock(side_effect=LockDisconnected("asleep")),
    ):
        assert await lock.async_get_max_slot() is None


async def test_deferred_setup_is_retried_once_the_lock_becomes_reachable(
    hass: HomeAssistant,
):
    """
    An integration that was loaded all along sends no LOADED transition.

    Setup defers when the provider integration is not connected, and the
    only thing that re-ran it was that integration reaching LOADED. A bridge
    whose entry was loaded before Lock Code Manager, and whose discovery data
    merely had not been replayed yet, never transitions -- so the lock stayed
    un-set-up for the rest of the run, and sync, which is gated on that,
    never wrote a code to it again.

    The connection check is what catches it, and the first reachable
    observation is enough: there may be no transition to wait for either,
    since that check can find the lock reachable the very first time it runs.
    """
    lock = _make_base_test_lock(hass, "test_lock_late_discovery")
    lock.set_connected(False)
    await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))
    assert lock.provider_setup_succeeded is False

    lock.set_connected(True)
    loaded_entry = MagicMock()
    loaded_entry.state = ConfigEntryState.LOADED
    lock.lock_config_entry = loaded_entry

    assert await lock.async_internal_is_reachable() is True
    await hass.async_block_till_done()

    assert lock.provider_setup_succeeded is True

    await lock.async_unload(False)


async def test_a_deferred_retry_that_hits_a_blip_gets_another_one(
    hass: HomeAssistant,
):
    """
    One transient failure during the retry must not consume the only retry.

    The deferred flag was cleared before the retried setup ran, so a
    LockDisconnected on the way through spent it: nothing armed another
    attempt, and the lock stayed un-set-up -- sync gated off it, no writes --
    until the whole integration was reloaded by hand. A connectivity failure
    is precisely the condition this retry exists for, so it re-arms.
    """
    lock = _make_base_test_lock(hass, "test_lock_blip_during_retry")
    lock.set_connected(False)
    await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))
    assert lock.provider_setup_succeeded is False

    lock.set_connected(True)
    loaded_entry = MagicMock()
    loaded_entry.state = ConfigEntryState.LOADED
    lock.lock_config_entry = loaded_entry

    with patch.object(
        lock, "async_setup", AsyncMock(side_effect=LockDisconnected("blip"))
    ):
        assert await lock.async_internal_is_reachable() is True
        await hass.async_block_till_done()
    assert lock.provider_setup_succeeded is False

    assert await lock.async_internal_is_reachable() is True
    await hass.async_block_till_done()

    assert lock.provider_setup_succeeded is True

    await lock.async_unload(False)


async def test_a_lock_that_was_set_up_is_not_set_up_again_on_every_check(
    hass: HomeAssistant,
):
    """
    The retry is armed by setup being skipped, not by it having failed.

    A lock whose setup ran and failed validation has a diagnosis and its own
    revalidation path; re-probing it from every connection check would put a
    capability read on the wire every thirty seconds for a lock that is known
    to be degraded.
    """
    lock = _make_base_test_lock(hass, "test_lock_validated", MockNativeUserLock)
    lock.set_connected(False)

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    with patch.object(
        lock, "async_get_capabilities", AsyncMock(return_value=no_pin_caps)
    ):
        await lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock))

        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        # The first reachable observation spends the one deferred retry.
        assert await lock.async_internal_is_reachable() is True
        await hass.async_block_till_done()

        with patch.object(lock, "_async_start_reconnect") as retry:
            assert await lock.async_internal_is_reachable() is True
            await hass.async_block_till_done()

    retry.assert_not_called()
    assert lock.provider_setup_succeeded is False

    await lock.async_unload(False)


class _NativeUserOwnerResolutionLock(MockNativeUserLock):
    """
    A native-user lock that uses the BASE's owner resolution.

    ``MockLCMLock`` overrides ``async_clear_usercode`` with a slot-only
    shortcut, which is what most tests want and what makes it useless for
    exercising the two-pass owner lookup. This delegates back to the base so
    the resolution under test is the real one.
    """

    async def async_get_capabilities(self) -> LockCapabilities:
        """Advertise PIN support so the base's write paths engage."""
        return _pin_capabilities()

    async def async_clear_usercode(
        self, code_slot: int, *, adopt_untagged: bool = True
    ) -> bool:
        """Resolve the owner the way a real native-user provider does."""
        return await BaseLock.async_clear_usercode(
            self, code_slot, adopt_untagged=adopt_untagged
        )


def _lock_user(user_id: int, name: str | None, pin_slot: int) -> User:
    """Build a lock-side user owning one PIN credential."""
    return User(
        user_id=user_id,
        name=name,
        credentials=(Credential(type=CredentialType.PIN, slot=pin_slot, state=None),),
    )


async def test_releasing_a_slot_does_not_delete_an_untagged_users_credential(
    hass: HomeAssistant,
):
    """
    Releasing never adopts an untagged owner.

    The clear path may: the slot is still LCM's, adoption is how a pre-tag
    install is taken over, and the write that follows corrects a wrong guess.
    Releasing has the opposite premise -- the slot is no longer LCM's, and
    nothing follows -- so an untagged user holding a PIN at that index, which
    is as likely to be one somebody else made, must be left alone.
    """
    lock = _make_base_test_lock(
        hass, "release_untagged", _NativeUserOwnerResolutionLock
    )
    deleted = AsyncMock(return_value=True)
    with (
        patch.object(
            lock,
            "async_get_users",
            AsyncMock(
                return_value=[
                    _lock_user(99, "Alice", 3),  # untagged: not ours to delete
                ]
            ),
        ),
        patch.object(lock, "async_delete_credential", deleted),
    ):
        await lock.async_release_managed_slot(3)

    deleted.assert_not_called()


async def test_releasing_a_slot_deletes_the_credential_of_its_tagged_user(
    hass: HomeAssistant,
):
    """The user LCM demonstrably created is still torn down."""
    lock = _make_base_test_lock(hass, "release_tagged", _NativeUserOwnerResolutionLock)
    deleted = AsyncMock(return_value=True)
    with (
        patch.object(
            lock,
            "async_get_users",
            AsyncMock(
                return_value=[
                    _lock_user(99, make_tagged_name(3, "Raman"), 3),
                ]
            ),
        ),
        patch.object(lock, "async_delete_credential", deleted),
    ):
        await lock.async_release_managed_slot(3)

    deleted.assert_called_once()
    assert deleted.call_args.args[0].user_id == 99


async def test_clearing_a_managed_slot_still_adopts_an_untagged_owner(
    hass: HomeAssistant,
):
    """The upgrade path this scoping must not break: a clear may still adopt."""
    lock = _make_base_test_lock(hass, "clear_adopts", _NativeUserOwnerResolutionLock)
    deleted = AsyncMock(return_value=True)
    with (
        patch.object(
            lock,
            "async_get_users",
            AsyncMock(
                return_value=[
                    _lock_user(99, "Alice", 3),
                ]
            ),
        ),
        patch.object(lock, "async_delete_credential", deleted),
    ):
        await lock.async_clear_usercode(3)

    deleted.assert_called_once()
    assert deleted.call_args.args[0].user_id == 99


async def test_a_wedged_provider_setup_does_not_park_the_entry(hass: HomeAssistant):
    """
    `async_setup` is under the deadline as well as the capability probe.

    It is device I/O on several providers -- akuvox and schlage run an
    unmanaged-code tagging pass in it, zwave-js-ui subscribes -- and setup
    runs while Home Assistant holds the entry's lock, so an unbounded call
    here parks the entry in `setup_in_progress` and takes reload and remove
    with it.
    """
    lock = _make_base_test_lock(hass, "wedged_provider_setup")
    lock._min_operation_delay = 0

    async def _never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(
            type(lock), "operation_timeout_seconds", property(lambda _s: 0.05)
        ),
        patch.object(lock, "async_setup", _never_returns),
        patch.object(lock, "async_get_usercodes", AsyncMock(return_value={})),
    ):
        await asyncio.wait_for(
            lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock)),
            timeout=5,
        )

    # Degraded, not parked: setup returned and the retry path is armed.
    assert lock._setup_succeeded is False
    assert lock._setup_complete.is_set()
    assert lock._setup_deferred is True


# The codes the mock lock starts with, and more for further slots.
_SLOT_PINS = {1: "1234", 2: "5678", 3: "3333", 4: "4444"}


async def _async_set_up_slots(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, count: int
) -> MockConfigEntry:
    """Set up an entry with ``count`` slots on lock 1, run until they are in sync."""
    slot_nums = range(1, count + 1)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                slot_num: {
                    CONF_NAME: f"test{slot_num}",
                    CONF_PIN: _SLOT_PINS[slot_num],
                    CONF_ENABLED: True,
                }
                for slot_num in slot_nums
            },
        },
        unique_id=f"{count} slots",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await async_tick_until(
        hass,
        freezer,
        lambda: all(_in_sync(hass, entry, slot_num) for slot_num in slot_nums),
        # The slots the lock lacks are written one after another.
        max_ticks=2 * count + 3,
        what="every slot in sync",
    )
    return entry


def _in_sync(hass: HomeAssistant, entry: MockConfigEntry, slot_num: int) -> bool:
    """Return whether a slot's in-sync sensor on lock 1 is on."""
    return hass.states.get(in_sync_entity_id(hass, entry, slot_num)).state == STATE_ON


async def test_a_write_that_never_returns_lets_go_of_its_slot_and_the_lock(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #1523: a write that never returns is cut off at the deadline.

    The write held the lock's turn and its slot's syncing state for good:
    the slot never retried, and every other slot's write queued behind it.
    Cut off, it gives the turn back: both writes then land.
    """
    entry = await _async_set_up_slots(hass, freezer, 2)
    lock = mock_lock(entry)
    wedge = lock.hang("set", 1, times=1)
    await async_set_slot_pin(hass, entry, 1, "1111")
    await async_set_slot_pin(hass, entry, 2, "2222")
    await async_advance_sync_ticks(hass, freezer, settle=False)
    await wedge.async_wait_held()
    wedged_at = time.monotonic()
    await async_advance_sync_ticks(hass, freezer, 5, settle=False)
    assert sync_status(hass, in_sync_entity_id(hass, entry, 1)) == "syncing"
    assert lock.codes == {1: "1234", 2: "5678"}

    await async_advance_across_deadline(
        hass,
        freezer,
        started_at=wedged_at,
        deadline=base_module.OPERATION_TIMEOUT,
        held=lambda: wedge.held == 1,
        what="the write holding the lock's turn",
    )
    await async_tick_until(
        hass,
        freezer,
        lambda: _in_sync(hass, entry, 1) and _in_sync(hass, entry, 2),
        max_ticks=3,
        what="both slots in sync after the cut-off",
    )

    assert lock.codes == {1: "1111", 2: "2222"}
    assert sorted(lock.service_calls["set_usercode"]) == [
        (1, "1111", "test1"),
        (2, "2222", "test2"),
    ]
    # Nothing reached the lock while the write held its turn. The time limit
    # is on the whole sync, which began with the read before the write.
    sync_began = max(at for at in lock.run_times if at < wedged_at)
    assert [
        at
        for at in lock.run_times
        if wedged_at <= at < sync_began + base_module.OPERATION_TIMEOUT
    ] == []
    await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_does_not_hang_on_a_capability_probe_that_never_answers(
    hass: HomeAssistant,
):
    """
    The probe is bounded too, not just the read that follows it.

    It runs before the coordinator exists and goes through neither
    `_execute_rate_limited` nor any budget of the provider's own --
    `MatterLock.async_get_capabilities` awaits `get_lock_info` bare. Bounding
    only the initial read would leave setup parking on the call before it.
    """
    lock = _make_base_test_lock(hass, "probe_hangs", MockNativeUserLock)
    lock._min_operation_delay = 0

    async def _never_returns(*_args, **_kwargs):
        await asyncio.Event().wait()

    with (
        patch.object(
            type(lock), "operation_timeout_seconds", property(lambda _s: 0.05)
        ),
        patch.object(lock, "async_get_capabilities", _never_returns),
        patch.object(lock, "async_get_usercodes", AsyncMock(return_value={})),
    ):
        await asyncio.wait_for(
            lock.async_setup_internal(lock.lock_config_entry, _stand_in(lock)),
            timeout=5,
        )

    # Degraded, not dropped: setup finished and the retry path is armed.
    assert lock._setup_succeeded is False
    assert lock._setup_complete.is_set()
    # The flag IS the retry path. `_handle_connection_transition` checks
    # nothing else, and the LOADED transition the state listener waits for
    # never arrives for an integration that was already loaded and then went
    # quiet -- so without this the lock never gets set up again.
    assert lock._setup_deferred is True


async def test_a_lock_whose_setup_never_answered_once_is_set_up_and_written_later(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for PRs #1526 and #1527: a setup that never answers is given up, then retried.

    The provider's setup had no deadline, so a lock whose integration went
    quiet stayed in setup for good: none of its slots' entities, nothing
    written (#1527). Once it had one, a setup cut off at its deadline was
    never tried again: the lock stayed un-set-up, and the sync, waiting on
    that, never wrote to it (#1526).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_PIN: "4321", CONF_ENABLED: True}},
        },
        unique_id="Setup Wedge",
    )
    entry.add_to_hass(hass)
    # Loaded and unloaded once first: setting it up again then reaches the
    # lock without waiting on the executor to import its platforms. A restart
    # while the lock's integration is wedged.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    with when_mock_lock_built(lambda lock: lock.hang("setup", times=1)) as wedges:
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        await async_run_until_quiet()
        [wedge] = wedges
        await wedge.async_wait_held()
        await async_advance_across_deadline(
            hass,
            freezer,
            started_at=time.monotonic(),
            deadline=base_module.OPERATION_TIMEOUT,
            held=lambda: wedge.held == 1,
            what="the lock's setup",
        )
    assert setup.done()
    assert entry.state is ConfigEntryState.LOADED
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    # The first read after the cut-off finds the lock answering, and that is
    # what sets it up again.
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=8,
        what="slot 1 in sync once the lock is set up",
    )
    assert lock.codes[1] == "4321"
    assert len(lock.service_calls["setup"]) == 2
    await hass.config_entries.async_unload(entry.entry_id)


async def test_slots_queued_behind_long_writes_are_not_a_lock_gone_offline(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Regression for #1535: waiting for the lock's turn is not the lock failing to answer.

    Three writes each take most of their deadline and then land, so slot
    4's write and the polls queue behind them for longer than they may
    wait, again and again. Each wait that ran out was charged as a
    disconnect, so a lock answering every command was marked unreachable:
    its slots suspended. The lock never stops answering here.
    """
    entry = await _async_set_up_slots(hass, freezer, 4)
    lock = mock_lock(entry)
    new_pins = {1: "1111", 2: "2222", 3: "3330", 4: "4440"}
    in_syncs = [in_sync_entity_id(hass, entry, slot_num) for slot_num in new_pins]
    statuses: list[str | None] = []

    @callback
    def _record(event: Event[EventStateChangedData]) -> None:
        if (state := event.data["new_state"]) is not None:
            statuses.append(state.attributes.get(ATTR_SYNC_STATUS))

    unsub = async_track_state_change_event(hass, in_syncs, _record)
    # Most of a deadline each, so none of the writes is cut off.
    hold = base_module.OPERATION_TIMEOUT * 0.9
    long_writes = [
        lock.slow("set", slot_num, seconds=hold, times=1) for slot_num in (1, 2, 3)
    ]
    for slot_num, pin in new_pins.items():
        await async_set_slot_pin(hass, entry, slot_num, pin)
    await async_advance_sync_ticks(hass, freezer, settle=False)
    await async_advance_clock(hass, freezer, 3 * hold, settle=False)
    assert [long_write.hits for long_write in long_writes] == [1, 1, 1]
    await async_tick_until(
        hass,
        freezer,
        lambda: all(_in_sync(hass, entry, slot_num) for slot_num in new_pins),
        max_ticks=5,
        what="every slot in sync",
    )
    unsub()

    assert lock.codes == new_pins
    assert "suspended" not in statuses
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, per_lock_issue_id("lock_offline", LOCK_1_ENTITY_ID)
        )
        is None
    )
    await hass.config_entries.async_unload(entry.entry_id)


class TestOperationBudget:
    """The deadline is sized to the exchanges a call walks (issue #1528)."""

    def test_no_declared_budget_means_the_flat_floor(self, hass: HomeAssistant):
        lock = _make_base_test_lock(hass, "budget_flat")
        assert lock.per_exchange_budget is None
        assert lock._operation_budget(1) == base_module.OPERATION_TIMEOUT
        assert lock._operation_budget(250) == base_module.OPERATION_TIMEOUT

    def test_a_walk_scales_the_floor_and_never_narrows_it(self, hass: HomeAssistant):
        lock = _make_base_test_lock(hass, "budget_scaled")
        with patch.object(type(lock), "per_exchange_budget", 90.0):
            floor = base_module.OPERATION_TIMEOUT
            assert lock._operation_budget(1) == floor  # one exchange: the floor
            assert lock._operation_budget(10) == 900.0  # ten slow slots
            assert lock._operation_budget(0) == floor
