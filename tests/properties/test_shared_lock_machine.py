"""
Stateful property test for locks that Lock Code Manager entries share.

Several entries can list one physical lock, and while loaded they drive it
through one provider instance. This machine sets up three entries over two
locks -- one polled, one that pushes -- and changes them only the ways a user
or Home Assistant does: setting entries up, unloading, reloading, disabling,
enabling and removing them, adding and dropping locks through the options
flow, booting with several at once, a lock whose setup fails, reloading the
locks' own integration, time passing, and the lock reporting a code use.

After every step it checks, through what Home Assistant and the mock lock
show:

1. Every loaded entry's slots are in sync on every lock it holds, with the
   lock's code on their code sensors, within a few sync ticks.
2. Each lock some loaded entry holds has exactly one live provider instance,
   and a lock no loaded entry holds has none: by the mock's ``setup`` and
   ``unload`` records, each instance unloaded at most once.
3. An unload is permanent only when the entry dropped the lock through its
   options and no other enabled entry lists it (PR #1482).
4. A push lock's live instance holds one subscription; every other instance
   holds none.
5. An instance that was unloaded, or dropped when its setup failed, never
   runs its provider setup again: no ``setup`` after its teardown in the
   mock's call order, even within the step that tore it down.
6. No repair is raised: nothing here should need the user.

Some rules also check what they did, as they do it. Time passing changes a
code on each held lock behind Lock Code Manager's back and lets a read
interval go by: exactly one instance of each held lock reads it, none of an
unheld one, and every holder's code sensor shows the change. The lock
reporting a code use reaches the entry owning the code exactly once, however
many instances listen. Dropping a lock through the options unloads it once
if that entry was its last holder and not at all otherwise, and takes the
entry's entities for it away. Reloading the locks' integration sets every
live instance up again.

Each example builds its own Home Assistant on its own loop and frozen clock:
Hypothesis reuses function-scoped fixtures across examples, so nothing here
may lean on them. Home Assistant starts in the first step, with any entries
the example boots with. Every step is bounded in its own loop, by steps of
the frozen clock: one waiting on work that never finishes fails as
``ExampleHung``, the same on every replay, so a hang shrinks like any other
failure. Teardown is bounded too: it drains the loop, and always takes the
example's Home Assistant off the harness's list of running instances, so one
hung example fails alone.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any
from unittest.mock import patch

import freezegun
from freezegun.api import FrozenDateTimeFactory
from hypothesis import Phase, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    INSTANCES,
    MockConfigEntry,
    async_test_home_assistant,
    mock_config_flow,
)

from homeassistant import loader, runner
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigFlow,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, STATE_ON
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    ATTR_IN_SYNC,
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    DOMAIN,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.exceptions import (
    ProviderNotImplementedError,
)

from ..common import (
    CLOCK_STEP,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    LOCK_DEVICE_DOMAIN,
    LockFault,
    MockLCMLock,
    MockLCMPushLock,
    async_advance_clock,
    async_configure_options,
    async_fire_mock_lock_use,
    async_run_until_quiet,
    async_settle_background,
    async_setup_mock_lock_entry,
    code_entity_id,
    current_entry,
    in_sync_entity_id,
    release_all_faults,
    when_mock_lock_built,
)

POLLED_LOCK = LOCK_1_ENTITY_ID
PUSH_LOCK = LOCK_2_ENTITY_ID
LOCKS = (POLLED_LOCK, PUSH_LOCK)
ENTRY_COUNT = 3
# Entry ``index`` holds the user in slot ``index + 1``, so entries sharing a
# lock never claim one slot twice. Every lock starts out holding every
# entry's code, so an entry that comes up in sync has written nothing.
PINS = {1: "1357", 2: "2468", 3: "3579"}
LOCK_LISTS = st.lists(st.sampled_from(LOCKS), min_size=1, max_size=2, unique=True)
# The polled lock reads on the coordinator's one-minute poll. The push lock
# polls on no timer, so its instances are given a drift check on the same
# cadence: a live instance reads, a torn-down one does not, on either lock.
READ_INTERVAL = timedelta(minutes=1)
# A slot comes up in sync within a tick or two; a second entry joining a lock
# waits out the coordinator's ten-second debounced read first.
MAX_TICKS_TO_SYNC = 8
# How long, in steps of the frozen clock, an operation may wait before it has
# hung. The loop's clock moves only when something steps it, so an asyncio
# timeout around a call that never returns would never fire: stepping the
# clock while the call waits is what bounds it, and does so the same way on
# every replay. A synchronous spin that never yields is bounded by nothing
# here; only the test's pytest timeout backstop catches it, unshrunk.
OPERATION_STEPS = 1000
# Stopping Home Assistant steps the clock a second at a time, past the
# timeouts of its own stop stages, which are minutes long in all.
STOP_STEP_SECONDS = 1.0
STOP_STEPS = 300
FROZEN_AT = "2026-01-01 12:00:00+00:00"
# Every lifecycle defect this machine has caught needed fewer than ten steps,
# and shorter examples are what keep a failure's shrink to seconds.
STEP_COUNT = 20
# The suite's whole-test timeout, raised for this test: a last-resort backstop
# only, past Hypothesis's own cutoff for a slow shrink.
BACKSTOP_SECONDS = 420


class _LockIntegrationFlow(ConfigFlow):
    """The mock lock integration's config flow, which nothing runs."""


class ExampleHung(Exception):
    """A step, or the teardown, waiting on work that never finishes."""


class FailedSetup(StrEnum):
    """How a lock's setup fails, in the rule that makes it fail."""

    # The provider's own setup raises something no handler expects: the lock
    # is dropped before anything of it is running.
    PROVIDER_BUG = "provider bug"
    # A push lock whose subscription is not implemented: the lock is dropped
    # after its coordinator and its state listener are running.
    NO_PUSH = "push not implemented"


def _build_provider(
    hass: HomeAssistant,
    dev_reg: Any,
    ent_reg: er.EntityRegistry,
    lock_config_entry: ConfigEntry | None,
    lock_entry: er.RegistryEntry,
) -> MockLCMLock:
    """Build the push mock for the push lock and the polled mock for the other."""
    cls = MockLCMPushLock if lock_entry.entity_id == PUSH_LOCK else MockLCMLock
    return cls(hass, dev_reg, ent_reg, lock_config_entry, lock_entry)


@dataclass
class _Position:
    """One of the machine's entries, as the machine expects it to be."""

    entry: MockConfigEntry | None = None
    locks: tuple[str, ...] = ()
    # The locks the entry holds while loaded: what it lists, less any whose
    # setup failed when it last came up.
    holds: set[str] = field(default_factory=set)
    loaded: bool = False
    disabled: bool = False


class SharedLockMachine(RuleBasedStateMachine):
    """Entries sharing locks, set up and torn down in random orders."""

    def __init__(self) -> None:
        super().__init__()
        self.positions = [_Position() for _ in range(ENTRY_COUNT)]
        self._entries_made = 0
        # Each lock's code table, which every instance built for it shares,
        # as every connection to one physical lock sees the same codes.
        self.physical = {lock: dict(PINS) for lock in LOCKS}
        # Every mock lock built, throwaways the flows read through included;
        # the entries' own are the ones that ran their provider setup.
        self.built: list[MockLCMLock] = []
        # What the next build of a lock should meet, set by the rule that
        # builds it: a failure for one lock, or a hang for every lock.
        self._fail_next: tuple[str, FailedSetup] | None = None
        self._hang_builds = False
        self._held_setups: list[LockFault] = []
        # Instances dropped by a failed setup, those dropped after their state
        # listener started among them. Keyed by identity: a provider instance
        # compares equal to any other for the same lock.
        self.dropped: set[int] = set()
        self.dropped_without_push: set[int] = set()
        # Unload records already checked, and the permanence each lock's
        # unload in the current step must have.
        self._unloads_seen: dict[int, int] = {}
        self._permanent: dict[str, bool] = {}
        self.credential_uses: Counter[str] = Counter()
        # Set once a step has hung: the loop may hold a task that will never
        # end, so teardown no longer drives Home Assistant through it.
        self.hung = False

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        runner.configure_event_loop(self.loop)
        self._stack = ExitStack()
        self._hass_cm = None
        self._entered = False
        # What starting a test Home Assistant changes, so teardown can put it
        # back however far starting got: it joins the harness's list of
        # running instances, and sets the default time zone, before anything
        # that can fail or hang.
        self._instances_before = list(INSTANCES)
        self._time_zone = dt_util.get_default_time_zone()
        # One fixed instant for every example, so a replay during shrinking
        # starts on the same clock readings as the run that failed. Hypothesis
        # keeps the real clock, for its own timing.
        self._frozen = freezegun.freeze_time(FROZEN_AT, ignore=["hypothesis"])
        self.freezer: FrozenDateTimeFactory = self._frozen.start()
        try:
            self._stack.enter_context(
                mock_config_flow(LOCK_DEVICE_DOMAIN, _LockIntegrationFlow)
            )
            self._stack.enter_context(
                patch.dict(
                    "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
                    {LOCK_DEVICE_DOMAIN: _build_provider},
                )
            )
            self._stack.enter_context(when_mock_lock_built(self._on_build))
            # Starting: the first step starts it, as Home Assistant boots.
            self._hass_cm = async_test_home_assistant(
                self.loop, initial_state=CoreState.starting
            )
            self.hass: HomeAssistant = self._run(
                self._async_bounded(self._hass_cm.__aenter__(), "build"), "build"
            )
            self._entered = True
            # What the autouse custom-integration fixture does for the
            # fixture's Home Assistant, which this one is not.
            self.hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
            self.lock_entry = self._run(
                self._async_bounded(async_setup_mock_lock_entry(self.hass), "build"),
                "build",
            )
            # Set up before any entry exists, so each setup below sets up its
            # own entry rather than every entry of the domain.
            assert self._run(
                self._async_bounded(
                    async_setup_component(self.hass, DOMAIN, {}), "build"
                ),
                "build",
            )
            self.hass.bus.async_listen(BUS_EVENT_CREDENTIAL_USED, self._on_use)
        except BaseException:
            self._close()
            raise

    # Plumbing.

    def _run[T](self, coro: Coroutine[Any, Any, T], what: str) -> T:
        """
        Run one step's coroutine, failing as ``ExampleHung`` on work that never ends.

        The harness's own bounds -- stepping the clock until background work
        finishes, running the loop until it is quiet -- fail the step when
        they run out. Whichever of them it was, the step is waiting on work
        that never finishes, and fails as that, with a message naming only
        the step, so every replay fails the same way.
        """
        try:
            return self.loop.run_until_complete(coro)
        except pytest.fail.Exception as err:
            self.hung = True
            raise ExampleHung(f"{what}: {err}") from None

    async def _async_bounded[T](
        self,
        operation: Awaitable[T],
        what: str,
        *,
        step: float = CLOCK_STEP,
        steps: int = OPERATION_STEPS,
    ) -> T:
        """
        Await ``operation``, stepping the frozen clock while it waits.

        Raises ``ExampleHung`` once it has waited ``steps`` steps, having
        cancelled it.
        """
        task = asyncio.ensure_future(operation)
        for _ in range(steps):
            await async_run_until_quiet()
            if task.done():
                return task.result()
            self.freezer.tick(step)
        task.cancel()
        self.hung = True
        raise ExampleHung(f"{what} never finished")

    def _on_build(self, lock: MockLCMLock) -> None:
        """Give each mock lock its physical code table, and what its rule asks."""
        lock.codes = self.physical[lock.lock.entity_id]
        if isinstance(lock, MockLCMPushLock):
            lock._hard_refresh_interval = READ_INTERVAL
        if self._fail_next is not None and self._fail_next[0] == lock.lock.entity_id:
            if self._fail_next[1] == FailedSetup.NO_PUSH:
                assert isinstance(lock, MockLCMPushLock)
                lock.subscribe_error = ProviderNotImplementedError(
                    lock, "setup_push_subscription"
                )
            else:
                lock.fail("setup", error=RuntimeError("provider setup bug"))
        if self._hang_builds:
            self._held_setups.append(lock.hang("setup", times=1))
        self.built.append(lock)

    def _on_use(self, event: Event) -> None:
        """Count each credential-used event by the entry it is attributed to."""
        self.credential_uses[event.data["config_entry_id"]] += 1

    async def _async_settle(self) -> None:
        """
        Let everything a step set off finish, on the frozen clock.

        The one way anything here settles, so work that never finishes is
        found by the same bound, and fails the same way, wherever it was set
        off.
        """
        await async_run_until_quiet()
        await async_settle_background(self.hass, self.freezer)

    def _act(self, operation: Callable[[], Coroutine[Any, Any, Any]], what: str) -> Any:
        """Run one operation through Home Assistant, then settle what it set off."""

        async def _act_and_settle() -> Any:
            result = await self._async_bounded(operation(), what)
            await self._async_settle()
            return result

        return self._run(_act_and_settle(), what)

    def _instances(self, lock_entity_id: str | None = None) -> list[MockLCMLock]:
        """Return the instances entries built, for one lock or for all."""
        return [
            lock
            for lock in self.built
            if lock.service_calls["setup"]
            and lock_entity_id in (None, lock.lock.entity_id)
        ]

    def _live(self, lock_entity_id: str) -> list[MockLCMLock]:
        """Return the instances of a lock neither unloaded nor dropped."""
        return [
            lock
            for lock in self._instances(lock_entity_id)
            if not lock.service_calls["unload"] and id(lock) not in self.dropped
        ]

    # The model.

    def _indices(self, wanted: Callable[[_Position], bool]) -> list[int]:
        return [index for index in range(ENTRY_COUNT) if wanted(self.positions[index])]

    @staticmethod
    def _settable(position: _Position) -> bool:
        return not position.loaded and not position.disabled

    @staticmethod
    def _loaded(position: _Position) -> bool:
        return position.loaded

    @staticmethod
    def _present(position: _Position) -> bool:
        return position.entry is not None

    @staticmethod
    def _enabled(position: _Position) -> bool:
        return position.entry is not None and not position.disabled

    @staticmethod
    def _disabled(position: _Position) -> bool:
        return position.disabled

    def _held(self, lock_entity_id: str) -> bool:
        """Return whether a loaded entry holds the lock."""
        return any(
            position.loaded and lock_entity_id in position.holds
            for position in self.positions
        )

    def _holders(self, lock_entity_id: str) -> list[int]:
        """Return the loaded entries holding the lock."""
        return self._indices(
            lambda position: position.loaded and lock_entity_id in position.holds
        )

    def _entry(self, index: int, locks: list[str] | tuple[str, ...]) -> ConfigEntry:
        """Return the entry at ``index``, adding one listing ``locks`` if none."""
        position = self.positions[index]
        if position.entry is None:
            self._entries_made += 1
            name = f"Shared {self._entries_made}"
            position.entry = current_entry(
                name, {index + 1: PINS[index + 1]}, tuple(locks)
            )
            position.entry.add_to_hass(self.hass)
            position.locks = tuple(locks)
        return position.entry

    def _came_up(self, index: int, failed: str | None = None) -> None:
        """Record that the entry at ``index`` loaded, holding all but ``failed``."""
        position = self.positions[index]
        position.loaded = True
        position.holds = set(position.locks) - {failed}

    def _pick(self, data: st.DataObject, wanted, label: str) -> int:
        return data.draw(st.sampled_from(self._indices(wanted)), label=label)

    def _set_up(self, index: int) -> None:
        """Set up the entry at ``index`` and check it loaded."""
        entry = self.positions[index].entry
        assert entry is not None
        assert self._act(
            lambda: self.hass.config_entries.async_setup(entry.entry_id), "set_up"
        )

    # Rules.

    @precondition(lambda self: self._indices(self._settable))
    @rule(data=st.data(), locks=LOCK_LISTS)
    def set_up(self, data: st.DataObject, locks: list[str]) -> None:
        """Set up an entry, adding it first if it does not exist yet."""
        index = self._pick(data, self._settable, "entry")
        self._entry(index, locks)
        self._set_up(index)
        self._came_up(index)

    @precondition(lambda self: self._indices(self._loaded))
    @rule(data=st.data())
    def unload(self, data: st.DataObject) -> None:
        index = self._pick(data, self._loaded, "entry")
        entry = self.positions[index].entry
        assert self._act(
            lambda: self.hass.config_entries.async_unload(entry.entry_id), "unload"
        )
        self.positions[index].loaded = False

    @precondition(lambda self: self._indices(self._enabled))
    @rule(data=st.data())
    def reload(self, data: st.DataObject) -> None:
        index = self._pick(data, self._enabled, "entry")
        entry = self.positions[index].entry
        assert self._act(
            lambda: self.hass.config_entries.async_reload(entry.entry_id), "reload"
        )
        self._came_up(index)

    @precondition(lambda self: self._indices(self._enabled))
    @rule(data=st.data())
    def disable(self, data: st.DataObject) -> None:
        index = self._pick(data, self._enabled, "entry")
        entry = self.positions[index].entry
        assert self._act(
            lambda: self.hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            ),
            "disable",
        )
        self.positions[index].loaded = False
        self.positions[index].disabled = True

    @precondition(lambda self: self._indices(self._disabled))
    @rule(data=st.data())
    def enable(self, data: st.DataObject) -> None:
        index = self._pick(data, self._disabled, "entry")
        entry = self.positions[index].entry
        assert self._act(
            lambda: self.hass.config_entries.async_set_disabled_by(
                entry.entry_id, None
            ),
            "enable",
        )
        self.positions[index].disabled = False
        self._came_up(index)

    @precondition(lambda self: self._indices(self._present))
    @rule(data=st.data())
    def remove(self, data: st.DataObject) -> None:
        index = self._pick(data, self._present, "entry")
        entry = self.positions[index].entry
        self._act(
            lambda: self.hass.config_entries.async_remove(entry.entry_id), "remove"
        )
        self.positions[index] = _Position()

    @precondition(lambda self: len(self._indices(self._loaded)) >= 2)
    @rule(data=st.data())
    def unload_two_at_once(self, data: st.DataObject) -> None:
        indices = data.draw(
            st.lists(
                st.sampled_from(self._indices(self._loaded)),
                min_size=2,
                max_size=2,
                unique=True,
            ),
            label="entries",
        )
        entries = [self.positions[index].entry for index in indices]

        async def _unload_both() -> list[bool]:
            return await asyncio.gather(
                *(
                    self.hass.config_entries.async_unload(entry.entry_id)
                    for entry in entries
                )
            )

        assert all(self._act(_unload_both, "unload_two_at_once"))
        for index in indices:
            self.positions[index].loaded = False

    @staticmethod
    def _droppable(position: _Position) -> bool:
        return position.loaded and len(position.locks) == 2

    @precondition(lambda self: self._indices(self._droppable))
    @rule(data=st.data(), lock=st.sampled_from(LOCKS))
    def drop_lock_through_options(self, data: st.DataObject, lock: str) -> None:
        """Take a lock out of an entry through its options, as the user does."""
        index = self._pick(data, self._droppable, "entry")
        position = self.positions[index]
        entry = position.entry
        assert entry is not None
        # The lock leaves Lock Code Manager only if no other enabled entry
        # lists it, loaded or not.
        self._permanent[lock] = not any(
            lock in other.locks
            for other_index, other in enumerate(self.positions)
            if other_index != index and self._enabled(other)
        )
        held_elsewhere = any(
            other_index != index and other.loaded and lock in other.holds
            for other_index, other in enumerate(self.positions)
        )
        unloads_before = sum(
            len(instance.service_calls["unload"]) for instance in self._instances(lock)
        )
        kept = tuple(other for other in position.locks if other != lock)
        self._configure_locks(entry, kept)
        unloads = (
            sum(
                len(instance.service_calls["unload"])
                for instance in self._instances(lock)
            )
            - unloads_before
        )
        # Let go of exactly once when this was its last holder, else not at all.
        expected = 0 if held_elsewhere or lock not in position.holds else 1
        assert unloads == expected, (
            f"dropping {lock} from {entry.title} unloaded it {unloads} times, "
            f"expected {expected}"
        )
        assert self._per_lock_entity(entry, index + 1, lock) is None, (
            f"{entry.title} kept its in-sync entity for {lock}"
        )
        position.locks = kept
        position.holds.discard(lock)

    @staticmethod
    def _addable(position: _Position) -> bool:
        return position.loaded and len(position.locks) == 1

    @precondition(lambda self: self._indices(self._addable))
    @rule(data=st.data())
    def add_lock_through_options(self, data: st.DataObject) -> None:
        """Add the other lock to an entry through its options, as the user does."""
        index = self._pick(data, self._addable, "entry")
        position = self.positions[index]
        assert position.entry is not None
        added = next(lock for lock in LOCKS if lock not in position.locks)
        self._configure_locks(position.entry, (*position.locks, added))
        position.locks = (*position.locks, added)
        position.holds.add(added)

    def _configure_locks(self, entry: ConfigEntry, locks: tuple[str, ...]) -> None:
        """Submit the options flow with ``locks`` and check the entry took it."""

        async def _submit() -> Any:
            started = await self.hass.config_entries.options.async_init(entry.entry_id)
            return await async_configure_options(
                self.hass, started["flow_id"], {CONF_LOCKS: list(locks)}
            )

        result = self._act(_submit, "options flow")
        assert result["type"] is FlowResultType.CREATE_ENTRY, result
        assert entry.data[CONF_LOCKS] == list(locks)

    def _failing_setups(self) -> list[tuple[int, str, FailedSetup]]:
        """Return each entry, lock and failure a failing setup can be drawn from."""
        return [
            (index, lock, how)
            for index in self._indices(self._settable)
            if self.positions[index].entry is not None
            for lock in self.positions[index].locks
            if not self._held(lock)
            for how in (
                (FailedSetup.PROVIDER_BUG, FailedSetup.NO_PUSH)
                if lock == PUSH_LOCK
                else (FailedSetup.PROVIDER_BUG,)
            )
        ]

    @precondition(lambda self: self._failing_setups())
    @rule(data=st.data())
    def set_up_with_a_failing_lock(self, data: st.DataObject) -> None:
        """Set up an entry whose new instance of one lock fails its setup."""
        index, lock, how = data.draw(
            st.sampled_from(self._failing_setups()), label="failure"
        )
        assert self.positions[index].entry is not None
        built_before = len(self.built)
        self._fail_next = (lock, how)
        try:
            self._set_up(index)
        finally:
            self._fail_next = None
        [failed] = [
            instance
            for instance in self.built[built_before:]
            if instance.lock.entity_id == lock and instance.service_calls["setup"]
        ]
        self.dropped.add(id(failed))
        if how == FailedSetup.NO_PUSH:
            self.dropped_without_push.add(id(failed))
        self._came_up(index, failed=lock)

    @initialize(
        booting=st.lists(
            st.tuples(st.integers(min_value=0, max_value=ENTRY_COUNT - 1), LOCK_LISTS),
            unique_by=lambda booted: booted[0],
            # At least one: set_up covers an entry set up after the start.
            min_size=1,
            max_size=ENTRY_COUNT,
        )
    )
    def start(self, booting: list[tuple[int, list[str]]]) -> None:
        """
        Start Home Assistant, with the entries it boots with, each on its own locks.

        Those entries set up together, as Home Assistant sets up every entry
        at boot, and finish setting up once it has started. Every lock built
        meanwhile holds its provider setup until all of them are waiting, so
        an entry joining a lock another entry is building reaches it before
        it is ready (#865).
        """
        entries = [self._entry(index, locks) for index, locks in booting]
        hass = self.hass

        async def _set_up_all() -> list[bool]:
            return await asyncio.gather(
                *(hass.config_entries.async_setup(entry.entry_id) for entry in entries)
            )

        async def _boot() -> None:
            assert all(await self._async_bounded(_set_up_all(), "start"))
            hass.set_state(CoreState.running)
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
            await async_run_until_quiet()
            for fault in self._held_setups:
                await fault.async_wait_held()
            for fault in self._held_setups:
                fault.clear()
            await self._async_settle()

        self._hang_builds = True
        try:
            self._run(_boot(), "start")
        finally:
            self._hang_builds = False
            for fault in self._held_setups:
                fault.clear()
            self._held_setups.clear()
        for index, _locks in booting:
            self._came_up(index)

    @precondition(lambda self: any(self._held(lock) for lock in LOCKS))
    @rule()
    def a_read_interval_passes(self) -> None:
        """
        Change each held lock's codes behind Lock Code Manager, then let time pass.

        Exactly one instance of each held lock reads it, none of a lock no
        loaded entry holds, and the change reaches every holder.
        """
        hass = self.hass
        reads_before = {
            id(instance): len(instance.service_calls["get_usercodes"])
            for instance in self._instances()
        }
        changed: dict[str, tuple[str, str]] = {}
        for lock in LOCKS:
            for index in self._holders(lock):
                slot = index + 1
                code = f"{slot}0{slot}0"
                self.physical[lock][slot] = code
                entry = self.positions[index].entry
                assert entry is not None
                changed[code_entity_id(hass, entry, slot, lock)] = (lock, code)
        seen: dict[str, set[str]] = {entity_id: set() for entity_id in changed}

        def _record(event: Event) -> None:
            if (entity_id := event.data["entity_id"]) in seen and (
                new_state := event.data["new_state"]
            ) is not None:
                seen[entity_id].add(new_state.state)

        unsubscribe = hass.bus.async_listen("state_changed", _record)
        try:

            async def _async_read_interval() -> None:
                await async_advance_clock(
                    hass, self.freezer, READ_INTERVAL.total_seconds() + 1, settle=False
                )
                await self._async_settle()

            self._run(_async_read_interval(), "a_read_interval_passes")
        finally:
            unsubscribe()

        for lock in LOCKS:
            readers = [
                instance
                for instance in self._instances(lock)
                if len(instance.service_calls["get_usercodes"])
                > reads_before.get(id(instance), 0)
            ]
            assert len(readers) == (1 if self._held(lock) else 0), (
                f"{len(readers)} instances of {lock} read it in a read interval; "
                f"held: {self._held(lock)}"
            )
        missed = {
            entity_id: changed[entity_id]
            for entity_id, states in seen.items()
            if changed[entity_id][1] not in states
        }
        assert not missed, f"code sensors that never showed the change: {missed}"

    @precondition(lambda self: self._held(PUSH_LOCK))
    @rule(data=st.data())
    def push_lock_reports_a_use(self, data: st.DataObject) -> None:
        """
        The push lock's integration reports one use of a code.

        It reaches every subscription still registered, a torn-down instance
        that never removed its own included, as a real lock's would; the use
        reaches the entry owning the code exactly once.
        """
        index = data.draw(st.sampled_from(self._holders(PUSH_LOCK)), label="entry")
        entry = self.positions[index].entry
        assert entry is not None
        before = self.credential_uses[entry.entry_id]

        async def _report() -> None:
            async_fire_mock_lock_use(self.hass, PUSH_LOCK, index + 1)

        self._act(_report, "push_lock_reports_a_use")
        uses = self.credential_uses[entry.entry_id] - before
        assert uses == 1, f"one use of slot {index + 1} reached {entry.title} {uses}x"

    @precondition(lambda self: any(self._held(lock) for lock in LOCKS))
    @rule()
    def reload_the_locks_integration(self) -> None:
        """
        Reload the integration the locks belong to.

        Every live instance runs its provider setup again; the invariants
        check that no torn-down one does.
        """
        live = [
            (instance, len(instance.service_calls["setup"]))
            for instance in self._live_all()
        ]
        assert self._act(
            lambda: self.hass.config_entries.async_reload(self.lock_entry.entry_id),
            "reload_the_locks_integration",
        )
        for instance, setups in live:
            assert len(instance.service_calls["setup"]) == setups + 1, (
                f"live {instance.lock.entity_id} instance did not set up again"
            )

    def _live_all(self) -> list[MockLCMLock]:
        return [instance for lock in LOCKS for instance in self._live(lock)]

    # Invariants.

    @invariant()
    def entries_are_where_the_model_put_them(self) -> None:
        for position in self.positions:
            if position.entry is None:
                continue
            assert (position.entry.state is ConfigEntryState.LOADED) is (
                position.loaded
            ), f"{position.entry.title} is {position.entry.state}"
            assert (position.entry.disabled_by is not None) is position.disabled

    @invariant()
    def loaded_slots_are_in_sync(self) -> None:
        hass = self.hass
        expected = {
            (
                in_sync_entity_id(hass, position.entry, index + 1, lock),
                code_entity_id(hass, position.entry, index + 1, lock),
            ): PINS[index + 1]
            for index, position in enumerate(self.positions)
            if position.loaded and position.entry is not None
            for lock in position.holds
        }

        def _in_sync() -> bool:
            return all(
                (in_sync := hass.states.get(in_sync_id)) is not None
                and in_sync.state == STATE_ON
                and (code := hass.states.get(code_id)) is not None
                and code.state == pin
                for (in_sync_id, code_id), pin in expected.items()
            )

        async def _async_ticks_until_in_sync() -> bool:
            for _ in range(MAX_TICKS_TO_SYNC):
                if _in_sync():
                    return True
                await async_advance_clock(
                    hass, self.freezer, TICK_INTERVAL.total_seconds(), settle=False
                )
                await self._async_settle()
            return _in_sync()

        assert self._run(_async_ticks_until_in_sync(), "loaded_slots_are_in_sync"), (
            f"slots not in sync with the lock's codes after {MAX_TICKS_TO_SYNC} "
            f"sync ticks: {expected}"
        )

    @invariant()
    def one_live_instance_per_held_lock(self) -> None:
        for lock in LOCKS:
            live = self._live(lock)
            assert len(live) == (1 if self._held(lock) else 0), (
                f"{len(live)} live instances of {lock}; held by "
                f"{[self.positions[index].entry.title for index in self._holders(lock)]}"
            )
        for instance in self._instances():
            assert len(instance.service_calls["unload"]) <= 1, (
                f"an instance of {instance.lock.entity_id} unloaded "
                f"{instance.service_calls['unload']}"
            )

    @invariant()
    def unloads_are_permanent_only_when_the_lock_leaves(self) -> None:
        for instance in self._instances():
            unloads = instance.service_calls["unload"]
            checked = self._unloads_seen.get(id(instance), 0)
            new = unloads[checked:]
            expected = (self._permanent.get(instance.lock.entity_id, False),)
            assert all(unload == expected for unload in new), (
                f"{instance.lock.entity_id} unloaded with {new}, expected {expected}"
            )
            self._unloads_seen[id(instance)] = len(unloads)
        self._permanent.clear()

    @invariant()
    def push_subscriptions_balance(self) -> None:
        live = {id(instance) for instance in self._live(PUSH_LOCK)}
        for instance in self._instances(PUSH_LOCK):
            assert isinstance(instance, MockLCMPushLock)
            if self._known_leak(instance):
                continue
            held = instance.subscribe_calls - instance.unsubscribe_calls
            is_live = id(instance) in live
            assert held == (1 if is_live else 0), (
                f"{'live' if is_live else 'torn-down'} push instance "
                f"holds {held} subscriptions"
            )

    @invariant()
    def torn_down_instances_never_set_up_again(self) -> None:
        for instance in self._instances():
            order = instance.call_order
            # Where its life ended: the call after its unload, or after its
            # one provider setup, the one that failed.
            if instance.service_calls["unload"]:
                after = order.index("unload") + 1
            elif id(instance) in self.dropped and not self._known_leak(instance):
                after = order.index("setup") + 1
            else:
                continue
            assert "setup" not in order[after:], (
                f"a torn-down {instance.lock.entity_id} instance set up again: {order}"
            )

    def _known_leak(self, instance: MockLCMLock) -> bool:
        """
        Return whether an instance was dropped after its state listener started.

        Nothing removes that listener, so the instance unsubscribes and sets
        up again whenever the locks' integration reloads: a known defect,
        pinned by ``tests/test_init.py::
        test_a_lock_dropped_after_its_listener_started_never_sets_up_again``
        and exempt here, alone, until it is fixed.
        """
        return id(instance) in self.dropped_without_push

    @invariant()
    def no_repairs(self) -> None:
        issues = [
            issue_id
            for (domain, issue_id) in ir.async_get(self.hass).issues
            if domain == DOMAIN
        ]
        assert not issues, f"repairs raised: {issues}"

    def _per_lock_entity(self, entry: ConfigEntry, slot: int, lock: str) -> str | None:
        """Return an entry's in-sync entity for a slot on a lock, if registered."""
        return er.async_get(self.hass).async_get_entity_id(
            BINARY_SENSOR_DOMAIN,
            DOMAIN,
            build_slot_unique_id(entry.entry_id, slot, ATTR_IN_SYNC, lock),
        )

    # Teardown.

    def teardown(self) -> None:
        self._close()

    def _close(self) -> None:
        """
        Unload every entry and stop this example's Home Assistant, bounded.

        Held faults are let go first, or the unloads wait on them forever.
        After a hang nothing is driven through Home Assistant but its stop.
        Whatever happened, every task left on the loop is cancelled and
        drained, so none is collected pending in a later test; every
        instance this example started comes off the harness's list of
        running ones, and the time zone is put back, however far starting it
        got -- an instance left on that list aborts the whole session; and
        the executors are shut down, so no thread outlives the test. A
        teardown that hangs itself fails the example as hung, once
        everything is closed.
        """
        built = hasattr(self, "hass")
        teardown_hang: ExampleHung | None = None
        try:
            release_all_faults()
            if built and not self.hung:
                try:
                    self._run(self._async_unload_all(), "teardown")
                except ExampleHung as err:
                    teardown_hang = err
            if built:
                with suppress(ExampleHung):
                    self._run(
                        self._async_bounded(
                            self.hass.async_stop(force=True),
                            "stop",
                            step=STOP_STEP_SECONDS,
                            steps=STOP_STEPS,
                        ),
                        "teardown",
                    )
            self._run(self._async_drain(), "teardown")
        finally:
            # Stopped before the executors are: their shutdown times itself
            # on the monotonic clock.
            self._frozen.stop()
            try:
                if self._entered:
                    assert self._hass_cm is not None
                    # Undoes what entering it did; awaits nothing.
                    self.loop.run_until_complete(
                        self._hass_cm.__aexit__(None, None, None)
                    )
            finally:
                dt_util.set_default_time_zone(self._time_zone)
                self._stack.close()
                try:
                    # One still on the list never finished stopping, which is
                    # what would have shut its import executor down. Home
                    # Assistant's executors bound their own shutdown,
                    # interrupting a thread that will not finish.
                    for instance in [
                        instance
                        for instance in INSTANCES
                        if not any(instance is old for old in self._instances_before)
                    ]:
                        INSTANCES.remove(instance)
                        instance.import_executor.shutdown()
                    # Awaited, so the thread it shuts down in is gone too.
                    self.loop.run_until_complete(self.loop.shutdown_default_executor())
                finally:
                    self.loop.close()
                    asyncio.set_event_loop(None)
        if teardown_hang is not None:
            raise teardown_hang

    async def _async_unload_all(self) -> None:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await self._async_bounded(
                    self.hass.config_entries.async_unload(entry.entry_id), "teardown"
                )
        await self._async_settle()

    async def _async_drain(self) -> None:
        """Cancel every task left on the loop and let them finish, bounded."""
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        for task in tasks:
            task.cancel()
        for _ in range(OPERATION_STEPS):
            await async_run_until_quiet()
            if all(task.done() for task in tasks):
                return
            self.freezer.tick(CLOCK_STEP)
        for task in tasks:
            if not task.done():
                # It swallows cancellation and will never finish. The step
                # that started it has failed as hung already; collected pending
                # in some later test, it would only log that again there.
                task._log_destroy_pending = False  # type: ignore[attr-defined]


# As many examples as the profile asks for. The explain phase reruns a shrunk
# failure once per step to annotate it, which costs more than the shrink here
# and adds nothing a reader of the steps lacks.
SharedLockMachine.TestCase.settings = settings(
    max_examples=settings.default.max_examples,
    stateful_step_count=STEP_COUNT,
    deadline=None,
    phases=tuple(phase for phase in Phase if phase is not Phase.explain),
    # Shrinks the one failure it reports, not every distinct one it meets on
    # the way: a broken lifecycle rule fails several invariants at once.
    report_multiple_bugs=False,
)
TestSharedLockMachine = SharedLockMachine.TestCase
# A last-resort backstop. A hang that awaits is each step's own bound to
# report, as ExampleHung, the same on every replay; this catches only what
# that bound cannot -- a synchronous spin that never yields -- and then
# unshrunk. The suite's whole-test timeout raises inside whatever example is
# running, which Hypothesis would take for that example's own failure, replay,
# and report as flaky; so it is raised past Hypothesis's own cutoff for a slow
# shrink, which stops shrinking and reports what it has before this fires.
TestSharedLockMachine.pytestmark = [pytest.mark.timeout(BACKSTOP_SECONDS)]
