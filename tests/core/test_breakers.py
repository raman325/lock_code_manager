"""When to stop trying: a slot's breaker, and a lock's link."""

from __future__ import annotations

from custom_components.lock_code_manager.core.breakers import (
    LinkState,
    Operation,
    OutcomeKind,
    SlotBreaker,
)


def test_three_failures_inside_the_window_trip_and_stay_tripped() -> None:
    """Once tripped, time passing does not untrip; only a reset does (decision 13)."""
    breaker = SlotBreaker(window=300.0, limit=3)
    for at in (0.0, 100.0, 200.0):
        breaker.charge(at)

    assert breaker.tripped
    breaker.charge(10_000.0)
    assert breaker.tripped
    breaker.reset()
    assert not breaker.tripped


def test_failures_age_out_before_the_breaker_trips() -> None:
    """Failures spread wider than the window never add up to a trip."""
    breaker = SlotBreaker(window=300.0, limit=3)
    for at in (0.0, 200.0, 400.0, 600.0):
        breaker.charge(at)

    assert not breaker.tripped


def test_reads_answering_do_not_hide_writes_that_never_arrive() -> None:
    """A lock whose writes all disconnect is down for writes (#1644)."""
    link = LinkState(trip_after=3)
    for at in range(6):
        link.outcome(Operation.WRITE, OutcomeKind.DISCONNECTED, float(at))
        link.outcome(Operation.READ, OutcomeKind.OK, float(at))

    assert not link.reachable
    assert link.down_for == {Operation.WRITE}


def test_an_answer_of_any_kind_ends_its_run() -> None:
    """A failed write still reached the lock: the write run starts over."""
    link = LinkState(trip_after=3)
    for at in range(2):
        link.outcome(Operation.WRITE, OutcomeKind.DISCONNECTED, float(at))
    link.outcome(Operation.WRITE, OutcomeKind.FAILED, 2.0)
    link.outcome(Operation.WRITE, OutcomeKind.DISCONNECTED, 3.0)

    assert link.reachable


def test_lcm_s_own_queue_running_out_charges_nothing() -> None:
    """A turn timeout never reached the lock, and says nothing about it (#1535)."""
    link = LinkState(trip_after=1)
    link.outcome(Operation.READ, OutcomeKind.TURN_TIMEOUT, 0.0)

    assert link.reachable
    assert not link.reached


def test_backoff_doubles_up_to_its_ceiling() -> None:
    """Each failure past the trip doubles the wait, never beyond the maximum."""
    link = LinkState(trip_after=3, backoff_min=60.0, backoff_max=200.0)
    delays = []
    for at in range(6):
        link.outcome(Operation.READ, OutcomeKind.DISCONNECTED, float(at))
        delays.append(link.next_attempt_in(float(at)))

    assert delays == [0.0, 0.0, 60.0, 120.0, 200.0, 200.0]


def test_only_a_lock_reached_once_is_reported_offline() -> None:
    """A lock still coming up at startup is not offline yet (#1257)."""
    never = LinkState(trip_after=1, alert_after=2)
    once = LinkState(trip_after=1, alert_after=2)
    once.outcome(Operation.READ, OutcomeKind.OK, 0.0)
    for link in (never, once):
        for at in range(1, 4):
            link.outcome(Operation.READ, OutcomeKind.DISCONNECTED, float(at))

    assert not never.offline_alert
    assert once.offline_alert
