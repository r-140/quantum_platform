from datetime import datetime, timedelta, timezone

from app.calibration_policy import (
    CalibrationDecision,
    CalibrationObservation,
    CalibrationPolicy,
)

NOW = datetime(2026, 8, 19, 20, 0, tzinfo=timezone.utc)


def observation(*, age_s: float = 0, error_rate: float = 0.0):
    return CalibrationObservation(
        backend_name="aer-simulator",
        observed_at=NOW - timedelta(seconds=age_s),
        error_rate=error_rate,
        shots=1024,
    )


def test_missing_observation_waits() -> None:
    assert CalibrationPolicy().evaluate(None, now=NOW) is CalibrationDecision.WAIT


def test_stale_observation_waits() -> None:
    policy = CalibrationPolicy(freshness=timedelta(seconds=60))
    assert policy.evaluate(observation(age_s=61), now=NOW) is CalibrationDecision.WAIT


def test_fresh_healthy_observation_allows() -> None:
    policy = CalibrationPolicy(freshness=timedelta(seconds=60))
    assert policy.evaluate(observation(age_s=60, error_rate=0.01), now=NOW) is CalibrationDecision.ALLOW


def test_fresh_high_error_observation_rejects() -> None:
    policy = CalibrationPolicy(reject_error_rate=0.10)
    assert policy.evaluate(observation(error_rate=0.10), now=NOW) is CalibrationDecision.REJECT
