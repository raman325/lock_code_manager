"""One address's verdict, rule by rule (the credential-model decisions)."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.core.address import (
    AddressState,
    AddressView,
    Written,
)
from custom_components.lock_code_manager.core.breakers import SlotBreaker
from custom_components.lock_code_manager.core.judge import Status, Verdict, judge
from custom_components.lock_code_manager.core.refusals import Repair
from custom_components.lock_code_manager.core.values import (
    DISABLED,
    EMPTY,
    HOLDS_MASKED,
    NOTHING,
    WANT_EMPTY,
    Desired,
    Holds,
    Observation,
    Reading,
    Strength,
    WantPin,
    pin_at,
)

WANT = WantPin("1234")

ROWS = [
    (
        "not read yet: no decision",
        WANT,
        Written.NOTHING,
        None,
        False,
        Verdict(Status.UNKNOWN),
    ),
    (
        "target written, not read yet: trusted",
        WANT,
        Written.TARGET,
        None,
        False,
        Verdict(Status.IN_SYNC),
    ),
    (
        "lock already holds the target",
        WANT,
        Written.NOTHING,
        Holds("1234"),
        True,
        Verdict(Status.IN_SYNC),
    ),
    (
        "write-only lock: on is trust (decision 1)",
        WANT,
        Written.TARGET,
        HOLDS_MASKED,
        True,
        Verdict(Status.IN_SYNC),
    ),
    (
        "written target read back empty: contradicted, charged (decision 2)",
        WANT,
        Written.TARGET,
        EMPTY,
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT, charge=True),
    ),
    (
        "keypad changed it: contradicted, charged",
        WANT,
        Written.TARGET,
        Holds("9999"),
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT, charge=True),
    ),
    (
        "disabled on the lock: always a contradiction (decision 5)",
        WANT,
        Written.TARGET,
        DISABLED,
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT, charge=True),
    ),
    (
        "target changed: rewrite, nothing charged",
        WANT,
        Written.OTHER,
        Holds("9999"),
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT),
    ),
    (
        "last write failed: write again",
        WANT,
        Written.FAILED,
        None,
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT),
    ),
    (
        "a write is still out: nothing more is written",
        WANT,
        Written.IN_FLIGHT,
        EMPTY,
        True,
        Verdict(Status.OUT_OF_SYNC),
    ),
    (
        "code still there after a clear: believe the lock (decision 12)",
        WANT_EMPTY,
        Written.TARGET,
        HOLDS_MASKED,
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT_EMPTY, charge=True),
    ),
    (
        "cleared and read back empty",
        WANT_EMPTY,
        Written.TARGET,
        EMPTY,
        True,
        Verdict(Status.IN_SYNC),
    ),
    (
        "disabled code where nothing is wanted: clear it",
        WANT_EMPTY,
        Written.NOTHING,
        DISABLED,
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT_EMPTY),
    ),
]


@pytest.mark.parametrize(
    ("desired", "last_write", "reading", "ever_read", "expected"),
    [pytest.param(*row[1:], id=row[0]) for row in ROWS],
)
def test_each_rule(
    desired: Desired,
    last_write: Written,
    reading: Reading | None,
    ever_read: bool,
    expected: Verdict,
) -> None:
    """Each credential-model rule, one row at a time."""
    seen = None if reading is None else Observation(reading, 10.0)
    view = AddressView(last_write, seen, ever_read)
    assert judge(desired, view, tripped=False) == expected


def test_an_address_nobody_manages_is_left_alone() -> None:
    """With nothing wanted, the judge neither writes nor reports a status."""
    view = AddressView(Written.NOTHING, Observation(Holds("1234"), 1.0), True)
    assert judge(None, view, tripped=False) == Verdict(Status.NOT_OURS)


@pytest.mark.parametrize(
    ("desired", "release", "repair"),
    [
        pytest.param(WANT, False, Repair.SLOT_SUSPENDED, id="set"),
        pytest.param(WANT_EMPTY, False, Repair.CODE_MAY_REMAIN, id="clear"),
        pytest.param(WANT_EMPTY, True, Repair.CODE_MAY_REMAIN, id="release clear"),
    ],
)
def test_a_tripped_breaker_suspends(
    desired: Desired, release: bool, repair: Repair
) -> None:
    """Suspended until the target changes; a clear that won't take warns of the code."""
    view = AddressView(Written.TARGET, Observation(HOLDS_MASKED, 1.0), True)
    assert judge(desired, view, tripped=True, release=release) == Verdict(
        Status.SUSPENDED, repair=repair
    )


def test_a_read_that_completed_lets_the_judge_decide_even_if_it_said_nothing() -> None:
    """A lock that never says what it holds must still be written (spec section 1)."""
    state = AddressState(pin_at(1), b"salt")
    assert judge(WANT, state.view(WANT), tripped=False) == Verdict(Status.UNKNOWN)
    state.observe(NOTHING, 1.0)
    assert judge(WANT, state.view(WANT), tripped=False) == Verdict(
        Status.OUT_OF_SYNC, write=WANT
    )


def _contradicted_clear(state: AddressState, start: float) -> None:
    """Clear the address, and have the lock still show a code after."""
    with state.start_write(WANT_EMPTY, start) as write:
        write.returned(Strength.SENT, start + 1)
    state.observe(HOLDS_MASKED, start + 2)


def test_a_release_clear_the_lock_ignores_ends_in_a_repair() -> None:
    """
    A code being cleared on its way out is retried once, then reported.

    The caller gives a release clear a breaker of two (design-review decision
    6) and charges each write once however often it is judged, so the second
    contradicted clear suspends it with the repair that warns the code may
    still open the door.
    """
    state = AddressState(pin_at(5), b"salt")
    breaker = SlotBreaker(limit=2)
    for attempt in range(2):
        _contradicted_clear(state, 10.0 * attempt)
        for _wake in range(3):
            verdict = judge(
                WANT_EMPTY,
                state.view(WANT_EMPTY),
                tripped=breaker.tripped,
                release=True,
            )
            if verdict.charge and state.take_charge():
                breaker.charge(10.0 * attempt + 2)

    assert judge(
        WANT_EMPTY, state.view(WANT_EMPTY), tripped=breaker.tripped, release=True
    ) == Verdict(Status.SUSPENDED, repair=Repair.CODE_MAY_REMAIN)
