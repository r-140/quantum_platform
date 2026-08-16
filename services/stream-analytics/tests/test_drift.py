"""
Unit tests for `app.drift` -- pure logic (Welford's online mean/variance
+ z-score), no Kafka/Faust involved. Same approach as test_rolling.py
and test_alerting.py: these 6 scenarios were first run as a standalone
script (see docs/architecture/kafka.md) -- including a direct check
against `statistics.mean`/`statistics.stdev` on a random sample, which
matched to 1e-9 -- before being transcribed into this form.
"""

from __future__ import annotations

import random
import statistics

from app.drift import WelfordStats, update, zscore


def test_matches_statistics_module_over_random_sample() -> None:
    rng = random.Random(7)
    samples = [rng.gauss(0.03, 0.015) for _ in range(80)]

    stats = WelfordStats()
    for x in samples:
        stats = update(stats, x)

    assert abs(stats.mean - statistics.mean(samples)) < 1e-9
    assert abs(stats.stddev - statistics.stdev(samples)) < 1e-9


def test_zscore_undefined_with_too_few_samples() -> None:
    stats = update(WelfordStats(), 0.05)

    assert stats.count == 1
    assert zscore(stats, 0.10) is None


def test_zscore_undefined_when_baseline_has_zero_variance() -> None:
    stats = WelfordStats()
    for _ in range(10):
        stats = update(stats, 0.0)

    assert stats.mean == 0.0
    assert stats.stddev == 0.0
    # None, not infinite -- zero variance means "not meaningful", per the
    # docstring in drift.py, not "any deviation is infinitely anomalous".
    assert zscore(stats, 0.0) is None
    assert zscore(stats, 0.5) is None


def test_outlier_gets_large_zscore_against_tight_baseline() -> None:
    stats = WelfordStats()
    for x in [0.02, 0.021, 0.019, 0.020, 0.0205, 0.0195, 0.02, 0.0203]:
        stats = update(stats, x)

    z = zscore(stats, 0.10)

    assert z is not None
    assert z > 5


def test_near_mean_value_gets_small_zscore() -> None:
    stats = WelfordStats()
    for x in [0.02, 0.021, 0.019, 0.020, 0.0205, 0.0195, 0.02, 0.0203]:
        stats = update(stats, x)

    z = zscore(stats, stats.mean + 0.0001)

    assert z is not None
    assert abs(z) < 1.0


def test_update_does_not_mutate_its_input() -> None:
    before = WelfordStats(count=3, mean=0.01, m2=0.0002)

    after = update(before, 0.5)

    assert before.count == 3
    assert before.mean == 0.01
    assert after.count == 4
