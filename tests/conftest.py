"""Fixtures for lock_code_manager tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack
from datetime import timedelta
import logging
import os
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import settings as hypothesis_settings
from probatio import to_field_list
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_config_flow,
    mock_platform,
)

from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.config_entries import ConfigEntriesFlowManager, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowManager, FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.json import json_bytes
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.domain.models import SyncState
from custom_components.lock_code_manager.domain.sync import SlotSyncManager
from custom_components.lock_code_manager.providers import INTEGRATIONS_CLASS_MAP
from custom_components.lock_code_manager.providers._base import BaseLock

from .common import (
    BASE_CONFIG,
    LiveMockCalendars,
    MockLCMLock,
    async_setup_mock_lock_entry,
    count_executor_work,
    forget_all_faults,
    release_all_faults,
    sync_manager_of,
)

pytest_plugins = ["pytest_homeassistant_custom_component"]

# Hypothesis profiles: "dev" keeps the full-suite run within its
# seconds budget; CI opts into deeper exploration via HYPOTHESIS_PROFILE=ci.
# deadline=None in both: per-example deadlines flake under the HA test
# harness's timing noise and CI runner variance.
hypothesis_settings.register_profile("dev", max_examples=15, deadline=None)
hypothesis_settings.register_profile(
    "ci", max_examples=200, deadline=None, print_blob=True
)
hypothesis_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

TEST_DOMAIN = "test"


@pytest.fixture
def aiohttp_client(aiohttp_client, socket_enabled):
    """Return aiohttp_client and allow opening sockets."""
    return aiohttp_client


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None]:
    """
    Release every mock-lock fault before any fixture is torn down.

    A ``hang`` or ``drop_reply`` fault left installed holds a call the sync
    managers' stop then waits on, and on a frozen clock that wait never
    ends. Fixture teardown is too late to let go of it -- an autouse fixture
    is torn down after the config entries -- so this runs before the
    fixtures' finalizers. Teardown runs whether the test body ran or its
    setup failed, so a fault a fixture installed is let go of too. A fault
    installed during teardown is only forgotten once it ends, not released:
    by then the event loop a held call waits on may be closed, and the
    next test must not see the fault either way.
    """
    release_all_faults()
    try:
        return (yield)
    finally:
        forget_all_faults()


@pytest.fixture(autouse=True)
def counted_executor_work() -> Generator[None]:
    """Count executor work, so the clock helpers never step past a thread."""
    with count_executor_work():
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


@pytest.fixture(autouse=True)
def auto_setup_mock_lock():
    """Automatically set up MockLCMLock for all tests."""
    with patch.dict(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock, **INTEGRATIONS_CLASS_MAP},
    ):
        yield


def _assert_reaches_the_frontend(result: FlowResult) -> None:
    """
    Fail unless ``result`` goes out over HTTP the way the flow views send it.

    The views prepare a result (``_prepare_result_json``: a create entry
    without its ``data``, ``context`` and the entry it made, a schema as a
    field list) and encode it with ``json_bytes``. Done here with the same
    public calls, so errors and description placeholders that do not encode
    fail too, not only the schema.
    """
    data = {
        key: value
        for key, value in result.items()
        if key not in ("data", "context", "result")
    }
    if "data_schema" in data:
        schema = data["data_schema"]
        data["data_schema"] = (
            []
            if schema is None
            else to_field_list(schema, custom_serializer=cv.custom_serializer)
        )
    json_bytes(data)


@pytest.fixture(autouse=True)
def assert_flow_results_serialize() -> Generator[None]:
    """
    Send every flow result through the conversion the HTTP layer makes.

    A flow driven through ``async_configure`` gets the schema object back
    untouched, so a validator the frontend has no representation for reaches
    a real browser as an unknown error and never a failing test (#1495).
    ``to_field_list`` with ``cv.custom_serializer`` is the exact call the
    views make, so what passes here is what renders; anything else would
    let a form pass the test and fail the browser.

    Checked at the flow managers' ``async_init`` and ``async_configure``,
    where the views get their results, rather than on the handlers: an
    ``AbortFlow`` raised in a step becomes a result without passing through
    any handler method, and so does every other result the manager builds.
    """

    def checked(cls: type[FlowManager], method: str) -> Any:
        """Wrap one manager class's own ``method``."""
        original = vars(cls)[method]

        async def _converted(self, *args: Any, **kwargs: Any) -> FlowResult:
            result = await original(self, *args, **kwargs)
            _assert_reaches_the_frontend(result)
            return result

        return patch.object(cls, method, _converted)

    with ExitStack() as stack:
        for cls in (FlowManager, ConfigEntriesFlowManager):
            for method in ("async_init", "async_configure"):
                if method in vars(cls):
                    stack.enter_context(checked(cls, method))
        yield


@pytest.fixture(autouse=True)
def assert_no_deprecation_reports() -> Generator[None]:
    """
    Fail a test that makes Home Assistant report this integration.

    A deprecated helper called from a custom integration only logs, while the
    same call from a test frame raises. So production code can carry one past
    a green suite and reach users as a warning that becomes a hard failure on
    the removal version -- issue #1595, where #1540 migrated every
    ``async_get_device`` call and a block merged from an older branch brought
    one back. Watching the log is what closes that gap, because the log is the
    only place the warning ever appeared.

    Home Assistant deduplicates these by call site for the life of the
    process, so the first test to reach a reintroduced call is the one that
    fails; later tests over the same line stay silent.
    """
    reports: list[str] = []

    class _Collect(logging.Handler):
        """Keep every frame report, so the filtering below is ours to choose."""

        def emit(self, record: logging.LogRecord) -> None:
            reports.append(record.getMessage())

    logger = logging.getLogger("homeassistant.helpers.frame")
    handler = _Collect()
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
    # Reports name the integration they blame, and the mock integrations these
    # tests register are not ours to fix.
    if ours := [report for report in reports if f"'{DOMAIN}'" in report]:
        pytest.fail("\n".join(ours))


def pytest_configure(config: pytest.Config) -> None:
    """Register this suite's markers."""
    config.addinivalue_line(
        "markers",
        "paced: keep the providers' real minimum delay between lock operations",
    )


@pytest.fixture(autouse=True)
def disable_rate_limiting(request: pytest.FixtureRequest):
    """
    Disable BaseLock rate limiting for most tests to speed them up.

    We patch __post_init__ to set _min_operation_delay=0.0 on new instances.
    A test marked ``paced`` keeps the real delay, so the pacing it checks is
    the one the providers ship with; it needs the ``freezer`` to get through
    the waits.
    """
    if request.node.get_closest_marker("paced") is not None:
        yield
        return
    original_post_init = getattr(BaseLock, "__post_init__", None)

    def patched_post_init(self):
        if original_post_init:
            original_post_init(self)
        self._min_operation_delay = 0.0

    with patch(
        "custom_components.lock_code_manager.providers._base.BaseLock.__post_init__",
        patched_post_init,
    ):
        yield


@pytest.fixture
async def mqtt_teardown(hass: HomeAssistant, mqtt_client_mock) -> AsyncGenerator[None]:
    """
    Cancel the MQTT client's misc periodic timer after the test.

    HA's MQTT client cancels that timer only on socket close, which the paho
    client mock never fires. Fire it here so teardown does not trip the
    lingering-timer check in verify_cleanup. Any test that sets up the real
    MQTT integration via ``mqtt_mock`` needs this.
    """
    yield
    mqtt_client_mock.on_socket_close(
        mqtt_client_mock, None, MagicMock(fileno=MagicMock(return_value=-1))
    )
    await hass.async_block_till_done()


class MockFlow(ConfigFlow):
    """Test flow."""


@pytest.fixture(name="mock_config_flow")
def config_flow_fixture(hass: HomeAssistant) -> Generator[None]:
    """Mock config flow."""
    mock_platform(hass, f"{TEST_DOMAIN}.config_flow")

    with mock_config_flow(TEST_DOMAIN, MockFlow):
        yield


@pytest.fixture(name="setup_lovelace_ui")
async def setup_lovelace_ui_fixture(hass: HomeAssistant, config: dict[str, Any]):
    """Set up Lovelace in UI mode."""
    assert await async_setup_component(hass, LL_DOMAIN, {"lovelace": config})
    yield


@pytest.fixture(name="mock_lock_config_entry")
async def mock_lock_config_entry_fixture(hass: HomeAssistant, mock_config_flow):
    """Set up lock entities using an entity platform."""
    config_entry = await async_setup_mock_lock_entry(hass)

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.fixture(name="lock_code_manager_config_entry")
async def lock_code_manager_config_entry_fixture(hass: HomeAssistant):
    """Set up the config entry for lock code manager."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.fixture(name="mock_calendars")
def mock_calendars_fixture(
    hass: HomeAssistant, mock_lock_config_entry
) -> LiveMockCalendars:
    """Return the mock lock integration's calendars, the current ones on each use."""
    return LiveMockCalendars(hass)


def get_in_sync_entity_obj(hass: HomeAssistant, entity_id: str):
    """
    Get the in-sync entity object for a given entity ID.

    Returns the entity object from the entity component registry.
    """
    entity_component = hass.data["entity_components"]["binary_sensor"]
    entity_obj = entity_component.get_entity(entity_id)
    assert entity_obj is not None
    return entity_obj


def sync_manager_for(hass: HomeAssistant, entity_id: str) -> SlotSyncManager:
    """Return the PIN sync manager behind an in-sync entity, by entity ID."""
    return sync_manager_of(get_in_sync_entity_obj(hass, entity_id))


async def async_trigger_sync_tick(
    hass: HomeAssistant, entity_id: str, set_dirty: bool = True
) -> None:
    """
    Manually trigger a sync tick for an in-sync entity.

    Encapsulates the pattern of forcing an entity into a tickable state and
    triggering an immediate tick, useful for testing tick-based sync behavior
    without waiting for the natural 2-second tick interval.

    Drives the sync manager's internals: it forces ``_state`` and calls
    ``_async_tick`` directly, so it proves nothing about the scheduled tick
    and pins the test to the manager's private shape. New tests use
    ``tests.common.async_advance_sync_ticks``, which only moves time.
    """
    manager = sync_manager_for(hass, entity_id)
    if set_dirty and manager._state not in (SyncState.LOADING, SyncState.OUT_OF_SYNC):
        manager._state = SyncState.OUT_OF_SYNC
    await manager._async_tick()
    await hass.async_block_till_done()


async def async_initial_tick(hass: HomeAssistant, entity_id: str) -> None:
    """
    Trigger initial tick for entity setup.

    During entity setup, the initial tick in async_start may fail if dependent
    entities (active, code sensor) are not yet registered. This helper triggers
    a tick to complete initial state loading, but only if the entity hasn't
    been initialized yet (state is LOADING).
    """
    manager = sync_manager_for(hass, entity_id)
    if manager._state is SyncState.LOADING:
        await manager._async_tick()
        await hass.async_block_till_done()


async def async_trigger_sync_tick_for_manager(
    hass: HomeAssistant, sync_manager, set_dirty: bool = True
) -> None:
    """
    Manually trigger a sync tick for a sync manager object.

    Useful when you already have the entity object or need to trigger
    ticks on multiple managers in a loop. Drives internals the same way
    ``async_trigger_sync_tick`` does; new tests use
    ``tests.common.async_advance_sync_ticks``.
    """
    if set_dirty and sync_manager._state not in (
        SyncState.LOADING,
        SyncState.OUT_OF_SYNC,
    ):
        sync_manager._state = SyncState.OUT_OF_SYNC
    await sync_manager._async_tick()
    await hass.async_block_till_done()


async def async_advance_time(hass: HomeAssistant, delta: timedelta) -> None:
    """
    Advance HA's mock clock by ``delta`` and settle pending work.

    Replaces the brittle ``async_fire_time_changed + async_block_till_done``
    pair. ``async_track_time_interval`` (used for the sync tick and the
    coordinator update interval) schedules its callback as a background
    task via ``async_run_hass_job(..., background=True)``. The default
    ``async_block_till_done`` only waits for foreground tasks, so chains
    spawned by a timer-fired tick can outlive the wait and leak across
    assertions. This helper passes ``wait_background_tasks=True`` to
    cover them.

    It fires the timers that would be due, but moves neither clock:
    ``time.monotonic`` (pending-write deadlines, rate limiting) and
    ``dt_util.utcnow`` (breaker windows) stand still. A test that needs time
    to pass for real uses ``tests.common.async_advance_sync_ticks`` with the
    ``freezer`` fixture.
    """
    async_fire_time_changed(hass, dt_util.utcnow() + delta)
    await hass.async_block_till_done(wait_background_tasks=True)
