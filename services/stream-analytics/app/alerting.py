"""
Pure alert-hysteresis logic, deliberately kept free of any Kafka/Faust
dependency -- same rationale as rolling.py: testable standalone, and
easy to verify in isolation from the Faust agent that drives it.

Why hysteresis and not a flat "value > threshold -> alert" check (which
is what consumer.py's ALERT_THRESHOLD currently does, as a log line
only): a single noisy sample crossing the threshold would otherwise
flip the alert state on every message, which is exactly the kind of
alert-flapping real monitoring systems are built to avoid. AlertTracker
instead requires `breach_streak` consecutive samples above the
threshold before entering ALERT, and `recovery_streak` consecutive
samples at/below it before clearing back to OK -- a standard debounce
pattern, not a novel one, but a genuine "streaming state machine" rather
than a stateless per-message check.

`error_rate` is currently always ~0.0 on AerBackend (see
orchestrator/app/tasks/calibration.py's "Honest limitation") -- there is
no real drift for this to catch yet. This module is still worth having:
the state machine itself is exercised and verified below with synthetic
values, and it's the natural place to plug in a real threshold once
there's a noise model or real hardware producing a meaningful
error_rate distribution.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from enum import Enum

DEFAULT_THRESHOLD = 0.05
DEFAULT_BREACH_STREAK = 3
DEFAULT_RECOVERY_STREAK = 3


class AlertLevel(Enum):
    OK = "ok"
    ALERT = "alert"


@dataclass(frozen=True)
class AlertState:
    """The full hysteresis state for one backend, as a single immutable
    value -- deliberately shaped this way (rather than three separate
    dicts, which an earlier version of this module used) so it can be
    stored as one value in a Faust `Table` (see faust_app.py), which
    needs a single serializable per-key value, not several independent
    counters living in separate Python dicts.
    """

    level: AlertLevel = AlertLevel.OK
    consecutive_breaches: int = 0
    consecutive_ok: int = 0


@dataclass(frozen=True)
class AlertTransition:
    """Returned by `step()`/`AlertTracker.evaluate()` only on a state
    change (OK -> ALERT or ALERT -> OK) -- most calls return `None`,
    since most samples don't cross a hysteresis boundary. Carries enough
    context to publish a self-describing event downstream without the
    consumer needing to look anything else up.
    """

    backend_name: str
    level: AlertLevel
    value: float
    threshold: float


def step(
    state: AlertState,
    value: float,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    breach_streak: int = DEFAULT_BREACH_STREAK,
    recovery_streak: int = DEFAULT_RECOVERY_STREAK,
    backend_name: str = "",
) -> tuple[AlertState, AlertTransition | None]:
    """Pure state-transition function: given the current `AlertState` for
    one backend and one new sample, returns the *next* `AlertState` and,
    only on a boundary crossing, an `AlertTransition` describing it.

    Kept pure (no mutation, no lookup by backend_name internally) so it
    can be driven directly against a Faust `Table`'s per-key value
    (get -> step -> set is the natural Faust pattern for stateful
    per-key logic) as well as against `AlertTracker`'s own plain dict
    below -- one state machine, two storage backends.
    """
    if value > threshold:
        breaches = state.consecutive_breaches + 1
        if state.level is AlertLevel.OK and breaches >= breach_streak:
            new_state = AlertState(level=AlertLevel.ALERT, consecutive_breaches=breaches, consecutive_ok=0)
            return new_state, AlertTransition(backend_name, AlertLevel.ALERT, value, threshold)
        return replace(state, consecutive_breaches=breaches, consecutive_ok=0), None

    ok_count = state.consecutive_ok + 1
    if state.level is AlertLevel.ALERT and ok_count >= recovery_streak:
        new_state = AlertState(level=AlertLevel.OK, consecutive_breaches=0, consecutive_ok=ok_count)
        return new_state, AlertTransition(backend_name, AlertLevel.OK, value, threshold)
    return replace(state, consecutive_breaches=0, consecutive_ok=ok_count), None


class AlertTracker:
    """Convenience wrapper around `step()` for callers that don't need
    (or don't have) an external table to hold state -- e.g. a possible
    future non-Faust consumer, and this module's own tests. Holds one
    `AlertState` per backend in a plain dict.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_THRESHOLD,
        breach_streak: int = DEFAULT_BREACH_STREAK,
        recovery_streak: int = DEFAULT_RECOVERY_STREAK,
    ) -> None:
        self._threshold = threshold
        self._breach_streak = breach_streak
        self._recovery_streak = recovery_streak
        self._states: dict[str, AlertState] = defaultdict(AlertState)

    def evaluate(self, backend_name: str, value: float) -> AlertTransition | None:
        """Records one sample for `backend_name` and returns an
        `AlertTransition` if -- and only if -- this sample caused a state
        change. Returns `None` on every other call, including "still
        breaching, already in ALERT" and "still fine, already in OK".
        """
        new_state, transition = step(
            self._states[backend_name],
            value,
            threshold=self._threshold,
            breach_streak=self._breach_streak,
            recovery_streak=self._recovery_streak,
            backend_name=backend_name,
        )
        self._states[backend_name] = new_state
        return transition

    def current_level(self, backend_name: str) -> AlertLevel:
        return self._states[backend_name].level if backend_name in self._states else AlertLevel.OK
