# Testing

## Unit tests for `polling.py`

`services/quantum-core/tests/unit/`:
- `fakes.py` — `ScriptedBackend`, a test double for `QuantumBackend` that
  plays back a scripted sequence of statuses and delegates
  `fetch_result()` to a custom function (to simulate transient errors,
  hard failures, stuck jobs, etc.);
- `conftest.py` — the `fake_clock` fixture, which patches
  `time.monotonic()` and `asyncio.sleep()` inside `polling.py`, so that
  backoff/timeout tests run in milliseconds instead of real
  seconds/tens of seconds;
- `test_circuit_breaker.py` — 5 tests: opening on threshold, counter
  reset on success, time-based half-open transition, re-opening after a
  failed trial attempt;
- `test_wait_for_result.py` — 8 tests: immediate success, growing
  backoff interval, retry on transient error (both successful and
  exhausted), timeout (+ calling `cancel()`), cancellation via
  `CancellationToken`, "hard" failure (returns normally, doesn't raise),
  blocking when the circuit breaker is open (no call to the backend at
  all).

### How these tests were verified without access to pytest

In the working environment where these tests were written, there's no
network access to install `pytest`/`pytest-asyncio`. Since `polling.py`
and `base.py` don't depend on anything beyond the standard library, it
was possible to:

1. First run every scenario as a plain asyncio script (no pytest, no
   mocking framework) — manually implementing the same `ScriptedBackend`
   and `FakeClock` that later became `fakes.py`/`conftest.py`.
2. Once all scenarios passed, port them into pytest-compatible form
   (`test_*.py`, fixtures, `pytest.raises`/`pytest.approx`).
3. **Additionally** — run the final test files themselves (not a draft),
   manually calling the test functions and substituting `fake_clock` in
   place of real pytest injection, with a minimal local stub for
   `pytest.approx`/`pytest.raises` (functionally equivalent, but not
   actual pytest).

This gives high confidence in the **logic** of both the tests and
`polling.py` itself — including subtle cases like "a hard failure should
not raise an exception" and "an open breaker shouldn't even touch the
backend," which are easy to get backwards while writing.

### ⚠️ What's still unverified

The pytest machinery itself — automatic fixture discovery by parameter
name (`fake_clock`), `pytest-asyncio` running in `asyncio_mode = "auto"`
mode (running `async def test_...` with no extra decorators), and the
correctness of the relative import `from .fakes import ...` during
pytest's test collection — **hasn't been checked**, since that requires
real pytest. This is a standard, well-documented usage pattern, so the
risk is lower than for logic errors, but still:

**Run `pytest` in your environment first**, before considering these
tests fully done:

```bash
cd services/quantum-core
pytest tests/unit/ -v
```

If you run into issues specifically with fixture discovery/test
collection (rather than the logic of the checks themselves), send the
output over and we'll sort it out separately from the already-confirmed
logic.
