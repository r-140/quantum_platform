"""
Pure rolling-average logic, deliberately kept free of any Kafka
dependency so it's testable standalone (see tests/test_rolling_error_rate.py)
and so the aggregation logic itself is easy to verify in isolation from the
consumer loop that feeds it.
"""

from __future__ import annotations

from collections import defaultdict, deque

DEFAULT_WINDOW_SIZE = 10


class RollingErrorRate:
    """Maintains the last `window_size` error_rate samples per
    `backend_name` and exposes their rolling average.

    A plain in-memory `deque` per backend, not Kafka Streams' RocksDB-backed
    windowed state store or Faust's table abstraction -- justified here
    because this project's actual event volume is tiny (one calibration
    cycle per orchestrator instance every few minutes; see
    orchestrator/app/tasks/calibration.py). A hand-rolled window is enough
    to express "rolling average over the last N samples" at this scale.
    Revisit if there are ever multiple producers, materially higher volume,
    or a need for windowing state that survives a process restart -- that's
    precisely the case those heavier frameworks exist for.
    """

    def __init__(self, window_size: int = DEFAULT_WINDOW_SIZE) -> None:
        self._window_size = window_size
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window_size))

    def add_sample(self, backend_name: str, error_rate: float) -> float:
        """Records one sample and returns the updated rolling average for
        that backend. Once the window is full, the oldest sample is
        dropped automatically (`deque(maxlen=...)`).
        """
        window = self._samples[backend_name]
        window.append(error_rate)
        return sum(window) / len(window)

    def sample_count(self, backend_name: str) -> int:
        return len(self._samples[backend_name])
