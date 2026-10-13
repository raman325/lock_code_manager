"""One address's verdict, rule by rule (the credential-model decisions)."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.core.belief import BeliefRecord
from custom_components.lock_code_manager.core.breakers import SlotBreaker
from custom_components.lock_code_manager.core.judge import (
    LastWrite,
    Status,
    Verdict,
    Written,
    judge,
    written,
)
from custom_components.lock_code_manager.core.observations import ObservationLog
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
WRITE_ID = 7

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
        Verdict(Status.OUT_OF_SYNC, write=WANT, charge_for=WRITE_ID),
    ),
    (
        "keypad changed it: contradicted, charged",
        WANT,
        Written.TARGET,
        Holds("9999"),
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT, charge_for=WRITE_ID),
    ),
    (
        "disabled on the lock: always a contradiction (decision 5)",
        WANT,
        Written.TARGET,
        DISABLED,
        True,
        Verdict(Status.OUT_OF_SYNC, write=WANT, charge_for=WRITE_ID),
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
        "last write raised: write again",
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
        Verdict(Status.OUT_OF_SYNC, write=WANT_EMPTY, charge_for=WRITE_ID),
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
    ("desired", "kind", "reading", "ever_read", "expected"),
    [pytest.param(*row[1:], id=row[0]) for row in ROWS],
)
def test_each_rule(
    desired: Desired,
    kind: Written,
    reading: Reading | None,
    ever_read: bool,
    expected: Verdict,
) -> None:
    """Each credential-model rule, one row at a time."""
    seen = None if reading is None else Observation(reading, 10.0)
    last = LastWrite(kind, None if kind is Written.NOTHING else WRITE_ID)
    assert judge(desired, last, seen, ever_read=ever_read, tripped=False) == expected


def test_an_address_nobody_manages_is_left_alone() -> None:
    """With nothing wanted, the judge neither writes nor reports a status."""
    assert judge(
        None,
        LastWrite(Written.NOTHING),
        Observation(Holds("1234"), 10.0),
        ever_read=True,
        tripped=False,
    ) == Verdict(Status.NOT_OURS)


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
    assert judge(
        desired,
        LastWrite(Written.TARGET, WRITE_ID),
        Observation(HOLDS_MASKED, 10.0),
        ever_read=True,
        tripped=True,
        release=release,
    ) == Verdict(Status.SUSPENDED, repair=repair)


def test_written_compares_the_last_write_with_the_target() -> None:
    """The last write is the target, something else, out, failed, or nothing."""
    beliefs = BeliefRecord(b"salt")
    slot = pin_at(1)
    assert written(beliefs, slot, WANT) == LastWrite(Written.NOTHING)
    write = beliefs.invalidate(slot, at=1.0)
    assert written(beliefs, slot, WANT) == LastWrite(Written.IN_FLIGHT, write)
    beliefs.record_failed(slot, write)
    assert written(beliefs, slot, WANT) == LastWrite(Written.FAILED, write)
    write = beliefs.invalidate(slot, at=2.0)
    beliefs.record_pin(slot, write, "1234", Strength.APPLIED, at=3.0)
    assert written(beliefs, slot, WANT) == LastWrite(Written.TARGET, write)
    assert written(beliefs, slot, WantPin("4321")) == LastWrite(Written.OTHER, write)
    assert written(beliefs, slot, WANT_EMPTY) == LastWrite(Written.OTHER, write)


def test_a_read_that_completed_lets_the_judge_decide_even_if_it_said_nothing() -> None:
    """
    No decision before a read completes; a silent read still completes.

    A lock that never says what it holds must still be written, so a read
    that came back with nothing informative counts as read (spec section 1).
    """
    log = ObservationLog()
    slot = pin_at(1)

    def _verdict() -> Verdict:
        return judge(
            WANT,
            LastWrite(Written.NOTHING),
            log.informative_since_write(slot),
            ever_read=log.ever_read(slot),
            tripped=False,
        )

    assert _verdict() == Verdict(Status.UNKNOWN)
    log.observe(slot, NOTHING, 1.0)
    assert _verdict() == Verdict(Status.OUT_OF_SYNC, write=WANT)


def test_two_writes_returning_at_the_same_time_are_charged_separately() -> None:
    """Charges are keyed by write, so a coarse clock cannot merge two failures."""
    beliefs = BeliefRecord(b"salt")
    slot = pin_at(1)
    charged = set()
    for _ in range(2):
        write = beliefs.invalidate(slot, at=100.0)
        beliefs.record_pin(slot, write, "1234", Strength.APPLIED, at=100.0)
        verdict = judge(
            WANT,
            written(beliefs, slot, WANT),
            Observation(EMPTY, 100.0),
            ever_read=True,
            tripped=False,
        )
        charged.add(verdict.charge_for)

    assert len(charged) == 2


def test_a_release_clear_the_lock_ignores_ends_in_a_repair() -> None:
    """
    A code being cleared on its way out is retried once, then reported.

    Its clear is believed and judged like any write; the caller gives
    release clears a breaker of two (design-review decision 6), so the
    second contradicted clear suspends it with the repair that warns the
    code may still open the door. Each write is charged once, however often
    it is judged.
    """
    beliefs = BeliefRecord(b"salt")
    log = ObservationLog()
    breaker = SlotBreaker(limit=2)
    slot = pin_at(5)
    charged: set[int] = set()
    for attempt in range(2):
        start = 10.0 * attempt
        write = beliefs.invalidate(slot, start)
        log.mark_write(slot, start)
        beliefs.record_cleared(slot, write, Strength.SENT, start + 1)
        log.mark_returned(slot, start + 1)
        log.observe(slot, HOLDS_MASKED, start + 2)
        for _wake in range(3):
            verdict = judge(
                WANT_EMPTY,
                written(beliefs, slot, WANT_EMPTY),
                log.informative_since_write(slot),
                ever_read=True,
                tripped=breaker.tripped,
                release=True,
            )
            if verdict.charge_for is not None and verdict.charge_for not in charged:
                charged.add(verdict.charge_for)
                breaker.charge(start + 2)

    assert judge(
        WANT_EMPTY,
        written(beliefs, slot, WANT_EMPTY),
        log.informative_since_write(slot),
        ever_read=True,
        tripped=breaker.tripped,
        release=True,
    ) == Verdict(Status.SUSPENDED, repair=Repair.CODE_MAY_REMAIN)
    assert len(charged) == 2
