"""Tests for the CircuitBreaker resilience primitive."""

from datetime import timedelta

from custom_components.lock_code_manager.domain.resilience import CircuitBreaker


class TestConsecutiveBackoffPolicy:
    """Lock-level config: threshold + exponential backoff, no window."""

    def _breaker(self) -> CircuitBreaker:
        return CircuitBreaker(
            3,
            backoff_initial=timedelta(seconds=60),
            backoff_max=timedelta(seconds=1800),
        )

    def test_not_tripped_below_threshold(self) -> None:
        breaker = self._breaker()
        for _ in range(2):
            breaker.record_failure()
        assert breaker.failure_count == 2
        assert not breaker.tripped
        assert breaker.backoff_delay == timedelta(0)

    def test_tripped_at_threshold_uses_initial_delay(self) -> None:
        breaker = self._breaker()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.tripped
        # 60 * 2 ** (3 - 3) == 60
        assert breaker.backoff_delay == timedelta(seconds=60)

    def test_backoff_escalates_exponentially(self) -> None:
        breaker = self._breaker()
        for _ in range(6):  # threshold + 3
            breaker.record_failure()
        # 60 * 2 ** (6 - 3) == 480
        assert breaker.backoff_delay == timedelta(seconds=480)

    def test_backoff_caps_at_max(self) -> None:
        breaker = self._breaker()
        for _ in range(30):
            breaker.record_failure()
        assert breaker.backoff_delay == timedelta(seconds=1800)

    def test_reset_clears_state(self) -> None:
        breaker = self._breaker()
        for _ in range(5):
            breaker.record_failure()
        breaker.reset()
        assert breaker.failure_count == 0
        assert not breaker.tripped
        assert breaker.backoff_delay == timedelta(0)


class TestWindowedTripPolicy:
    """Slot-level config: threshold within a sliding window, no backoff."""

    def _breaker(self) -> CircuitBreaker:
        return CircuitBreaker(3, window=timedelta(minutes=5))

    def test_trips_at_threshold_within_window(self) -> None:
        breaker = self._breaker()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.tripped

    def test_backoff_delay_unused_is_zero(self) -> None:
        breaker = self._breaker()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.backoff_delay == timedelta(0)

    def test_window_expiry_resets_count(self, freezer) -> None:
        breaker = self._breaker()
        breaker.record_failure()
        breaker.record_failure()
        freezer.tick(timedelta(minutes=5, seconds=1))
        # First failure is now outside the window: count restarts at 1.
        breaker.record_failure()
        assert breaker.failure_count == 1
        assert not breaker.tripped

    def test_failures_within_window_accumulate(self, freezer) -> None:
        breaker = self._breaker()
        breaker.record_failure()
        freezer.tick(timedelta(minutes=1))
        breaker.record_failure()
        freezer.tick(timedelta(minutes=1))
        breaker.record_failure()
        assert breaker.tripped

    def test_tripped_clears_when_window_elapses(self, freezer) -> None:
        breaker = self._breaker()
        for _ in range(3):
            breaker.record_failure()
        assert breaker.tripped
        # Once the window passes with no new failures, the stale breaches no
        # longer count as tripped.
        freezer.tick(timedelta(minutes=5, seconds=1))
        assert not breaker.tripped
        # reset() also clears the tripped state immediately.
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.tripped
        breaker.reset()
        assert not breaker.tripped
