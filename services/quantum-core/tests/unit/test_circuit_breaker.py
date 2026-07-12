"""
Unit tests for `quantum_core.sync.polling.CircuitBreaker`.

Scenarios here were first exercised as a standalone asyncio script (no
pytest) to confirm the fake-clock approach and the breaker's behavior were
both correct before being transcribed into this form -- see
docs/testing.md for why that extra step mattered given this environment
couldn't install pytest to run these directly.
"""

from __future__ import annotations

from quantum_core.sync.polling import CircuitBreaker


def test_starts_closed() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_after_s=10.0)
    assert not breaker.is_open


def test_opens_at_failure_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_after_s=10.0)
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.is_open, "should not open before reaching the threshold"

    breaker.record_failure()
    assert breaker.is_open, "should open exactly at the threshold"


def test_success_resets_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=3, reset_after_s=10.0)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert not breaker.is_open, "success should have reset the streak, not just paused it"


def test_half_open_after_reset_period(fake_clock) -> None:
    breaker = CircuitBreaker(failure_threshold=2, reset_after_s=5.0)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open

    fake_clock.advance(4.9)
    assert breaker.is_open, "should still be open just before reset_after_s elapses"

    fake_clock.advance(0.2)  # total 5.1s, past the threshold
    assert not breaker.is_open, "should allow a trial call through (half-open) after reset_after_s"


def test_half_open_trial_failure_reopens_immediately(fake_clock) -> None:
    """After the half-open trial is allowed through, a single further
    failure should be enough to trip it open again -- the breaker
    shouldn't require a fresh full `failure_threshold` streak.
    """
    breaker = CircuitBreaker(failure_threshold=2, reset_after_s=5.0)
    breaker.record_failure()
    breaker.record_failure()
    fake_clock.advance(5.1)
    assert not breaker.is_open  # half-open now

    breaker.record_failure()  # the trial call fails
    assert breaker.is_open, "a failed trial call should reopen the breaker"