"""
Stateful property test for locks that Lock Code Manager entries share.

Several entries can list one physical lock, and while loaded they drive it
through one provider instance. This machine sets up three entries over two
locks -- one polled, one that pushes -- and changes them only the ways a user
or Home Assistant does: setting entries up, unloading, reloading, disabling,
enabling and removing them, adding and dropping locks through the options
flow, booting several at once, a lock whose setup fails, reloading the
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
   runs its provider setup again.
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
may lean on them. Every step is bounded in real time and fails rather than
hangs, so a failure shrinks instead of timing out.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Coroutine
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
import threading
from typing import Any
from unittest.mock import patch

import freezegun
from freezegun.api import FrozenDateTimeFactory
from hypothesis import Phase, settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)
import pytest
from pytest_homeassistant_custom_component.common import (
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

from custom_components.lock_code_manager.const import (
    ATTR_IN_SYNC,
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.config import build_slot_unique_id
from custom_components.lock_code_manager.domain.exceptions import (
    ProviderNotImplementedError,
)

from ..common import (
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    LOCK_DEVICE_DOMAIN,
    LockFault,
    MockLCMLock,
    MockLCMPushLock,
    async_advance_clock,
    async_configure_options,
    async_run_until_quiet,
    async_settle_background,
    async_setup_mock_lock_entry,
    async_tick_until,
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
# Steps take milliseconds of real time. One still running after this is
# waiting on something that will never come, and is reported as that.
STEP_SECONDS = 10.0
FROZEN_AT = "2026-01-01 12:00:00+00:00"
# Every lifecycle defect this machine has caught needed fewer than ten steps,
# and shorter examples are what keep a failure's shrink to seconds.
STEP_COUNT = 20
SHRINK_SECONDS = 420


class _LockIntegrationFlow(ConfigFlow):
    """The mock lock integration's config flow, which nothing runs."""


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
        self._hung: list[LockFault] = []
        # Instances dropped by a failed setup, and the provider setups each
        # dropped or unloaded instance had run when it went. Keyed by identity:
        # a provider instance compares equal to any other for the same lock.
        self.dropped: set[int] = set()
        self.dropped_without_push: set[int] = set()
        self._setups_when_gone: dict[int, int] = {}
        # Unload records already checked, and the permanence each lock's
        # unload in the current step must have.
        self._unloads_seen: dict[int, int] = {}
        self._permanent: dict[str, bool] = {}
        self.credential_uses: Counter[str] = Counter()

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        runner.configure_event_loop(self.loop)
        self._stack = ExitStack()
        self._hass_cm = None
        # One fixed instant for every example, so a replay during shrinking
        # starts on the same clock readings as the run that failed.
        self._frozen = freezegun.freeze_time(FROZEN_AT)
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
            self._hass_cm = async_test_home_assistant(self.loop)
            self.hass: HomeAssistant = self._run(self._hass_cm.__aenter__())
            # What the autouse custom-integration fixture does for the
            # fixture's Home Assistant, which this one is not.
            self.hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
            self.lock_entry = self._run(async_setup_mock_lock_entry(self.hass))
            # Set up before any entry exists, so each setup below sets up its
            # own entry rather than every entry of the domain.
            assert self._run(async_setup_component(self.hass, DOMAIN, {}))
            self.hass.bus.async_listen(BUS_EVENT_CREDENTIAL_USED, self._on_use)
        except BaseException:
            self._close()
            raise

    # Plumbing.

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        """Run one step's coroutine, failing it if it runs too long in real time."""
        task = self.loop.create_task(coro)
        watchdog = threading.Timer(
            STEP_SECONDS, self.loop.call_soon_threadsafe, (task.cancel,)
        )
        watchdog.start()
        try:
            return self.loop.run_until_complete(task)
        except asyncio.CancelledError:
            raise AssertionError(
                f"step still running after {STEP_SECONDS:.0f}s of real time"
            ) from None
        finally:
            watchdog.cancel()

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
            self._hung.append(lock.hang("setup", times=1))
        self.built.append(lock)

    def _on_use(self, event: Event) -> None:
        """Count each credential-used event by the entry it is attributed to."""
        self.credential_uses[event.data["config_entry_id"]] += 1

    async def _async_settle(self) -> None:
        """Let everything the step set off finish, on the frozen clock."""
        await async_run_until_quiet()
        await async_settle_background(self.hass, self.freezer)

    def _act(self, operation: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
        """Run one operation through Home Assistant, then settle what it set off."""

        async def _act_and_settle() -> Any:
            result = await operation()
            await self._async_settle()
            return result

        return self._run(_act_and_settle())

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
        assert self._act(lambda: self.hass.config_entries.async_setup(entry.entry_id))

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
        assert self._act(lambda: self.hass.config_entries.async_unload(entry.entry_id))
        self.positions[index].loaded = False

    @precondition(lambda self: self._indices(self._enabled))
    @rule(data=st.data())
    def reload(self, data: st.DataObject) -> None:
        index = self._pick(data, self._enabled, "entry")
        entry = self.positions[index].entry
        assert self._act(lambda: self.hass.config_entries.async_reload(entry.entry_id))
        self._came_up(index)

    @precondition(lambda self: self._indices(self._enabled))
    @rule(data=st.data())
    def disable(self, data: st.DataObject) -> None:
        index = self._pick(data, self._enabled, "entry")
        entry = self.positions[index].entry
        assert self._act(
            lambda: self.hass.config_entries.async_set_disabled_by(
                entry.entry_id, ConfigEntryDisabler.USER
            )
        )
        self.positions[index].loaded = False
        self.positions[index].disabled = True

    @precondition(lambda self: self._indices(self._disabled))
    @rule(data=st.data())
    def enable(self, data: st.DataObject) -> None:
        index = self._pick(data, self._disabled, "entry")
        entry = self.positions[index].entry
        assert self._act(
            lambda: self.hass.config_entries.async_set_disabled_by(entry.entry_id, None)
        )
        self.positions[index].disabled = False
        self._came_up(index)

    @precondition(lambda self: self._indices(self._present))
    @rule(data=st.data())
    def remove(self, data: st.DataObject) -> None:
        index = self._pick(data, self._present, "entry")
        entry = self.positions[index].entry
        self._act(lambda: self.hass.config_entries.async_remove(entry.entry_id))
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

        assert all(self._act(_unload_both))
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

        result = self._act(_submit)
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

    @precondition(lambda self: len(self._indices(self._settable)) >= 2)
    @rule(data=st.data(), locks=LOCK_LISTS)
    def boot_together(self, data: st.DataObject, locks: list[str]) -> None:
        """
        Set entries up while Home Assistant starts, then let it finish starting.

        Every lock built meanwhile holds its provider setup until all of them
        are waiting, so an entry joining a lock another entry is building
        reaches it before it is ready (#865).
        """
        indices = data.draw(
            st.lists(
                st.sampled_from(self._indices(self._settable)), min_size=2, unique=True
            ),
            label="entries",
        )
        entries = [self._entry(index, locks) for index in indices]
        hass = self.hass

        async def _boot() -> None:
            hass.set_state(CoreState.starting)
            for entry in entries:
                assert await hass.config_entries.async_setup(entry.entry_id)
            await async_run_until_quiet()
            hass.set_state(CoreState.running)
            hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
            await async_run_until_quiet()
            for fault in self._hung:
                await fault.async_wait_held()
            for fault in self._hung:
                fault.clear()
            await self._async_settle()

        self._hang_builds = True
        try:
            self._run(_boot())
        finally:
            self._hang_builds = False
            for fault in self._hung:
                fault.clear()
            self._hung.clear()
        for index in indices:
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
            self._run(
                async_advance_clock(
                    hass, self.freezer, READ_INTERVAL.total_seconds() + 1
                )
            )
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
        The push lock reports one use of a code to everything listening to it.

        Every instance holding a subscription hears the lock, as every
        subscription to a real lock does; the use reaches the entry owning
        the code exactly once.
        """
        index = data.draw(st.sampled_from(self._holders(PUSH_LOCK)), label="entry")
        entry = self.positions[index].entry
        assert entry is not None
        before = self.credential_uses[entry.entry_id]
        listening = [
            instance
            for instance in self._instances(PUSH_LOCK)
            if isinstance(instance, MockLCMPushLock)
            and instance.subscribe_calls > instance.unsubscribe_calls
        ]

        async def _report() -> None:
            for instance in listening:
                instance.async_fire_code_slot_event(index + 1, to_locked=False)

        self._act(_report)
        uses = self.credential_uses[entry.entry_id] - before
        assert uses == 1, f"one use of slot {index + 1} reached {entry.title} {uses}x"

    @precondition(
        lambda self: (
            any(self._held(lock) for lock in LOCKS) and not self.dropped_without_push
        )
    )
    @rule()
    def reload_the_locks_integration(self) -> None:
        """
        Reload the integration the locks belong to.

        Every live instance runs its provider setup again; the invariants
        check that no torn-down one does. Not run once an instance has been
        dropped with its push subscription unimplemented: that one keeps
        listening and sets up again on every reload, a known defect pinned by
        ``tests/test_init.py::
        test_a_lock_dropped_after_its_listener_started_never_sets_up_again``.
        """
        live = [
            (instance, len(instance.service_calls["setup"]))
            for instance in self._live_all()
        ]
        assert self._act(
            lambda: self.hass.config_entries.async_reload(self.lock_entry.entry_id)
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

        self._run(
            async_tick_until(
                hass,
                self.freezer,
                _in_sync,
                max_ticks=MAX_TICKS_TO_SYNC,
                what="every loaded entry's slots in sync with the lock's codes",
            )
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
            held = instance.subscribe_calls - instance.unsubscribe_calls
            is_live = id(instance) in live
            assert held == (1 if is_live else 0), (
                f"{'live' if is_live else 'torn-down'} push instance "
                f"holds {held} subscriptions"
            )

    @invariant()
    def torn_down_instances_never_set_up_again(self) -> None:
        for instance in self._instances():
            if instance.service_calls["unload"] or id(instance) in self.dropped:
                setups = len(instance.service_calls["setup"])
                gone = self._setups_when_gone.setdefault(id(instance), setups)
                assert gone == setups, (
                    f"a torn-down {instance.lock.entity_id} instance set up again"
                )

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
        Home Assistant is stopped before it is exited, or the harness finds
        the instance still running and aborts the whole session.
        """
        started = hasattr(self, "hass")
        try:
            release_all_faults()
            if started:
                self._run(self._async_unload_all())
        finally:
            # Stopped before Home Assistant is, so its stop runs on real time.
            self._frozen.stop()
            try:
                if started:
                    self._run(self._async_stop())
            finally:
                self._stack.close()
                self.loop.run_until_complete(self.loop.shutdown_default_executor())
                self.loop.close()
                asyncio.set_event_loop(None)

    async def _async_unload_all(self) -> None:
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await self.hass.config_entries.async_unload(entry.entry_id)
        await self._async_settle()

    async def _async_stop(self) -> None:
        await self.hass.async_stop(force=True)
        await self._hass_cm.__aexit__(None, None, None)


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
# A hang is already reported by each step's own bound, so the suite's
# whole-test timeout here only has to outlast a shrink -- seconds as a rule,
# but cut short it would report the unshrunk failure, or none. Past
# Hypothesis's own cutoff for a slow shrink, which reports what it has.
TestSharedLockMachine.pytestmark = [pytest.mark.timeout(SHRINK_SECONDS)]
