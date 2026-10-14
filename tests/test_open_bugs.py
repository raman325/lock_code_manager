"""
Open bugs, each pinned by a strict xfail at the seam.

Each test states the behavior the issue asks for, driven and checked only
through Home Assistant and the mock lock. While the bug is open the test
fails; a fix makes it pass, and the strict marker then fails the run until
the marker is removed.
"""

from __future__ import annotations

from collections.abc import Iterator
import itertools
import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir, translation

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    SERVICE_HARD_REFRESH_USERCODES,
    SUBENTRY_TYPE_USER,
)
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.queries import get_entry_config
from custom_components.lock_code_manager.providers._base import (
    MIN_OPERATION_DELAY,
    OPERATION_TIMEOUT,
)

from .common import (
    BASE_CONFIG,
    CONFIG_ENTRY_VERSION,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    TWO_SLOTS_ON_LOCK_1,
    LockFault,
    MockLCMLock,
    async_advance_clock,
    async_advance_sync_ticks,
    async_configure_flow,
    async_configure_options,
    async_run_until_quiet,
    async_set_slot_pin,
    async_step_clock_until,
    async_switch_slot,
    async_tick_until,
    current_entry,
    in_sync_entity_id,
    lcm_issue_ids,
    mock_lock,
    short_stop_grace,
    when_mock_lock_built,
)


@pytest.fixture
def short_grace() -> Iterator[None]:
    """
    Shorten the stop grace for the whole test, teardown included.

    Adding users reloads the entry, and each unload waits out the grace on
    the real clock. Requested first, this is torn down last.
    """
    with short_stop_grace():
        yield


async def _async_setup_two_slots(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, *, paced: bool = False
) -> MockConfigEntry:
    """Set up an entry with slots 1 and 2 on lock 1, both in sync."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=TWO_SLOTS_ON_LOCK_1, unique_id="Open bugs"
    )
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    # Paced, setup waits out the delay between its reads on the frozen clock;
    # the first setup in a run also imports the platforms on threads.
    await async_step_clock_until(
        hass, freezer, setup.done, 100, "setup", step=MIN_OPERATION_DELAY / 2
    )
    assert setup.result()

    def _both_in_sync() -> bool:
        try:
            in_syncs = [in_sync_entity_id(hass, entry, slot) for slot in (1, 2)]
        except AssertionError:
            return False  # the per-lock entities are added after setup returns
        return all(
            (state := hass.states.get(e)) is not None and state.state == STATE_ON
            for e in in_syncs
        )

    await async_tick_until(
        hass,
        freezer,
        _both_in_sync,
        max_ticks=5,
        what="both slots in sync",
        settle=not paced,
    )
    return entry


@pytest.mark.xfail(
    strict=True,
    reason="#1622: a release clear the lock acknowledges but ignores is never checked",
)
async def test_a_deleted_users_code_does_not_stay_on_the_lock_unreported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    A clear the lock ignores, when a user is deleted, is retried or reported.

    Otherwise the deleted user's PIN keeps opening the door and nothing says so.
    """
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    lock.ignore("clear", 2)
    removed = get_entry_config(entry).name_for(2)
    subentry = next(s for s in entry.subentries.values() if s.title == removed)

    assert hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()
    await async_advance_sync_ticks(hass, freezer, 10)

    assert 2 not in lock.codes or lcm_issue_ids(hass)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="#1642: a refused write does not count toward the lock's pacing",
)
@pytest.mark.paced
async def test_the_operation_after_a_refused_write_is_paced(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """Every operation that reaches the lock, refused or not, is spaced from the next."""
    entry = await _async_setup_two_slots(hass, freezer, paced=True)
    lock = mock_lock(entry)
    await async_advance_sync_ticks(hass, freezer, 3, settle=False)
    lock.run_times.clear()
    lock.refuse("set", 1, times=1)

    await async_set_slot_pin(hass, entry, 1, "1111")
    await async_set_slot_pin(hass, entry, 2, "2222")
    await async_advance_sync_ticks(hass, freezer, 4, settle=False)

    gaps = [b - a for a, b in itertools.pairwise(lock.run_times)]
    # Unloaded before asserting: the unload waits out the pacing on the
    # frozen clock, which a failed test's teardown would never move.
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await async_advance_clock(hass, freezer, 10 * MIN_OPERATION_DELAY, settle=False)
    assert unload.done()
    assert len(gaps) >= 2
    assert min(gaps) >= MIN_OPERATION_DELAY


@pytest.mark.xfail(
    strict=True,
    reason="#1643: strikes against a PIN no longer wanted count against the new one",
)
async def test_a_pin_changed_while_a_write_is_pending_starts_with_no_strikes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """The new PIN's first failure does not suspend the slot."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"

    # Two strikes against 4321, then it lands, and the read confirming it fails.
    lock.fail("set", 1, times=2)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_sync_ticks(hass, freezer, 2)
    lock.fail("get", times=1)
    await async_advance_sync_ticks(hass, freezer)

    # The user changes the PIN while 4321 waits, and its first write fails.
    await async_set_slot_pin(hass, entry, 1, "5555")
    lock.fail("set", 1, times=1)
    await async_advance_sync_ticks(hass, freezer, 4)

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert lock.codes[1] == "5555"
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="#1644: writes that hang every time never escalate while reads succeed",
)
async def test_a_write_that_hangs_every_time_is_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """A slot whose write never returns is suspended with a repair, not retried forever."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    wedge = lock.hang("set", 1)

    await async_set_slot_pin(hass, entry, 1, "4321")
    for _ in range(6):
        await async_advance_clock(hass, freezer, OPERATION_TIMEOUT, settle=False)

    issues = lcm_issue_ids(hass)
    wedge.clear()
    assert issues
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason="#1644: writes failing as disconnected never escalate while reads succeed",
)
async def test_writes_failing_as_disconnected_are_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """A slot whose every write fails, while reads answer, is suspended with a repair."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    lock.fail("set", 1, error=LockDisconnected)

    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_sync_ticks(hass, freezer, 60)

    assert lcm_issue_ids(hass)
    lock.clear_faults()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open: a clear the lock acknowledges but ignores is sent again every "
        "tick, never charged and never reported"
    ),
)
async def test_a_clear_the_lock_keeps_ignoring_is_reported(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """A slot turned off whose clear never sticks is suspended with a repair."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    lock.ignore("clear", 1)

    await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
    await async_advance_sync_ticks(hass, freezer, 30)

    clears = [call for call in lock.service_calls["clear_usercode"] if call[0] == 1]
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    )
    lock.clear_faults()
    await hass.config_entries.async_unload(entry.entry_id)
    assert len(clears) <= MAX_SYNC_ATTEMPTS + 1
    assert issue is not None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open: strikes against a PIN survive the slot being turned off, so the "
        "slot can suspend for a rejected code before its clear is sent"
    ),
)
async def test_a_slot_turned_off_after_failed_writes_is_cleared(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """Turning a failing slot off sends its clear, rather than suspending it for the set."""
    entry = await _async_setup_two_slots(hass, freezer)
    lock = mock_lock(entry)
    lock.fail("set", 1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_sync_ticks(hass, freezer, MAX_SYNC_ATTEMPTS)

    await async_switch_slot(hass, entry, 1, SERVICE_TURN_OFF)
    await async_advance_sync_ticks(hass, freezer, 4)

    lock.clear_faults()
    codes = dict(lock.codes)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    )
    await hass.config_entries.async_unload(entry.entry_id)
    assert 1 not in codes
    assert issue is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open: two entries already managing one slot (from before #1626) fight "
        "over it and nothing reports the overlap"
    ),
)
async def test_an_existing_slot_overlap_is_reported_not_fought_over(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """Two entries on lock 1's slot 1 with different PINs raise a repair, or stop writing."""
    entries = []
    for unique_id, pin in (("first", "1111"), ("second", "2222")):
        entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [LOCK_1_ENTITY_ID],
                CONF_SLOTS: {
                    1: {CONF_NAME: unique_id, CONF_PIN: pin, CONF_ENABLED: True}
                },
            },
            unique_id=unique_id,
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)

    await async_advance_sync_ticks(hass, freezer, 20)

    lock = mock_lock(entries[0])
    writes = [call for call in lock.service_calls["set_usercode"] if call[0] == 1]
    # The mock lock's own code at slot 2 raises an unmanaged-code repair;
    # that one is not about the overlap.
    issues = [i for i in lcm_issue_ids(hass) if not i.startswith("unmanaged_code")]
    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    assert issues or len(writes) <= 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open (#1536 residual): adding users through the user subentry flow "
        "waits on the lock read with no progress step"
    ),
)
async def test_adding_users_shows_progress_while_the_lock_is_read(
    short_grace: None,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The dialog reports a slow read instead of freezing until the lock answers."""
    entry = lock_code_manager_config_entry
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "several"}
    )
    held = mock_lock(entry).hang("get")

    # Only the locks built for the read hang; the reload after the users
    # are added builds new ones, which must answer.
    with when_mock_lock_built(lambda lock: lock.hang("get")) as built:
        submitting = hass.async_create_task(
            hass.config_entries.subentries.async_configure(
                flow["flow_id"],
                {CONF_USERS: {"user3": {CONF_ENABLED: True, CONF_PIN: "3333"}}},
            )
        )
        await async_run_until_quiet()
    answered = submitting.done()
    for fault in (held, *built):
        fault.clear()
    result = await submitting

    assert answered
    assert result["type"] == "progress"


def _entry(
    hass: HomeAssistant, unique_id: str, lock: str, slots: dict[int, str]
) -> MockConfigEntry:
    """Add an entry on ``lock`` with one user per slot, named and PINned as given."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock],
            CONF_SLOTS: {
                slot: {CONF_NAME: name, CONF_PIN: f"{slot}" * 4, CONF_ENABLED: True}
                for slot, name in slots.items()
            },
        },
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    return entry


async def _async_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, lock: str, locks: list[str]
) -> dict[str, Any]:
    """Start a reauth of ``entry`` naming ``lock`` as missing, and submit ``locks``."""
    entry.async_start_reauth(hass, context={"lock_entity_id": lock})
    await hass.async_block_till_done()
    [flow] = entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    return await async_configure_flow(hass, flow["flow_id"], {CONF_LOCKS: locks})


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open: reauth checks every lock it is given for overlaps, so an "
        "overlap the entry already carries blocks every reauth"
    ),
)
async def test_reauth_completes_with_an_overlap_the_entry_already_has(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Dropping the gone lock is not refused over an overlap the kept lock already had."""
    gone = "lock.front_door"
    entry = _entry(hass, "first", LOCK_1_ENTITY_ID, {1: "test1", 2: "test2"})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_LOCKS: [LOCK_1_ENTITY_ID, gone]}
    )
    _entry(hass, "second", LOCK_1_ENTITY_ID, {1: "other"})

    result = await _async_reauth(hass, entry, gone, [LOCK_1_ENTITY_ID])

    assert result.get("errors") in (None, {})
    assert result.get("reason") == "locks_updated"


@pytest.mark.xfail(
    strict=True,
    reason="open: reauth accepts the very lock it was opened for being gone",
)
async def test_reauth_refuses_the_lock_that_is_gone(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Submitting the seeded list, gone lock and all, is an error naming that lock."""
    gone = "lock.front_door"
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID, gone],
            CONF_SLOTS: {1: {CONF_NAME: "test1", CONF_PIN: "1111", CONF_ENABLED: True}},
        },
        unique_id="gone",
    )
    entry.add_to_hass(hass)

    result = await _async_reauth(hass, entry, gone, [LOCK_1_ENTITY_ID, gone])

    assert result["type"] == "form"
    assert result["errors"]
    # The description already names it; the error has to as well.
    assert any(
        gone in str(value)
        for key, value in result["description_placeholders"].items()
        if key != "lock"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open: an error's own {lock} placeholder overwrites the reauth "
        "description's, so the form names the wrong lock as missing"
    ),
)
async def test_reauth_keeps_naming_the_missing_lock_after_an_error(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """After an overlap error on another lock, the description still names the missing one."""
    entry = _entry(hass, "first", LOCK_1_ENTITY_ID, {1: "test1", 2: "test2"})
    _entry(hass, "second", LOCK_2_ENTITY_ID, {1: "other"})

    result = await _async_reauth(hass, entry, LOCK_1_ENTITY_ID, [LOCK_2_ENTITY_ID])

    assert result["errors"] == {"base": "slots_already_configured"}
    assert result["description_placeholders"]["lock"] == LOCK_1_ENTITY_ID


@pytest.mark.xfail(
    strict=True,
    reason="open: an overlap error lists slot numbers, which the user never sees",
)
async def test_an_overlap_error_names_the_users_in_the_way(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """The error says which users collide, by name, so the user can act on it."""
    entry = _entry(hass, "first", LOCK_1_ENTITY_ID, {1: "test1", 2: "test2"})
    _entry(hass, "second", LOCK_2_ENTITY_ID, {1: "Grandma"})

    result = await _async_reauth(hass, entry, LOCK_1_ENTITY_ID, [LOCK_2_ENTITY_ID])
    translations = await translation.async_get_translations(
        hass, "en", "config", {DOMAIN}
    )
    template = translations[
        f"component.{DOMAIN}.config.error.{result['errors']['base']}"
    ]
    rendered = re.sub(
        r"\{(\w+)\}",
        lambda match: str(result["description_placeholders"].get(match[1], "")),
        template,
    )

    assert "Grandma" in rendered


@pytest.mark.xfail(
    strict=True,
    reason=(
        "open: adding a lock in the options flow never reads it, so codes "
        "already on it are overwritten"
    ),
)
async def test_adding_a_lock_keeps_the_codes_already_on_it(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """A code someone set at lock 2's keypad survives the lock joining the entry."""
    entry = _entry(hass, "first", LOCK_1_ENTITY_ID, {1: "test1", 2: "test2"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await async_configure_options(
        hass, flow["flow_id"], {CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID]}
    )
    await hass.async_block_till_done()
    await async_advance_sync_ticks(hass, freezer, 5)

    lock_2 = entry.runtime_data.locks.get(LOCK_2_ENTITY_ID)
    codes = dict(lock_2.codes) if lock_2 is not None else None
    await hass.config_entries.async_unload(entry.entry_id)
    # Either the flow refused the lock, or the codes on it were left alone.
    assert result.get("errors") or codes == {1: "1234", 2: "5678"}


@pytest.mark.xfail(
    strict=True,
    reason="open: refreshing a lock no loaded entry manages succeeds silently",
)
async def test_refreshing_a_lock_nothing_manages_says_so(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    The action tells the user nothing was refreshed, instead of succeeding.

    ``tests/test_services.py`` pins today's silence; a fix rewrites both.
    """
    entry = await _async_setup_two_slots(hass, freezer)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_HARD_REFRESH_USERCODES,
            {ATTR_ENTITY_ID: LOCK_1_ENTITY_ID},
            blocking=True,
        )


def _lock_io(lock: MockLCMLock) -> int:
    """Count the operations that reached ``lock``: everything but its lifecycle."""
    return sum(call not in ("setup", "unload") for call in lock.call_order)


async def _async_hold_a_write(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> tuple[MockConfigEntry, MockLCMLock, LockFault]:
    """Set up an entry on lock 1 with a PIN change on its way, held at the lock."""
    try:
        entry = await _async_setup_two_slots(hass, freezer)
        lock = mock_lock(entry)
    except AssertionError as err:
        pytest.fail(f"setup: the first entry did not come up in sync: {err}")
    held = lock.hang("set", 1)
    await async_set_slot_pin(hass, entry, 1, "4321")
    await async_advance_clock(hass, freezer, MIN_OPERATION_DELAY, settle=False)
    await held.async_wait_held()
    return entry, lock, held


async def _async_reach_lock_1_while_held(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    lock: MockLCMLock,
    held: LockFault,
    entry: MockConfigEntry,
) -> int:
    """
    Set up ``entry`` beside the held write; return what reached lock 1 meanwhile.

    Counts the held lock's own operations from now on, and every lock 1 a
    provider builds along the way, so neither joining the lock nor building
    a new one hides a call. Setup may finish or wait its turn; once the
    count is taken the write is let go and setup must succeed.
    """
    before = _lock_io(lock)
    entry.add_to_hass(hass)
    with when_mock_lock_built(lambda built: built) as built:
        setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
        for _ in range(5):
            await async_advance_clock(hass, freezer, 2, settle=False)
        if not held.held:
            pytest.fail("setup: the write is no longer held")
        reached = (_lock_io(lock) - before) + sum(
            _lock_io(other)
            for other in built
            if other.lock.entity_id == LOCK_1_ENTITY_ID
        )
        held.clear()
        await async_step_clock_until(hass, freezer, setup.done, 100, "the setup")
    if not setup.result():
        pytest.fail(f"setup: {entry.title} did not set up")
    return reached


async def _async_unload_all(hass: HomeAssistant) -> None:
    """Unload every entry, failing the test on one that will not unload."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and not (
            await hass.config_entries.async_unload(entry.entry_id)
        ):
            pytest.fail(f"teardown: {entry.title} did not unload")


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "open: an entry setting up beside one still unloading talks to the "
        "lock past the write in flight"
    ),
)
async def test_an_entry_set_up_beside_one_unloading_waits_for_its_write(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    A second entry on a lock waits its turn while the first one unloads.

    The first entry is unloading with a write still at the lock: its stop
    grace lets the write finish. An entry setting up meanwhile joins only a
    lock held by a loaded entry, so it builds its own and reads the lock
    while the write is in flight -- two at once on one lock, which the turn
    exists to prevent (#139). The design review's handle decision (R2, Q1)
    has it join the lock already there.
    """
    first, lock, held = await _async_hold_a_write(hass, freezer)
    unload = hass.async_create_task(hass.config_entries.async_unload(first.entry_id))
    await async_run_until_quiet()
    if first.state is not ConfigEntryState.UNLOAD_IN_PROGRESS:
        pytest.fail(f"setup: the first entry is {first.state}, not unloading")

    # Well inside the first entry's stop grace.
    reached = await _async_reach_lock_1_while_held(
        hass, freezer, lock, held, current_entry("Beside the unload", {3: "3333"})
    )
    await async_step_clock_until(hass, freezer, unload.done, 100, "first unload")
    await _async_unload_all(hass)
    assert reached == 0


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "open: the migration's unmanaged-code sweep reads a lock past the "
        "write in flight"
    ),
)
async def test_the_unmanaged_code_sweep_waits_for_a_write_in_flight(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_lock_config_entry,
) -> None:
    """
    An entry migrating beside a busy lock reads it in that lock's turn.

    Migrating from version 3 or older sweeps each of the entry's locks for
    codes nothing manages, through a throwaway provider of its own. That
    read skips the turn the entry already holding the lock is using, so it
    reaches the lock while that entry's write is still in flight.
    """
    _first, lock, held = await _async_hold_a_write(hass, freezer)
    migrating = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Legacy")

    reached = await _async_reach_lock_1_while_held(hass, freezer, lock, held, migrating)
    if migrating.version != CONFIG_ENTRY_VERSION:
        pytest.fail(f"setup: the entry migrated to {migrating.version} only")
    await _async_unload_all(hass)
    assert reached == 0
