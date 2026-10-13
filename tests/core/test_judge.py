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
    Holds,
    Observation,
    Reading,
    Strength,
    WantPin,
    pin_at,
)

WANT = WantPin("1234")
WROTE_AT = 2.0


def _seen(reading: Reading) -> Observation:
    return Observation(reading, 10.0)


@pytest.mark.parametrize(
    ("desired", "last_write", "reading", "ever_read", "expected"),
    [
        pytest.param(
            WANT,
            Written.NOTHING,
            None,
            False,
            Verdict(Status.UNKNOWN),
            id="not read yet: no decision",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            None,
            False,
            Verdict(Status.IN_SYNC),
            id="target written, not read yet: trusted",
        ),
        pytest.param(
            WANT,
            Written.NOTHING,
            Holds("1234"),
            True,
            Verdict(Status.IN_SYNC),
            id="lock already holds the target",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            HOLDS_MASKED,
            True,
            Verdict(Status.IN_SYNC),
            id="write-only lock: on is trust (decision 1)",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            EMPTY,
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT, charge_for=WROTE_AT),
            id="written target read back empty: contradicted, charged (decision 2)",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            Holds("9999"),
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT, charge_for=WROTE_AT),
            id="keypad changed it: contradicted, charged",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            DISABLED,
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT, charge_for=WROTE_AT),
            id="disabled on the lock: always a contradiction (decision 5)",
        ),
        pytest.param(
            WANT,
            Written.OTHER,
            Holds("9999"),
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT),
            id="target changed: rewrite, nothing charged",
        ),
        pytest.param(
            WANT,
            Written.UNSETTLED,
            None,
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT),
            id="last write raised: write again",
        ),
        pytest.param(
            WANT_EMPTY,
            Written.TARGET,
            HOLDS_MASKED,
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT_EMPTY, charge_for=WROTE_AT),
            id="code still there after a clear: believe the lock (decision 12)",
        ),
        pytest.param(
            WANT_EMPTY,
            Written.TARGET,
            EMPTY,
            True,
            Verdict(Status.IN_SYNC),
            id="cleared and read back empty",
        ),
        pytest.param(
            WANT_EMPTY,
            Written.NOTHING,
            DISABLED,
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT_EMPTY),
            id="disabled code where nothing is wanted: clear it",
        ),
    ],
)
def test_each_rule(
    desired, last_write: Written, reading: Reading | None, ever_read: bool, expected
) -> None:
    """Each credential-model rule, one row at a time."""
    seen = None if reading is None else _seen(reading)
    last = LastWrite(last_write, None if last_write is Written.NOTHING else WROTE_AT)
    assert judge(desired, last, seen, ever_read=ever_read, tripped=False) == expected


def test_an_address_nobody_manages_is_left_alone() -> None:
    """With nothing wanted, the judge neither writes nor reports a status."""
    assert judge(
        None,
        LastWrite(Written.NOTHING),
        _seen(Holds("1234")),
        ever_read=True,
        tripped=False,
    ) == Verdict(Status.NOT_OURS)


@pytest.mark.parametrize(
    ("release", "repair"),
    [(False, Repair.SLOT_SUSPENDED), (True, Repair.CODE_MAY_REMAIN)],
)
def test_a_tripped_breaker_suspends(release: bool, repair: Repair) -> None:
    """Suspended until the target changes; a release clear asks the user to check."""
    assert judge(
        WANT_EMPTY if release else WANT,
        LastWrite(Written.TARGET, WROTE_AT),
        _seen(HOLDS_MASKED),
        ever_read=True,
        tripped=True,
        release=release,
    ) == Verdict(Status.SUSPENDED, repair=repair)


def test_written_compares_the_last_write_with_the_target() -> None:
    """The last write is the target, something else, unsettled, or nothing."""
    beliefs = BeliefRecord(b"salt")
    slot = pin_at(1)
    assert written(beliefs, slot, WANT) == LastWrite(Written.NOTHING)
    beliefs.invalidate(slot, at=1.0)
    assert written(beliefs, slot, WANT) == LastWrite(Written.UNSETTLED, 1.0)
    beliefs.record_pin(slot, "1234", Strength.APPLIED, at=2.0)
    assert written(beliefs, slot, WANT) == LastWrite(Written.TARGET, 2.0)
    assert written(beliefs, slot, WantPin("4321")) == LastWrite(Written.OTHER, 2.0)
    assert written(beliefs, slot, WANT_EMPTY) == LastWrite(Written.OTHER, 2.0)


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


def test_a_release_clear_the_lock_ignores_ends_in_a_repair() -> None:
    """
    A code being cleared on its way out is retried once, then reported.

    Its clear is believed and judged like any write; the caller gives
    release clears a breaker of two (design-review decision 6), so the
    second contradicted clear suspends it with the repair that warns the
    code may still open the door.
    """
    beliefs = BeliefRecord(b"salt")
    log = ObservationLog()
    breaker = SlotBreaker(limit=2)
    slot = pin_at(5)
    charged: set[float] = set()
    for attempt in range(2):
        start = 10.0 * attempt
        beliefs.invalidate(slot, start)
        log.mark_write(slot, start)
        beliefs.record_cleared(slot, Strength.SENT, start + 1)
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
