"""
Unit tests for `app.rolling.RollingErrorRate` -- pure logic, no Kafka
involved. These scenarios were first run as a standalone script (see
docs/architecture/kafka.md) to confirm the window/eviction/averaging math
before being transcribed into this form.
"""

from __future__ import annotations

from app.rolling import RollingErrorRate


def test_single_sample_average_equals_itself() -> None:
    rolling = RollingErrorRate(window_size=10)

    avg = rolling.add_sample("aer-simulator", 0.0)

    assert avg == 0.0
    assert rolling.sample_count("aer-simulator") == 1


def test_average_over_growing_window() -> None:
    rolling = RollingErrorRate(window_size=10)

    avgs = [rolling.add_sample("aer-simulator", v) for v in [0.0, 0.02, 0.10]]

    assert avgs == [0.0, 0.01, (0.0 + 0.02 + 0.10) / 3]


def test_oldest_sample_evicted_once_window_full() -> None:
    rolling = RollingErrorRate(window_size=3)

    for v in [0.0, 0.02, 0.10]:
        rolling.add_sample("aer-simulator", v)
    # window is now full at [0.0, 0.02, 0.10]; adding a 4th sample should
    # evict the oldest (0.0), not just grow the window indefinitely.
    avg = rolling.add_sample("aer-simulator", 0.20)

    assert avg == (0.02 + 0.10 + 0.20) / 3
    assert rolling.sample_count("aer-simulator") == 3


def test_backends_tracked_independently() -> None:
    rolling = RollingErrorRate(window_size=5)

    rolling.add_sample("backend-a", 0.5)
    rolling.add_sample("backend-b", 0.1)

    assert rolling.sample_count("backend-a") == 1
    assert rolling.sample_count("backend-b") == 1


def test_unknown_backend_has_zero_samples() -> None:
    rolling = RollingErrorRate()

    assert rolling.sample_count("never-seen") == 0
