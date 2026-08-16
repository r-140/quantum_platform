"""
Statistical drift detection: maintains a running (all-time) mean/stddev
of `error_rate` per backend via Welford's online algorithm, and exposes
how many standard deviations the *current* windowed average
(faust_app.py's `window_avg`, a short-term 60s signal) sits from that
long-term baseline -- a z-score.

Why this on top of the flat-threshold alerting in alerting.py: a static
`error_rate > 0.05` check has no idea what "normal" looks like for a
given backend -- it treats 0.05 as anomalous everywhere, forever. A
z-score instead asks "is this backend behaving differently from its own
history", which is the actual definition of *drift*, and the reason
this module exists (see calibration.py's "Honest limitation" -- this is
built specifically so there's a mechanism ready the moment a noise
model or real hardware gives error_rate a distribution worth measuring
drift against; today, on noiseless AerBackend, every z-score will
correctly come out as ~0/undefined, which is the expected behavior of
this algorithm given constant input, not a sign of a bug in it).

Welford's algorithm (Welford 1962, popularized via Knuth TAOCP vol 2) is
used instead of the naive "keep a list, call statistics.stdev()"
approach because it's O(1) per sample in both time and memory --
important here since the baseline is meant to accumulate over the
backend's *entire* history, not a bounded recent window (unlike
RollingErrorRate/the tumbling Faust tables, which deliberately only keep
recent samples).
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_SAMPLES_FOR_ZSCORE = 2  # sample variance is undefined with fewer than 2 points


@dataclass(frozen=True)
class WelfordStats:
    """Running (count, mean, M2) triple -- M2 is the running sum of
    squared differences from the mean, Welford's algorithm's namesake
    intermediate quantity. Immutable, mirroring AlertState in
    alerting.py, so it can be stored as one value in a Faust `Table`.
    """

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    @property
    def variance(self) -> float:
        """Sample variance (ddof=1, matching `statistics.variance`), not
        population variance -- consistent with treating each backend's
        history as a sample of its true underlying error-rate
        distribution, not the full population of it.
        """
        if self.count < MIN_SAMPLES_FOR_ZSCORE:
            return 0.0
        return self.m2 / (self.count - 1)

    @property
    def stddev(self) -> float:
        return self.variance**0.5


def update(stats: WelfordStats, value: float) -> WelfordStats:
    """One step of Welford's online algorithm: folds `value` into
    `stats` and returns the new running statistics. Pure -- no mutation
    -- for the same reason app.alerting.step() is pure: this needs to
    drive a Faust `Table`'s get -> update -> set cycle directly.
    """
    count = stats.count + 1
    delta = value - stats.mean
    mean = stats.mean + delta / count
    delta2 = value - mean
    m2 = stats.m2 + delta * delta2
    return WelfordStats(count=count, mean=mean, m2=m2)


def zscore(stats: WelfordStats, value: float) -> float | None:
    """How many standard deviations `value` sits from `stats`'s running
    mean. Returns `None` (not 0.0 -- an explicit "not yet meaningful",
    distinct from "exactly on the mean") when there aren't enough
    samples yet, or when the baseline has zero variance (e.g. every
    sample so far has been identical -- exactly what AerBackend's
    noiseless error_rate looks like today) and a z-score is therefore
    undefined rather than merely large.
    """
    if stats.count < MIN_SAMPLES_FOR_ZSCORE or stats.stddev == 0.0:
        return None
    return (value - stats.mean) / stats.stddev
