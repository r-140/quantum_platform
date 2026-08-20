"""Pure policy for deciding whether an expensive experiment may execute."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class CalibrationDecision(str, Enum):
    ALLOW = "allow"
    WAIT = "wait_for_calibration"
    REJECT = "reject"


@dataclass(frozen=True)
class CalibrationObservation:
    backend_name: str
    observed_at: datetime
    error_rate: float
    shots: int


@dataclass(frozen=True)
class CalibrationPolicy:
    freshness: timedelta = timedelta(minutes=10)
    reject_error_rate: float = 0.10

    def evaluate(
        self,
        observation: CalibrationObservation | None,
        *,
        now: datetime | None = None,
    ) -> CalibrationDecision:
        if observation is None:
            return CalibrationDecision.WAIT
        current_time = now or datetime.now(timezone.utc)
        observed_at = observation.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        if current_time - observed_at > self.freshness:
            return CalibrationDecision.WAIT
        if observation.error_rate >= self.reject_error_rate:
            return CalibrationDecision.REJECT
        return CalibrationDecision.ALLOW
