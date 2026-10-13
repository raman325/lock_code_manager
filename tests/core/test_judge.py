"""One address's verdict, rule by rule (the credential-model decisions)."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.core.belief import BeliefRecord
from custom_components.lock_code_manager.core.judge import (
    Status,
    Verdict,
    Written,
    judge,
    written,
)
from custom_components.lock_code_manager.core.refusals import Repair
from custom_components.lock_code_manager.core.values import (
    DISABLED,
    EMPTY,
    HOLDS_MASKED,
    WANT_EMPTY,
    Holds,
    Observation,
    Reading,
    Strength,
    WantPin,
    pin_at,
)

WANT = WantPin("1234")


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
            Verdict(Status.OUT_OF_SYNC, write=WANT, charge=True),
            id="written target read back empty: contradicted, charged (decision 2)",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            Holds("9999"),
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT, charge=True),
            id="keypad changed it: contradicted, charged",
        ),
        pytest.param(
            WANT,
            Written.TARGET,
            DISABLED,
            True,
            Verdict(Status.OUT_OF_SYNC, write=WANT, charge=True),
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
            Verdict(Status.OUT_OF_SYNC, write=WANT_EMPTY, charge=True),
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
    assert (
        judge(desired, last_write, seen, ever_read=ever_read, tripped=False) == expected
    )


def test_an_address_nobody_manages_is_left_alone() -> None:
    """With nothing wanted, the judge neither writes nor reports a status."""
    assert judge(
        None, Written.NOTHING, _seen(Holds("1234")), ever_read=True, tripped=False
    ) == Verdict(Status.NOT_OURS)


@pytest.mark.parametrize(
    ("release", "repair"),
    [(False, Repair.SLOT_SUSPENDED), (True, Repair.CODE_MAY_REMAIN)],
)
def test_a_tripped_breaker_suspends(release: bool, repair: Repair) -> None:
    """Suspended until the target changes; a release clear asks the user to check."""
    assert judge(
        WANT_EMPTY if release else WANT,
        Written.TARGET,
        _seen(HOLDS_MASKED),
        ever_read=True,
        tripped=True,
        release=release,
    ) == Verdict(Status.SUSPENDED, repair=repair)


def test_written_compares_the_last_write_with_the_target() -> None:
    """The last write is the target, something else, unsettled, or nothing."""
    beliefs = BeliefRecord(b"salt")
    slot = pin_at(1)
    assert written(beliefs, slot, WANT) is Written.NOTHING
    beliefs.invalidate(slot, at=1.0)
    assert written(beliefs, slot, WANT) is Written.UNSETTLED
    beliefs.record_pin(slot, "1234", Strength.APPLIED, at=2.0)
    assert written(beliefs, slot, WANT) is Written.TARGET
    assert written(beliefs, slot, WantPin("4321")) is Written.OTHER
    assert written(beliefs, slot, WANT_EMPTY) is Written.OTHER
