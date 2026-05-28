"""Reusable circuit breaker primitive for resilience tracking."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util


class CircuitBreaker:
    """
    Track failures and decide when to stop or back off.

    A single primitive covers two policies, chosen by the constructor
    arguments:

    Consecutive backoff (set ``backoff_initial`` / ``backoff_max``, leave
    ``window`` unset): counts consecutive failures and escalates
    ``backoff_delay`` exponentially once ``tripped``. Used at the lock level
    for connectivity.

    Windowed trip (set ``window``, leave the backoff arguments unset): counts
    failures within a sliding window. ``tripped`` latches once the threshold is
    reached and stays latched until ``reset`` is called, so a caller decides
    when recovery happens. Used at the slot level for a code that never
    converges.
    """

    def __init__(
        self,
        threshold: int,
        *,
        window: timedelta | None = None,
        backoff_initial: timedelta | None = None,
        backoff_max: timedelta | None = None,
    ) -> None:
        """Initialize the circuit breaker."""
        self._threshold = threshold
        self._window = window
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._failure_count = 0
        self._first_failure: datetime | None = None

    def record_failure(self) -> None:
        """Record a failure, restarting the count if the window has elapsed."""
        now = dt_util.utcnow()
        if (
            self._window is not None
            and self._first_failure is not None
            and now - self._first_failure > self._window
        ):
            self._failure_count = 0
            self._first_failure = None
        if self._first_failure is None:
            self._first_failure = now
        self._failure_count += 1

    def record_success(self) -> None:
        """Record a success, clearing all failure state."""
        self.reset()

    def reset(self) -> None:
        """Clear all failure state."""
        self._failure_count = 0
        self._first_failure = None

    @property
    def failure_count(self) -> int:
        """Return the current failure count."""
        return self._failure_count

    @property
    def tripped(self) -> bool:
        """
        Return whether the failure threshold has been reached.

        For a windowed breaker the breaches must fall within the trailing
        window; once the window elapses with no new failures the stale
        breaches no longer count as tripped.
        """
        if self._failure_count < self._threshold:
            return False
        if self._window is not None and self._first_failure is not None:
            return dt_util.utcnow() - self._first_failure <= self._window
        return True

    @property
    def backoff_delay(self) -> timedelta:
        """Return the current backoff delay, or zero when not tripped."""
        if not self.tripped or self._backoff_initial is None:
            return timedelta(0)
        delay = self._backoff_initial * 2 ** (self._failure_count - self._threshold)
        if self._backoff_max is not None and delay > self._backoff_max:
            return self._backoff_max
        return delay
