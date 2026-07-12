"""
Shared pytest fixtures for quantum_core's unit tests.
"""

from __future__ import annotations

import pytest

import quantum_core.sync.polling as polling_module


class FakeClock:
    """A controllable stand-in for `time.monotonic()`. Tests advance it
    explicitly (via the `fake_sleep` this fixture also installs) instead of
    waiting on the wall clock -- this is what makes tests covering
    `PollingConfig.timeout_s` or exponential backoff run in milliseconds
    instead of tens of seconds.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def time(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Patches `time.monotonic` and `asyncio.sleep` *as seen from inside
    quantum_core.sync.polling* so that:
    - `time.monotonic()` returns a controllable fake time;
    - `asyncio.sleep(s)` advances that fake time by `s` instead of actually
      waiting.

    Patched on the `time`/`asyncio` module objects (not via `from time
    import monotonic`-style rebinding) because `polling.py` does `import
    time` / `import asyncio` and calls `time.monotonic()` /
    `asyncio.sleep()` -- patching the attribute on the shared module object
    is what makes the patch visible there.
    """
    clock = FakeClock()

    monkeypatch.setattr(polling_module.time, "monotonic", clock.time)

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    monkeypatch.setattr(polling_module.asyncio, "sleep", fake_sleep)

    return clock