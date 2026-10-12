"""
Property-based tests for the questions "which entry manages this lock or slot".

Each question counts a different set of entries, and each set is what its
callers rely on today. Getting one wrong is silent in both directions:

* **counting an entry it should not** keeps a slot polled, attributed or
  wanted on the lock for an entry that will never sync it;
* **missing an entry it should count** lets a new entry be issued numbers
  another one holds, or reports another entry's code as unmanaged.

The expected answers are computed here from the drawn entries alone, so the
oracle states each set without going through the code under test. The
configuration each entry answers with follows ``get_entry_config``: a loaded
or unloading entry answers from its cached view, any other from storage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from hypothesis import given, strategies as st
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_USER,
    ConfigEntryDisabler,
    ConfigEntryState,
)
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import CONF_LOCKS, DOMAIN
from custom_components.lock_code_manager.domain.config import EntryConfig
from custom_components.lock_code_manager.domain.queries import (
    another_enabled_entry_configures,
    configured_owner,
    entries_configuring,
    slots_configured_on,
    syncing_owner,
)

from ..common import user_subentries

LOCKS = ("lock.front", "lock.back", "lock.side")
SLOTS = tuple(range(1, 6))

# Written out rather than imported: the oracle states which load states keep
# an entry's slots wanted on the lock, so it cannot borrow the answer.
KEEPS_ITS_SLOTS = frozenset(
    {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.NOT_LOADED,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
    }
)
CACHE_IS_CURRENT = frozenset(
    {ConfigEntryState.LOADED, ConfigEntryState.UNLOAD_IN_PROGRESS}
)


@dataclass(frozen=True)
class Layout:
    """Which locks an entry lists and which slot numbers its users hold."""

    locks: frozenset[str]
    slots: frozenset[int]


@dataclass(frozen=True)
class EntrySpec:
    """One drawn entry: what it stores, what it cached, and how it stands."""

    stored: Layout
    cached: Layout | None
    state: ConfigEntryState
    disabled: bool
    ignored: bool

    @property
    def answers_with(self) -> Layout:
        """The configuration this entry answers with."""
        if self.cached is not None and self.state in CACHE_IS_CURRENT:
            return self.cached
        return self.stored

    @property
    def enabled(self) -> bool:
        """Neither disabled nor ignored, in any load state."""
        return not self.disabled and not self.ignored

    @property
    def keeps_its_slots(self) -> bool:
        """Enabled, and in a load state whose slots are wanted on the lock."""
        return self.enabled and self.state in KEEPS_ITS_SLOTS

    def configures(self, lock: str) -> bool:
        """Whether this entry lists ``lock``."""
        return lock in self.answers_with.locks

    def configures_slot(self, lock: str, slot: int) -> bool:
        """Whether this entry lists ``lock`` and a user of it holds ``slot``."""
        return self.configures(lock) and slot in self.answers_with.slots


LAYOUTS = st.builds(
    Layout,
    locks=st.frozensets(st.sampled_from(LOCKS), max_size=3),
    slots=st.frozensets(st.sampled_from(SLOTS), max_size=4),
)

ENTRY_SPECS = st.builds(
    EntrySpec,
    stored=LAYOUTS,
    cached=st.none() | LAYOUTS,
    state=st.sampled_from(ConfigEntryState),
    disabled=st.booleans(),
    ignored=st.booleans(),
)

# Several entries over three locks and five slots, so entries overlap on a
# lock and on a slot often enough that "first match wins" is exercised.
ENTRY_SETS = st.lists(ENTRY_SPECS, max_size=5)


def _layout_entry(layout: Layout, **kwargs: Any) -> MockConfigEntry:
    """Build an entry whose locks and users are ``layout``."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: sorted(layout.locks)},
        subentries_data=user_subentries(
            {
                slot: {CONF_NAME: f"User {slot}", CONF_PIN: "1234", CONF_ENABLED: True}
                for slot in sorted(layout.slots)
            }
        ),
        **kwargs,
    )


def _entry(spec: EntrySpec) -> MockConfigEntry:
    """Build the real config entry ``spec`` describes."""
    entry = _layout_entry(
        spec.stored,
        state=spec.state,
        disabled_by=ConfigEntryDisabler.USER if spec.disabled else None,
        source=SOURCE_IGNORE if spec.ignored else SOURCE_USER,
    )
    if spec.cached is not None:
        entry.runtime_data = SimpleNamespace(
            config=EntryConfig.from_entry(_layout_entry(spec.cached))
        )
    return entry


def _with_entries(
    specs: list[EntrySpec],
    probe: Callable[[HomeAssistant, list[MockConfigEntry]], None],
) -> None:
    """Add ``specs`` to a fresh Home Assistant and run ``probe`` against them."""

    async def _run() -> None:
        async with async_test_home_assistant() as hass:
            entries = [_entry(spec) for spec in specs]
            for entry in entries:
                entry.add_to_hass(hass)
            try:
                probe(hass, entries)
            finally:
                await hass.async_stop(force=True)

    asyncio.run(_run())


def _first(
    specs: list[EntrySpec],
    entries: list[MockConfigEntry],
    wanted: Callable[[EntrySpec], bool],
) -> MockConfigEntry | None:
    """Return the first entry whose spec satisfies ``wanted``, in added order."""
    return next(
        (entry for spec, entry in zip(specs, entries, strict=True) if wanted(spec)),
        None,
    )


@given(specs=ENTRY_SETS)
def test_entries_configuring_lists_every_entry_in_any_state(
    specs: list[EntrySpec],
) -> None:
    """Every entry listing the lock, disabled and ignored included, in added order."""

    def probe(hass: HomeAssistant, entries: list[MockConfigEntry]) -> None:
        for lock in LOCKS:
            expected = [
                entry
                for spec, entry in zip(specs, entries, strict=True)
                if spec.configures(lock)
            ]
            assert list(entries_configuring(hass, lock)) == expected

    _with_entries(specs, probe)


@given(specs=ENTRY_SETS, excluding=st.none() | st.integers(min_value=0, max_value=4))
def test_slots_configured_on_counts_every_entry_in_any_state(
    specs: list[EntrySpec], excluding: int | None
) -> None:
    """Every entry listing the lock counts, disabled and ignored included."""

    def probe(hass: HomeAssistant, entries: list[MockConfigEntry]) -> None:
        left_out = (
            entries[excluding]
            if excluding is not None and excluding < len(entries)
            else None
        )
        for lock in LOCKS:
            expected = {
                slot
                for spec, entry in zip(specs, entries, strict=True)
                if entry is not left_out and spec.configures(lock)
                for slot in spec.answers_with.slots
            }
            assert slots_configured_on(hass, lock, excluding=left_out) == expected

    _with_entries(specs, probe)


@given(specs=ENTRY_SETS)
def test_configured_owner_is_the_first_entry_listing_the_slot(
    specs: list[EntrySpec],
) -> None:
    """Every entry counts, in any state; the first one added wins."""

    def probe(hass: HomeAssistant, entries: list[MockConfigEntry]) -> None:
        for lock in LOCKS:
            for slot in SLOTS:
                expected = _first(
                    specs, entries, lambda spec: spec.configures_slot(lock, slot)
                )
                assert configured_owner(hass, lock, slot) is expected

    _with_entries(specs, probe)


@given(specs=ENTRY_SETS)
def test_syncing_owner_counts_only_entries_keeping_their_slots(
    specs: list[EntrySpec],
) -> None:
    """Only an enabled entry in a state that keeps its slots owns one."""

    def probe(hass: HomeAssistant, entries: list[MockConfigEntry]) -> None:
        for lock in LOCKS:
            for slot in SLOTS:
                expected = _first(
                    specs,
                    entries,
                    lambda spec: (
                        spec.keeps_its_slots and spec.configures_slot(lock, slot)
                    ),
                )
                assert syncing_owner(hass, lock, slot) is expected

    _with_entries(specs, probe)


@given(specs=ENTRY_SETS)
def test_another_enabled_entry_configures_ignores_disabled_and_ignored(
    specs: list[EntrySpec],
) -> None:
    """Another entry counts if it is neither disabled nor ignored, in any state."""

    def probe(hass: HomeAssistant, entries: list[MockConfigEntry]) -> None:
        for asking in entries:
            for lock in LOCKS:
                expected = any(
                    entry is not asking and spec.enabled and spec.configures(lock)
                    for spec, entry in zip(specs, entries, strict=True)
                )
                assert another_enabled_entry_configures(hass, asking, lock) is expected

    _with_entries(specs, probe)
