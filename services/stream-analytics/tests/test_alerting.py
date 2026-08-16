"""
Unit tests for `app.alerting.AlertTracker` -- pure logic, no Kafka/Faust
involved. Same approach as test_rolling.py: these 7 scenarios were first
run as a standalone script (see docs/architecture/kafka.md) to confirm
the hysteresis state machine before being transcribed into this form.
"""

from __future__ import annotations

from app.alerting import AlertLevel, AlertTracker


def test_single_breach_does_not_trigger() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=3, recovery_streak=3)

    assert tracker.evaluate("aer-simulator", 0.10) is None
    assert tracker.current_level("aer-simulator") == AlertLevel.OK


def test_streak_of_breaches_triggers_alert_on_last_one() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=3, recovery_streak=3)

    assert tracker.evaluate("aer-simulator", 0.10) is None
    assert tracker.evaluate("aer-simulator", 0.10) is None
    transition = tracker.evaluate("aer-simulator", 0.10)

    assert transition is not None
    assert transition.level == AlertLevel.ALERT
    assert transition.backend_name == "aer-simulator"
    assert tracker.current_level("aer-simulator") == AlertLevel.ALERT


def test_continued_breaches_do_not_repeat_the_transition() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=1, recovery_streak=3)
    tracker.evaluate("aer-simulator", 0.10)  # triggers ALERT
    assert tracker.current_level("aer-simulator") == AlertLevel.ALERT

    assert tracker.evaluate("aer-simulator", 0.20) is None
    assert tracker.evaluate("aer-simulator", 0.30) is None
    assert tracker.current_level("aer-simulator") == AlertLevel.ALERT


def test_single_recovery_sample_does_not_clear_alert() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=1, recovery_streak=3)
    tracker.evaluate("aer-simulator", 0.10)  # triggers ALERT

    assert tracker.evaluate("aer-simulator", 0.01) is None
    assert tracker.current_level("aer-simulator") == AlertLevel.ALERT


def test_breach_during_recovery_resets_the_recovery_streak() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=1, recovery_streak=3)
    tracker.evaluate("aer-simulator", 0.10)  # triggers ALERT

    assert tracker.evaluate("aer-simulator", 0.01) is None  # ok streak 1
    assert tracker.evaluate("aer-simulator", 0.10) is None  # breach resets ok streak to 0
    assert tracker.current_level("aer-simulator") == AlertLevel.ALERT

    assert tracker.evaluate("aer-simulator", 0.01) is None  # ok streak 1
    assert tracker.evaluate("aer-simulator", 0.01) is None  # ok streak 2
    transition = tracker.evaluate("aer-simulator", 0.01)  # ok streak 3 -> clears

    assert transition is not None
    assert transition.level == AlertLevel.OK
    assert tracker.current_level("aer-simulator") == AlertLevel.OK


def test_backends_tracked_independently() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=2, recovery_streak=2)
    tracker.evaluate("backend-a", 0.10)
    transition = tracker.evaluate("backend-a", 0.10)

    assert transition is not None and transition.level == AlertLevel.ALERT
    assert tracker.current_level("backend-b") == AlertLevel.OK


def test_exactly_at_threshold_is_not_a_breach() -> None:
    tracker = AlertTracker(threshold=0.05, breach_streak=1, recovery_streak=1)

    assert tracker.evaluate("aer-simulator", 0.05) is None
    assert tracker.current_level("aer-simulator") == AlertLevel.OK

    transition = tracker.evaluate("aer-simulator", 0.0500001)
    assert transition is not None and transition.level == AlertLevel.ALERT
