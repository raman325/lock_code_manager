"""One table: what a failed write does to its address (decision 1)."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.core.refusals import (
    ErrorKind,
    Outcome,
    Repair,
    Response,
    respond,
)


@pytest.mark.parametrize(
    ("kind", "clearing", "expected"),
    [
        (
            ErrorKind.VALUE_REFUSED,
            False,
            Response(Outcome.DISABLE, Repair.SLOT_DISABLED),
        ),
        (
            ErrorKind.SLOT_REFUSED,
            False,
            Response(Outcome.SUSPEND, Repair.SLOT_SUSPENDED),
        ),
        # A refused clear never disables: that loops (#1633).
        (
            ErrorKind.VALUE_REFUSED,
            True,
            Response(Outcome.SUSPEND, Repair.CODE_MAY_REMAIN),
        ),
        (
            ErrorKind.SLOT_REFUSED,
            True,
            Response(Outcome.SUSPEND, Repair.CODE_MAY_REMAIN),
        ),
        (ErrorKind.TRANSIENT, False, Response(Outcome.SLOT_STRIKE)),
        (ErrorKind.TRANSIENT, True, Response(Outcome.SLOT_STRIKE)),
        (ErrorKind.UNREACHABLE, False, Response(Outcome.LOCK_STRIKE)),
        (ErrorKind.ALREADY_SO, True, Response(Outcome.SUCCESS)),
        (ErrorKind.TURN_TIMEOUT, False, Response(Outcome.NOTHING)),
        (ErrorKind.TURN_TIMEOUT, True, Response(Outcome.NOTHING)),
    ],
)
def test_each_failure_does_what_would_change_its_outcome(
    kind: ErrorKind, clearing: bool, expected: Response
) -> None:
    """Classified by what would change the outcome, not by where it came from."""
    assert respond(kind, clearing=clearing) == expected
