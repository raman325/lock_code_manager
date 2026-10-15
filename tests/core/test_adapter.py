"""The provider adapter: an existing provider, as the worker sees it."""

import asyncio
from collections.abc import Collection
import contextlib
from datetime import timedelta
from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager import providers
from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.core.adapter import (
    ProviderPort,
    _Coordinator,
    lock_error,
    reading,
)
from custom_components.lock_code_manager.core.slot import Holds, Repair, Seen, Status
from custom_components.lock_code_manager.core.worker import (
    BACKOFF_AFTER,
    BACKOFF_MAX,
    BACKOFF_MIN,
    OFFLINE_AFTER,
    ErrorKind,
    LockError,
    LockWorker,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockBusy,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
    LockOperationUnsupported,
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._mqtt import BaseMqttLock
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

from ..common import (
    NO_PIN_CAPABILITIES,
    MockLCMLock,
    MockLCMPushLock,
    when_mock_lock_built,
)
from .test_worker import FakeLock, _started, rig_factory  # noqa: F401  (rig_factory is a fixture)

SALT = b"salt"


def _build(hass: HomeAssistant, cls: type[MockLCMLock] = MockLCMLock) -> MockLCMLock:
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    # The lock's own integration is up.
    config_entry.mock_state(hass, ConfigEntryState.LOADED)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "front", config_entry=config_entry
    )
    return cls(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)


async def _port(
    hass: HomeAssistant, cls: type[MockLCMLock] = MockLCMLock, *, setup: bool = True
) -> tuple[ProviderPort, LockWorker, MockLCMLock]:
    lock = _build(hass, cls)
    port = ProviderPort(lock, name_for=lambda slot: f"User {slot}")
    port.worker = LockWorker(port, SALT, gap=0.0)
    if setup:
        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        await port.async_setup(entry)
    return port, port.worker, lock


@pytest.fixture
async def ports(hass: HomeAssistant):
    """Build ports on mock locks; unload and stop them after the test."""
    built: list[tuple[ProviderPort, LockWorker]] = []

    async def _make(cls: type[MockLCMLock] = MockLCMLock, *, setup: bool = True):
        port, worker, lock = await _port(hass, cls, setup=setup)
        built.append((port, worker))
        return port, worker, lock

    yield _make
    for port, worker in built:
        await worker.stop(grace=0.1)
        await port.async_unload(False)


# What a provider says ------------------------------------------------------------


def test_readings() -> None:
    """Empty, a code it will not show, and a code it shows."""
    assert reading(SlotCredential.empty()) is Seen.EMPTY
    assert reading(SlotCredential.unreadable()) is Seen.MASKED
    assert reading(SlotCredential.known("1234")) == Holds("1234")


@pytest.mark.parametrize(
    ("error", "clearing", "kind"),
    [
        (CodeRejectedError(1, "lock.front", "no"), False, ErrorKind.VALUE_REFUSED),
        (DuplicateCodeError(1, "lock.front"), False, ErrorKind.VALUE_REFUSED),
        (CodeRejectedError(1, "lock.front", "no"), True, ErrorKind.SLOT_REFUSED),
        (LockOperationUnsupported("bad slot"), False, ErrorKind.SLOT_REFUSED),
        (LockOperationFailed("api down"), False, ErrorKind.NO_ANSWER),
        (LockDisconnected("gone"), False, ErrorKind.NO_ANSWER),
        (LockBusy("busy"), True, ErrorKind.NO_ANSWER),
    ],
)
def test_what_a_provider_error_means(error, clearing, kind) -> None:
    """The provider's errors, as what each does to a slot."""
    assert (mapped := lock_error(error, clearing=clearing)) is not None
    assert mapped.kind is kind


def test_anything_else_a_provider_raises_is_its_bug() -> None:
    """Not a LockError: the worker's provider-bug path."""
    assert lock_error(ProviderNotImplementedError(None, "x"), clearing=False) is None


# Reading -------------------------------------------------------------------------


async def test_a_read_takes_what_the_provider_says_about_each_slot(ports) -> None:
    """Only the slots asked about; a code it will not show is masked."""
    port, _, lock = await ports()
    lock.codes = {1: "1234", 5: "5555"}
    lock.write_only = {3}

    assert await port.read([1, 2, 3]) == {
        1: Holds("1234"),
        2: Seen.EMPTY,
        3: Seen.MASKED,
    }


async def test_a_read_errors_as_the_worker_knows_them(ports) -> None:
    """A provider that cannot reach the lock is no answer."""
    port, _, lock = await ports()
    lock.fail("get", error=LockDisconnected)

    with pytest.raises(LockError) as caught:
        await port.read([1])
    assert caught.value.kind is ErrorKind.NO_ANSWER


async def test_a_read_past_the_providers_budget_is_no_answer(ports) -> None:
    """Bounded by the provider's own budget for the walk."""
    port, _, lock = await ports()
    lock.hang("get")
    with patch.object(
        MockLCMLock, "operation_timeout_seconds", new_callable=PropertyMock
    ) as budget:
        budget.return_value = 0.01
        with pytest.raises(LockError) as caught:
            await port.read([1])

    assert caught.value.kind is ErrorKind.NO_ANSWER


class _FreshOnlyOnHardRefresh(MockLCMLock):
    """A provider whose plain read is a cache, like Z-Wave JS's."""

    fresh_read_is_hard_refresh = True


async def test_a_provider_whose_plain_read_is_a_cache_is_read_by_hard_refresh(
    ports,
) -> None:
    """A write is checked against what the lock says, never a cache."""
    port, _, lock = await ports(_FreshOnlyOnHardRefresh)
    await port.read([1])

    assert lock.service_calls["hard_refresh_codes"] == [([1],)]


def test_only_zwave_js_reads_by_hard_refresh() -> None:
    """Hard refresh renames codes on Schlage and Akuvox, and drops virtual writes."""
    assert ZWaveJSLock.fresh_read_is_hard_refresh
    for cls in providers.INTEGRATIONS_CLASS_MAP.values():
        if cls is not ZWaveJSLock:
            assert not cls.fresh_read_is_hard_refresh, cls


class _Silent(MockLCMLock):
    """A bridge that can tell the lock said nothing about some slots."""

    silent: frozenset[int] = frozenset()

    async def async_get_usercodes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        codes = await super().async_get_usercodes(slots)
        self.last_silent_slots = self.silent
        return codes


async def test_a_slot_the_lock_said_nothing_about_is_silent(ports) -> None:
    """Left out of the reading, not folded into a masked code."""
    port, _, lock = await ports(_Silent)
    lock.silent = frozenset({2})

    assert await port.read([1, 2]) == {1: Holds("1234")}


class _Foreign(MockLCMLock):
    """A lock with users: some PINs sit under users with no slot tag."""

    foreign: frozenset[int] = frozenset()

    async def async_get_usercodes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        codes = await super().async_get_usercodes(slots)
        self.last_foreign_slots = self.foreign
        return codes


async def test_someone_else_s_code_at_a_slot_s_index_is_not_the_slot_s(
    ports,
) -> None:
    """The slot reads empty; the code is kept as one nobody here manages (#1695)."""
    port, _, lock = await ports(_Foreign)
    lock.foreign = frozenset({2})

    assert await port.read([1, 2]) == {1: Holds("1234"), 2: Seen.EMPTY}
    assert port.others[2] == Holds("5678")


async def test_a_turned_off_slot_holding_someone_else_s_code_is_in_sync(
    ports,
) -> None:
    """Nothing to clear: no write, no tries, no hold (#1695)."""
    _, worker, lock = await ports(_Foreign)
    lock.foreign = frozenset({2})
    worker.want(2, None)
    worker.start()
    await worker.settled()

    assert worker.status(2) == (Status.IN_SYNC, None)
    assert lock.codes[2] == "5678"


async def test_a_read_the_lock_said_nothing_to_is_no_answer(ports) -> None:
    """Once it has answered reads: silence about everything is a lock gone quiet."""
    port, _, lock = await ports(_Silent)
    await port.read([1])
    lock.silent = frozenset({1, 2})

    with pytest.raises(LockError) as caught:
        await port.read([1, 2])
    assert caught.value.kind is ErrorKind.NO_ANSWER


async def test_a_lock_that_never_answered_a_read_is_silent_not_gone(ports) -> None:
    """A write-only converter: silence is its answer, and its writes are trusted."""
    port, _, lock = await ports(_Silent)
    lock.silent = frozenset({1, 2})

    assert await port.read([1, 2]) == {}


# Writing -------------------------------------------------------------------------


async def test_a_write_goes_on_with_the_slots_name(ports) -> None:
    """A set names the user it is for; a clear clears."""
    port, _, lock = await ports()
    await port.write(3, "3333")
    await port.write(1, None)

    assert lock.codes == {2: "5678", 3: "3333"}
    assert lock.service_calls["set_usercode"] == [(3, "3333", "User 3")]


@pytest.mark.parametrize(
    ("pin", "kind"),
    [("9999", ErrorKind.VALUE_REFUSED), (None, ErrorKind.SLOT_REFUSED)],
)
async def test_a_refused_write(ports, pin, kind) -> None:
    """A refused PIN turns the slot off; a refused clear holds it."""
    port, _, lock = await ports()
    lock.refuse("set" if pin else "clear", 1)

    with pytest.raises(LockError) as caught:
        await port.write(1, pin)
    assert caught.value.kind is kind


async def test_a_providers_bug_on_a_write_reaches_the_worker_as_is(ports) -> None:
    """Not a LockError: the worker charges it to the slot."""
    port, _, lock = await ports()
    lock.fail("set", error=RuntimeError("bug"))

    with pytest.raises(RuntimeError):
        await port.write(1, "9999")


async def test_nothing_is_written_before_the_provider_is_set_up(ports) -> None:
    """The worker hears no answer and backs off until setup succeeds."""
    port, _, lock = await ports(setup=False)

    with pytest.raises(LockError) as caught:
        await port.write(3, "3333")
    assert caught.value.kind is ErrorKind.NO_ANSWER
    assert not caught.value.sent
    assert 3 not in lock.codes


async def test_a_slot_past_the_locks_capacity_is_refused_unsent(ports) -> None:
    """Told apart from an unreachable lock (#1398): the slot holds."""
    port, _, lock = await ports()
    with patch.object(
        type(lock),
        "_assert_slot_within_capacity",
        side_effect=LockOperationUnsupported("slot 40 is past 30"),
    ):
        with pytest.raises(LockError) as caught:
            await port.write(40, "4040")

    assert caught.value.kind is ErrorKind.SLOT_REFUSED
    assert not caught.value.sent
    assert 40 not in lock.codes


# Through the worker -----------------------------------------------------------------


async def test_a_worker_syncs_a_provider_through_the_port(ports) -> None:
    """Read first, write what differs, confirm, done."""
    _, worker, lock = await ports()
    worker.want(1, "1234")
    worker.want(3, "3333")
    worker.start()
    await worker.settled()

    assert lock.codes[3] == "3333"
    assert worker.status(1) == worker.status(3) == (Status.IN_SYNC, None)
    assert [call[0] for call in lock.service_calls["set_usercode"]] == [3]


async def test_a_refused_pin_turns_the_slot_off(ports) -> None:
    """The lock's refusal reaches the integration as a disabled slot."""
    port, worker, lock = await ports()
    disabled: list[int] = []
    worker.on_disable = disabled.append
    lock.refuse("set", 3)
    worker.want(3, "3333")
    worker.start()
    await worker.settled()

    assert worker.status(3) == (Status.SUSPENDED, Repair.SLOT_DISABLED)
    assert disabled == [3]


# Pushes and the stand-in coordinator ---------------------------------------------------


async def test_a_providers_echo_of_its_own_write_is_not_a_push(ports) -> None:
    """Only the lock reports: an echo stands in for nothing it said."""
    port, worker, lock = await ports(MockLCMPushLock)
    worker.want(2, None)
    worker.want(3, "3333")
    pushed: list[tuple[int, object]] = []
    worker.push = lambda slot, value: pushed.append((slot, value))  # type: ignore[method-assign]
    await port.write(3, "3333")
    lock._push_credential_update(2, SlotCredential.empty())

    assert pushed == [(2, Seen.EMPTY)]


async def test_setup_subscribes_the_provider_to_its_pushes(ports) -> None:
    """The provider's own lifecycle runs, on the stand-in coordinator."""
    _, _, lock = await ports(MockLCMPushLock)

    assert lock.provider_setup_succeeded
    assert lock.subscribe_calls == 1
    assert lock.service_calls["get_usercodes"] == []  # the worker reads first


async def test_the_provider_asks_whether_a_pin_is_wanted(ports) -> None:
    """Some locks call a slot available after a PIN lands; the provider asks."""
    _, worker, lock = await ports()
    worker.want(3, "3333")
    worker.want(4, None)

    assert lock._expects_pin(3)
    assert not lock._expects_pin(4)
    assert not lock._expects_pin(5)


async def test_a_providers_refresh_request_is_a_read(ports) -> None:
    """After a reconnect or a lock's event: every tracked slot is read."""
    _, worker, lock = await ports()
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    reads = len(lock.service_calls["get_usercodes"])

    assert lock.coordinator is not None
    await lock.coordinator.async_request_refresh()
    async with asyncio.timeout(5):
        while len(lock.service_calls["get_usercodes"]) == reads:
            await asyncio.sleep(0)


async def test_an_exchange_a_provider_starts_goes_in_the_locks_turn(ports) -> None:
    """An event's follow-up: answered through the worker, the lock's error as raised."""
    _, worker, lock = await ports()
    worker.start()
    await worker.settled()
    assert lock.coordinator is not None
    tasks: list[asyncio.Task | None] = []

    async def _ask() -> str:
        tasks.append(asyncio.current_task())
        return "answer"

    async def _fail() -> None:
        raise LockOperationFailed("busy")

    assert await lock.coordinator.async_in_turn(_ask) == "answer"
    assert tasks == [worker._task]  # run by the worker, not its caller
    with pytest.raises(LockOperationFailed, match="busy"):
        await lock.coordinator.async_in_turn(_fail)


async def test_a_providers_bug_in_an_exchange_it_starts_is_logged_and_raised(
    ports, caplog: pytest.LogCaptureFixture
) -> None:
    """An error the worker cannot read as the lock's: logged as the provider's bug."""
    _, worker, lock = await ports()
    worker.start()
    await worker.settled()
    assert lock.coordinator is not None

    async def _bug() -> None:
        raise LockCodeManagerProviderError("oops")

    with pytest.raises(LockCodeManagerProviderError, match="oops"):
        await lock.coordinator.async_in_turn(_bug)
    assert "Lock provider failed" in caplog.text


async def test_an_exchange_a_provider_starts_after_the_worker_stops_is_not_sent(
    ports,
) -> None:
    """Once the lock's worker has stopped, nothing a provider starts reaches it."""
    _, worker, lock = await ports()
    worker.start()
    await worker.settled()
    await worker.stop(grace=0.1)
    assert lock.coordinator is not None
    asked: list[bool] = []

    async def _ask() -> None:
        asked.append(True)

    with pytest.raises(LockDisconnected):
        await lock.coordinator.async_in_turn(_ask)
    assert not asked


def test_the_stand_in_answers_everything_a_provider_asks_its_coordinator() -> None:
    """Every ``coordinator.<name>`` in provider code exists on the stand-in."""
    asked = {
        name
        for path in Path(providers.__file__).parent.glob("*.py")
        for name in re.findall(r"coordinator\.([a-z_]+)", path.read_text())
    }

    assert asked
    stand_in = _Coordinator(ProviderPort.__new__(ProviderPort))
    for name in asked:
        assert hasattr(stand_in, name), name


async def test_silence_from_an_earlier_read_is_not_carried_over(ports) -> None:
    """A read that never reached the bridge's slot walk says nothing about silence."""
    port, _, lock = await ports(_Silent)
    lock.silent = frozenset({2})
    await port.read([1, 2])
    _Silent.async_get_usercodes = MockLCMLock.async_get_usercodes  # type: ignore[method-assign]
    try:
        assert await port.read([1, 2]) == {1: Holds("1234"), 2: Holds("5678")}
    finally:
        del _Silent.async_get_usercodes


async def test_a_push_after_the_write_is_back_is_the_lock(ports) -> None:
    """Only the echo during the write is dropped; the lock's later word counts."""
    port, worker, lock = await ports(MockLCMPushLock)
    worker.want(3, "3333")
    pushed: list[tuple[int, object]] = []
    worker.push = lambda slot, value: pushed.append((slot, value))  # type: ignore[method-assign]
    await port.write(3, "3333")
    lock._confirm_slot(3, SlotCredential.empty())

    assert pushed == [(3, Seen.EMPTY)]


@pytest.mark.parametrize(
    ("push", "drift", "interval"),
    [
        (False, None, (timedelta(minutes=1), None)),
        (False, timedelta(hours=1), (timedelta(minutes=1), timedelta(hours=1))),
        (True, None, (None, None)),
        (True, timedelta(hours=1), (None, timedelta(hours=1))),
    ],
)
async def test_polls_come_at_the_providers_cadence(
    hass: HomeAssistant, push, drift, interval
) -> None:
    """A lock that does not push is scanned; the drift check runs regardless."""
    port, _, lock = await _port(hass, MockLCMPushLock, setup=False)
    lock._supports_push = push
    lock._hard_refresh_interval = drift

    assert port._cadence() == interval


async def test_disabled_polling_skips_the_scan_not_the_drift_check(
    hass: HomeAssistant, ports
) -> None:
    """ "Disable polling" spares the drift check, which catches keypad changes."""
    port, worker, lock = await ports()
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    port.polling = lambda: False
    reads = len(lock.service_calls["get_usercodes"])

    port._on_scan(dt_util.utcnow())
    await hass.async_block_till_done()
    assert len(lock.service_calls["get_usercodes"]) == reads

    port._on_poll(dt_util.utcnow())
    async with asyncio.timeout(5):
        while len(lock.service_calls["get_usercodes"]) == reads:
            await asyncio.sleep(0)


async def test_the_connection_check_lets_the_provider_see_its_integration(
    ports,
) -> None:
    """It drives the provider's resubscribing; a failure there is only logged."""
    port, _, lock = await ports()
    with patch.object(
        type(lock), "async_internal_is_reachable", side_effect=LockDisconnected("x")
    ) as check:
        await port._check_connection(dt_util.utcnow())

    check.assert_called_once()


async def test_an_mqtt_bridge_records_the_slots_its_lock_said_nothing_about() -> None:
    """The bridges fold silence into unreadable; the adapter needs it apart."""
    bridge = SimpleNamespace(_reads_have_succeeded=False, lock=SimpleNamespace())

    async def _read_slot(slot: int) -> SlotCredential | None:
        return None if slot == 2 else SlotCredential.known("1111")

    await BaseMqttLock._async_read_slots(
        bridge,  # type: ignore[arg-type]
        [1, 2],
        _read_slot,
        transport_failure="went unanswered",
    )

    assert bridge.last_silent_slots == {2}


# An outage the provider calls a failure ----------------------------------------------


async def test_an_outage_on_reads_and_writes_holds_no_slot(ports) -> None:
    """Providers wrap an API outage as a failed operation: the lock backs off."""
    _, worker, lock = await ports()
    lock.fail("get", error=LockOperationFailed)
    lock.fail("set", error=LockOperationFailed)
    worker.want(3, "3333")
    worker.start()
    await worker.settled()

    assert worker.status(3)[1] is None
    assert not worker.reachable


async def test_writes_failing_while_reads_answer_hold_the_slot(ports) -> None:
    """The read-back answers in the same sync: the failed write is the slot's."""
    _, worker, lock = await ports()
    lock.fail("set", 3, error=LockOperationFailed)
    worker.want(3, "3333")
    worker.start()
    async with asyncio.timeout(5):
        while worker.status(3)[0] is not Status.SUSPENDED:
            worker.request_sync()
            await worker.settled()

    assert worker.status(3) == (Status.SUSPENDED, Repair.CANT_SET)
    assert worker.reachable


class _NoConnectionCheck(MockLCMPushLock):
    """Like Z-Wave JS: no connection check, no poll."""

    @property
    def connection_check_interval(self) -> timedelta | None:
        return None


async def test_a_deferred_setup_is_retried_by_the_next_write(
    hass: HomeAssistant, ports
) -> None:
    """Nothing else asks a provider with no connection check whether it is up."""
    with when_mock_lock_built(
        lambda lock: lock.fail("setup", error=TimeoutError, times=1)
    ):
        port, _, lock = await ports(_NoConnectionCheck)
    assert not lock.provider_setup_succeeded

    with contextlib.suppress(LockError):
        await port.write(3, "3333")
    await hass.async_block_till_done()

    assert lock.provider_setup_succeeded


async def test_the_poll_reads_the_lock(hass: HomeAssistant, ports) -> None:
    """On the event loop, at the provider's cadence."""
    _, worker, lock = await ports()
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    reads = len(lock.service_calls["get_usercodes"])

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    async with asyncio.timeout(2):
        while len(lock.service_calls["get_usercodes"]) == reads:
            await asyncio.sleep(0.01)


async def test_a_lock_that_turns_out_to_push_stops_polling(
    hass: HomeAssistant, ports
) -> None:
    """A bridge learns it can push once discovery lands; its poll goes."""
    port, worker, lock = await ports(MockLCMPushLock, setup=False)
    lock._supports_push = False
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await port.async_setup(entry)
    worker.want(1, "1234")
    worker.start()
    await worker.settled()

    lock._supports_push = True
    assert lock.coordinator is not None
    lock.coordinator.note_push_capability()
    reads = len(lock.service_calls["get_usercodes"])
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=2))
    await hass.async_block_till_done()
    await worker.settled()

    assert len(lock.service_calls["get_usercodes"]) == reads


# Setup and unload ----------------------------------------------------------------


async def _sync_until_quiet(worker, rounds: int = 10) -> None:
    for _ in range(rounds):
        worker.request_sync()
        await worker.settled()
        await asyncio.sleep(0)


# F1 -------------------------------------------------------------------------------


async def test_a_setup_that_failed_on_a_blip_is_retried_by_the_next_write(
    hass: HomeAssistant, ports
) -> None:
    """Z-Wave JS at boot with its node dead: the integration stays loaded."""
    with when_mock_lock_built(
        lambda lock: lock.fail("setup", error=LockDisconnected, times=1)
    ):
        port, _, lock = await ports(_NoConnectionCheck)
    assert not lock.provider_setup_succeeded

    with contextlib.suppress(LockError):
        await port.write(3, "3333")
    await hass.async_block_till_done()

    assert lock.provider_setup_succeeded


async def test_a_lock_whose_setup_failed_on_a_blip_gets_its_slots_written(
    hass: HomeAssistant, ports
) -> None:
    """Reads answer once the node is back: the slot must not be held forever."""
    with when_mock_lock_built(
        lambda lock: lock.fail("setup", error=LockDisconnected, times=1)
    ):
        _, worker, lock = await ports(_NoConnectionCheck)
    worker.want(3, "3333")
    worker.start()
    await _sync_until_quiet(worker)
    await hass.async_block_till_done()
    await _sync_until_quiet(worker)

    assert lock.codes.get(3) == "3333"
    assert worker.status(3) == (Status.IN_SYNC, None)


# F2 -------------------------------------------------------------------------------


async def test_a_lock_not_set_up_holds_no_slot(hass: HomeAssistant, ports) -> None:
    """Setup failed validation: the lock backs off; no slot is held for it."""

    def _no_pin(lock) -> None:
        lock.capabilities = NO_PIN_CAPABILITIES

    with when_mock_lock_built(_no_pin):
        _, worker, lock = await ports()
    assert not lock.provider_setup_succeeded
    worker.want(3, "3333")
    worker.start()
    await _sync_until_quiet(worker)

    assert worker.status(3)[1] is None
    assert lock.service_calls["set_usercode"] == []


# F3 -------------------------------------------------------------------------------


async def test_the_poll_is_not_rearmed_once_unloaded(ports) -> None:
    """A reconnect finishing while unload awaits the poll arms nothing."""
    port, worker, lock = await ports()
    assert port._poll_unsubs
    assert lock.coordinator is not None
    coordinator = lock.coordinator

    async def _poll_racing_a_reconnect() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            coordinator.note_push_capability()

    port._poll = asyncio.create_task(_poll_racing_a_reconnect())
    await asyncio.sleep(0)
    await worker.stop(grace=0.1)
    await port.async_unload(False)

    leaked = list(port._poll_unsubs)
    for unsub in leaked:
        unsub()
    assert not leaked


async def test_a_provider_the_port_never_set_up_is_read(ports) -> None:
    """A one-off read's provider is never set up: reading it needs nothing more."""
    port, _, lock = await ports(setup=False)

    assert await port.read([1]) == {1: Holds("1234")}


# Codes nobody here manages ------------------------------------------------------------


async def test_a_read_keeps_what_the_lock_said_of_slots_nobody_here_manages(
    ports,
) -> None:
    """A provider that reads the whole lock tells of every code on it."""
    port, _, lock = await ports()
    lock.codes = {1: "1234", 7: "7777"}

    assert await port.read([1]) == {1: Holds("1234")}
    assert port.others[7] == Holds("7777")


async def test_a_push_about_a_slot_nobody_here_manages_is_kept_aside(ports) -> None:
    """The worker tracks only wanted slots; the card still shows the rest."""
    port, worker, lock = await ports(MockLCMPushLock)
    lock._push_credential_update(9, SlotCredential.known("9999"))

    assert port.others == {9: Holds("9999")}
    assert 9 not in worker.slots


async def test_the_last_word_on_a_slot_stays_after_the_worker_lets_it_go(
    ports,
) -> None:
    """A one-off write's read-back is still on the card once the slot is dropped."""
    port, _, lock = await ports()
    lock.codes = {1: "1234", 7: "7777"}
    await port.read([7])
    lock.codes[7] = "7070"
    await port.read([7])

    assert port.others[7] == Holds("7070")


async def test_a_clear_that_found_nothing_says_so(ports) -> None:
    """Not proof of what the slot holds: the worker does not record it as cleared."""
    port, _, lock = await ports()

    assert await port.write(9, None) is False
    assert await port.write(1, None) is True


async def test_an_unknown_capacity_does_not_stop_a_read(ports) -> None:
    """A capability probe that breaks limits nothing: the slot is still read."""
    port, _, lock = await ports()
    with patch.object(
        type(lock), "_assert_slot_within_capacity", side_effect=RuntimeError("bug")
    ):
        assert await port.read([1]) == {1: Holds("1234")}


async def test_a_code_gone_from_a_lock_read_whole_leaves_the_side_table(ports) -> None:
    """A read of the whole lock tells of every code: one it omits is gone."""
    port, _, lock = await ports()
    lock.codes = {1: "1234", 7: "7777"}
    await port.read([1])
    del lock.codes[7]  # deleted at the keypad
    await port.read([1])

    assert 7 not in port.others


async def test_a_lock_walked_slot_by_slot_keeps_what_it_was_not_asked(ports) -> None:
    """A walk of the slots asked for says nothing about the others."""
    port, _, lock = await ports()
    lock.codes = {1: "1234", 7: "7777"}
    await port.read([1])
    with patch.object(type(lock), "per_exchange_budget", 10.0):
        del lock.codes[7]
        await port.read([1])

    assert port.others[7] == Holds("7777")


async def _reads_after(hass: HomeAssistant, lock, worker, later: timedelta) -> int:
    """Return how many reads reach the lock when the clock moves ``later`` on."""
    before = len(lock.service_calls["get_usercodes"])
    async_fire_time_changed(hass, dt_util.utcnow() + later)
    await hass.async_block_till_done()
    await worker.settled()
    return len(lock.service_calls["get_usercodes"]) - before


async def _push_lock(ports, drift: timedelta | None, *, push: bool = True):  # noqa: F811
    """Set up a push lock with a drift-check interval, its slot 1 in sync."""
    port, worker, lock = await ports(MockLCMPushLock, setup=False)
    lock._supports_push = push
    lock._hard_refresh_interval = drift
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(lock.hass)
    await port.async_setup(entry)
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    return port, worker, lock


async def test_a_lock_that_pushes_is_read_by_its_drift_check_alone(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """No scan for a lock that pushes; its drift check reads it on time."""
    _, worker, lock = await _push_lock(ports, timedelta(hours=1))

    assert await _reads_after(hass, lock, worker, timedelta(minutes=5)) == 0
    assert await _reads_after(hass, lock, worker, timedelta(minutes=61)) > 0


async def test_a_lock_that_pushes_with_no_drift_check_is_never_polled(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """Without a drift-check interval nothing reads a pushing lock on a timer."""
    _, worker, lock = await _push_lock(ports, None)

    assert await _reads_after(hass, lock, worker, timedelta(hours=3)) == 0


async def test_a_lock_that_does_not_push_is_never_subscribed(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """Set up and unloaded, a lock without push is neither subscribed nor unsubscribed."""
    port, worker, lock = await _push_lock(ports, None, push=False)
    assert lock.subscribe_calls == 0

    await worker.stop(grace=0.1)
    await port.async_unload(False)

    assert (lock.subscribe_calls, lock.unsubscribe_calls) == (0, 0)


async def test_a_lock_that_seems_to_stop_pushing_is_not_polled(
    hass: HomeAssistant,
    ports,  # noqa: F811
) -> None:
    """
    Push data going missing for a while is not a lock that stopped pushing.

    The provider says so after every setup that succeeds; chasing it would
    give the lock a poll that the next publication takes away again.
    """
    _, worker, lock = await _push_lock(ports, None)
    lock._supports_push = False
    assert lock.coordinator is not None
    lock.coordinator.note_push_capability()

    assert await _reads_after(hass, lock, worker, timedelta(minutes=5)) == 0


async def test_a_push_about_several_slots_reaches_each(
    ports,  # noqa: F811
) -> None:
    """Every slot in one push reaches the worker; one nobody manages is kept aside."""
    port, worker, lock = await ports(MockLCMPushLock)
    worker.want(1, "1234")
    worker.want(3, None)
    pushed: list[tuple[int, object]] = []
    worker.push = lambda slot, value: pushed.append((slot, value))  # type: ignore[method-assign]
    assert lock.coordinator is not None

    lock.coordinator.push_update(
        {
            1: SlotCredential.known("9999"),
            3: SlotCredential.empty(),
            7: SlotCredential.known("7777"),
        }
    )

    assert pushed == [(1, Holds("9999")), (3, Seen.EMPTY)]
    assert port.others == {7: Holds("7777")}


async def test_a_poll_the_lock_does_not_answer_raises_nothing_and_keeps_the_reading(
    ports,  # noqa: F811
) -> None:
    """A failed poll is the worker's to count: the slot keeps what was last read."""
    port, worker, lock = await ports()
    worker.want(1, "1234")
    worker.start()
    await worker.settled()
    seen = worker.slots[1].seen
    lock.fail("get", error=LockDisconnected)

    await port._read_tracked()

    assert worker.slots[1].seen == seen == Holds("1234")
    assert worker.reachable


async def _until(condition, rounds: int = 500) -> None:
    for _ in range(rounds):
        if condition():
            return
        await asyncio.sleep(0)
    pytest.fail("condition never came")


async def test_a_silent_lock_backs_off_doubling_to_the_cap_then_starts_over(
    rig_factory,  # noqa: F811
) -> None:
    """
    Unanswered reads back the lock off only from the threshold, doubling to a cap.

    It is offline only once that many have gone unanswered; answering ends it
    all, and the next outage backs off from the minimum again.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    paced = rig.clock.sleep
    backoffs: list[tuple[float, bool]] = []

    async def _recorded(seconds: float) -> None:
        if seconds >= BACKOFF_MIN:
            backoffs.append((seconds, rig.worker.offline))
        await paced(seconds)

    rig.worker.sleep = _recorded
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER - 1):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert rig.worker.reachable

    with pytest.raises(LockError):
        await rig.worker.read([1])
    assert not rig.worker.reachable
    probes = OFFLINE_AFTER - BACKOFF_AFTER + 1
    await _until(lambda: len(backoffs) >= probes)

    expected = [min(BACKOFF_MIN * 2**n, BACKOFF_MAX) for n in range(probes)]
    assert [seconds for seconds, _ in backoffs[:probes]] == expected
    assert expected[-1] == BACKOFF_MAX
    assert [offline for _, offline in backoffs[:probes]] == [False] * (probes - 1) + [
        True
    ]

    lock.reads_unanswered = False
    await _until(lambda: rig.worker.reachable)
    assert not rig.worker.offline
    backoffs.clear()
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    await _until(lambda: bool(backoffs))

    assert backoffs[0] == (BACKOFF_MIN, False)


async def _fast(_: float) -> None:
    await asyncio.sleep(0)


async def test_a_slot_past_capacity_is_no_answer_from_a_dead_lock(ports) -> None:  # noqa: F811
    """
    A read of only slots past the lock's capacity asks the lock nothing.

    The port filters them out and returns {} -- which the worker takes as
    the lock answering. During an outage the probe of the held past-capacity
    slot then "answers": the backoff ends, the owed try of the normal slot is
    paid, and the lock never goes offline. The outage holds a normal slot.
    """
    _, worker, lock = await ports()
    worker.sleep = _fast

    async def capacity(self, slot: int) -> None:
        if slot > 30:
            raise LockOperationUnsupported(f"slot {slot} is past 30")

    with patch.object(type(lock), "_assert_slot_within_capacity", capacity):
        worker.want(1, "1111")
        worker.want(40, "4040")
        worker.start()
        await worker.settled()
        assert worker.status(1) == (Status.IN_SYNC, None)
        assert worker.status(40) == (Status.SUSPENDED, Repair.CANT_SET)

        # The lock goes dead, and the user changes slot 1's PIN.
        lock.fail("get", error=LockDisconnected)
        lock.fail("set", error=LockDisconnected)
        worker.want(1, "2222")
        async with asyncio.timeout(5):
            while not worker.offline and worker.status(1)[1] is None:
                await asyncio.sleep(0)

        # An outage alone never holds a normal slot.
        assert worker.status(1)[1] is None
        assert worker.offline
        await worker.stop(grace=0.1)


async def test_a_lock_with_only_slots_past_capacity_holds_them_at_once(
    ports,  # noqa: F811
) -> None:
    """Its reads ask nothing: each slot is held, the lock is not taken for gone."""
    _, worker, lock = await ports()
    worker.sleep = _fast

    async def capacity(self, slot: int) -> None:
        if slot > 30:
            raise LockOperationUnsupported(f"slot {slot} is past 30")

    with patch.object(type(lock), "_assert_slot_within_capacity", capacity):
        worker.want(40, "4040")
        worker.start()
        await worker.settled()
        assert worker.status(40) == (Status.SUSPENDED, Repair.CANT_SET)
        assert worker.reachable
        assert 40 not in lock.codes
        await worker.stop(grace=0.1)
