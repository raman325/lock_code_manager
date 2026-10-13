"""
When to stop trying: per address (``SlotBreaker``) and per lock (``LinkState``).

Both are plain state machines over the outcomes they are told about, and take
the time as an argument.
"""

from __future__ import annotations

from collections import deque
from enum import StrEnum


class SlotBreaker:
    """
    Stop rewriting an address that keeps failing to take its target.

    ``limit`` charges inside ``window`` seconds trip it, and once tripped it
    stays tripped until the target changes or the slot is disabled, suspended
    or resumed (credential-model decision 13). Agreement never resets it:
    otherwise a keypad fighting a code back could never trip it (#776). Before
    it trips, old charges age out of the window.
    """

    def __init__(self, window: float = 300.0, limit: int = 3) -> None:
        """Start untripped."""
        self._window = window
        self._limit = limit
        self._charges: deque[float] = deque()
        self._tripped = False

    def charge(self, now: float) -> None:
        """Count a failure of the current target at ``now``."""
        if self._tripped:
            return
        self._charges.append(now)
        while self._charges and self._charges[0] <= now - self._window:
            self._charges.popleft()
        if len(self._charges) >= self._limit:
            self._tripped = True

    @property
    def tripped(self) -> bool:
        """Return whether the address is to be left alone until reset."""
        return self._tripped

    def reset(self) -> None:
        """Forget every charge: the target changed, or the slot was re-enabled."""
        self._charges.clear()
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
    # Nothing came back: no connection, or no answer in time.
    DISCONNECTED = "disconnected"
    # Lock Code Manager's own queue for the turn ran out; nothing was sent.
    TURN_TIMEOUT = "turn_timeout"


class LinkState:
    """
    Whether a lock can be reached, and how long to wait before trying again.

    Each kind of operation keeps its own run of consecutive disconnects, and
    an answer of any kind ends only that kind's run. A lock that answers
    every read while every write disconnects is down for writes (#1644): a
    read succeeding says nothing about whether writes get through. The lock is
    down while any kind's run is ``trip_after`` or longer, and is retried on
    a backoff that doubles from ``backoff_min`` to ``backoff_max``.

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
        self._last_failure: float | None = None
        self._reached = False

    def outcome(self, operation: Operation, kind: OutcomeKind, now: float) -> None:
        """Record how one exchange of ``operation`` ended at ``now``."""
        if kind is OutcomeKind.TURN_TIMEOUT:
            return
        if kind is OutcomeKind.DISCONNECTED:
            self._runs[operation] += 1
            self._last_failure = now
            return
        self._reached = True
        self._runs[operation] = 0

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

    def next_attempt_in(self, now: float) -> float:
        """Return how long to wait before trying a down lock again (0 if up)."""
        if self.reachable or self._last_failure is None:
            return 0.0
        over = max(self._runs.values()) - self._trip_after
        delay = min(self._backoff_min * 2**over, self._backoff_max)
        return max(0.0, self._last_failure + delay - now)

    @property
    def offline_alert(self) -> bool:
        """Return whether to tell the user the lock is offline."""
        return self._reached and max(self._runs.values()) >= self._alert_after
