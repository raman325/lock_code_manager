"""
When to stop trying: per address (``SlotBreaker``) and per lock (``LinkState``).

Both are plain state machines over the outcomes they are told about, and take
the time as an argument.
"""

from __future__ import annotations

from bisect import insort
from enum import StrEnum


class SlotBreaker:
    """
    Stop rewriting an address that keeps failing to take its target.

    ``limit`` charges inside ``window`` seconds trip it, and once tripped it
    stays tripped until the target changes or the slot is disabled, suspended
    or resumed (credential-model decision 13). Agreement never resets it:
    otherwise a keypad fighting a code back could never trip it (#776). Before
    it trips, old charges age out of the window.

    Each write is charged at most once until the breaker is reset: the caller
    passes the write the charge is for, so judging an address again and
    again never counts one failed write twice, and a reset (a new target)
    lets the same write count again.
    """

    def __init__(self, window: float = 300.0, limit: int = 3) -> None:
        """Start untripped."""
        self._window = window
        self._limit = limit
        self._charges: list[float] = []
        self._tripped = False
        # The writes charged since the last reset, held so their identity
        # cannot be reused; few, since the breaker trips after ``limit``.
        self._charged: list[object] = []

    def charge(self, at: float, write: object | None = None) -> bool:
        """
        Count a failure of the current target that happened at ``at``.

        ``write`` is the write that failed; one already charged since the
        last reset counts no more, and False is returned. Charges may arrive
        out of order (a late result handled after a later one): the window is
        measured back from the latest charge.
        """
        if self._tripped:
            return False
        if write is not None:
            if any(charged is write for charged in self._charged):
                return False
            self._charged.append(write)
        insort(self._charges, at)
        latest = self._charges[-1]
        self._charges = [t for t in self._charges if t > latest - self._window]
        if len(self._charges) >= self._limit:
            self._tripped = True
        return True

    @property
    def tripped(self) -> bool:
        """Return whether the address is to be left alone until reset."""
        return self._tripped

    def reset(self) -> None:
        """Forget every charge: the target changed, or the slot was re-enabled."""
        self._charges.clear()
        self._charged.clear()
        self._tripped = False


class Operation(StrEnum):
    """What kind of exchange an outcome came from."""

    READ = "read"
    WRITE = "write"
    PROBE = "probe"


class OutcomeKind(StrEnum):
    """How an exchange with the lock ended."""

    # The lock answered as asked.
    OK = "ok"
    # The lock answered, with a failure: it is there, whatever it said.
    FAILED = "failed"
    # The lock did not answer: no connection, no answer in time, or a service
    # in front of it answering in its place.
    DISCONNECTED = "disconnected"
    # Lock Code Manager's own queue for the turn ran out; nothing was sent.
    TURN_TIMEOUT = "turn_timeout"


class LinkState:
    """
    Whether a lock can be reached, and how long to wait before trying again.

    Each kind of operation keeps its own run of consecutive disconnects, and
    an answer of any kind ends only that kind's run. A lock that answers
    every read while every write disconnects is down for writes (#1644): a
    read succeeding says nothing about whether writes get through. Any answer
    ends the read and probe runs, since it shows the lock can be read; only a
    write getting through ends the write run. Each kind whose
    run is ``trip_after`` or longer is down, and is retried on its own
    backoff, doubling from ``backoff_min`` to ``backoff_max``.

    The offline alert is for a lock that was reached and then lost: one that
    has not answered since startup is still coming up (#1257).
    """

    def __init__(
        self,
        trip_after: int = 3,
        backoff_min: float = 60.0,
        backoff_max: float = 1800.0,
        alert_after: int = 12,
    ) -> None:
        """Start reachable, never yet reached."""
        self._trip_after = trip_after
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._alert_after = alert_after
        self._runs: dict[Operation, int] = dict.fromkeys(Operation, 0)
        self._last_failure: dict[Operation, float] = {}
        self._reached = False

    def outcome(self, operation: Operation, kind: OutcomeKind, now: float) -> None:
        """Record how one exchange of ``operation`` ended at ``now``."""
        if kind is OutcomeKind.TURN_TIMEOUT:
            return
        if kind is OutcomeKind.DISCONNECTED:
            self._runs[operation] += 1
            self._last_failure[operation] = now
            return
        self._reached = True
        # Any answer shows the lock can be read; only a write getting through
        # shows that writes do.
        self._runs[Operation.READ] = 0
        self._runs[Operation.PROBE] = 0
        if operation is Operation.WRITE:
            self._runs[Operation.WRITE] = 0

    @property
    def reached(self) -> bool:
        """Return whether the lock has answered anything since startup."""
        return self._reached

    @property
    def down_for(self) -> frozenset[Operation]:
        """Return the kinds of operation the lock is down for."""
        return frozenset(
            op for op, run in self._runs.items() if run >= self._trip_after
        )

    @property
    def reachable(self) -> bool:
        """Return whether no kind of operation is down."""
        return not self.down_for

    def next_attempt_in(self, operation: Operation, now: float) -> float:
        """Return how long to wait before trying ``operation`` again (0 if not down)."""
        run = self._runs[operation]
        if run < self._trip_after:
            return 0.0
        # Capped before raising to a power: a dead lock retried for weeks would
        # otherwise overflow a float.
        doublings = min(run - self._trip_after, 32)
        delay = min(self._backoff_min * 2**doublings, self._backoff_max)
        return max(0.0, self._last_failure[operation] + delay - now)

    @property
    def offline_alert(self) -> bool:
        """
        Return whether to tell the user the lock is offline.

        Only when nothing answers: a lock answering reads while its writes
        disconnect is down for writes, not offline.
        """
        unanswered = max(self._runs[Operation.READ], self._runs[Operation.PROBE])
        return self._reached and unanswered >= self._alert_after
