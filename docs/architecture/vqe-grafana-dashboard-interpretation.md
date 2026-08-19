# How to interpret the Grafana dashboards

Two Grafana dashboards expose the VQE telemetry at different levels:

- **VQE Overview** — raw per-iteration metrics. Use this dashboard to inspect the optimizer's convergence and the hardware/software interaction of individual VQE iterations.
- **VQE Window Metrics** — Faust-derived 60-second tumbling-window aggregates. Use this dashboard to see the overall behavior of the VQE workload over time rather than individual optimizer iterations.

The dashboards use the same `experiment_id` selector, so both can be used to inspect the same VQE run.

## VQE Overview — raw iteration metrics

### VQE Energy / Convergence

This panel plots the energy returned by each COBYLA iteration.

- **X-axis** — VQE iteration.
- **Y-axis** — energy. For the current molecular H₂ Hamiltonian, this is expressed in **Hartree (Ha)**.
- **Energy** — the energy estimated for that particular optimizer iteration.

The important property is the **direction of convergence**. VQE minimizes the energy, so a sequence such as:

```text
-0.2 → -0.5 → -0.9 → -1.1 → -1.3
```

indicates that the optimizer is finding progressively lower-energy states.

A curve that stops improving and becomes approximately flat indicates that the optimizer has reached a region where further iterations produce little improvement. Oscillation or repeated movement to higher energies can indicate that the optimizer has not converged or that the underlying measurements are noisy.

The absolute value should not be interpreted in isolation: whether an energy is physically reasonable depends on the Hamiltonian and molecule being simulated. For the current H₂ example, the useful question is primarily whether the result approaches the expected ground-state energy.

### Quantum vs Classical Execution Time

This panel compares the two main parts of one VQE iteration:

- **Quantum Time (`quantum_time_s`)** — time spent waiting for the backend across the circuits required by the iteration.
- **Classical Time (`classical_time_s`)** — the remaining iteration wall time, including COBYLA and Python-side orchestration overhead.

Both values are measured in **seconds**.

A large quantum time relative to classical time means the iteration is dominated by backend interaction, which is expected for a real remote QPU or a backend with significant queue/execution latency.

A large classical component means the bottleneck is on the software side rather than in the quantum backend.

These metrics are particularly useful when comparing a local simulator with real hardware: a simulator may have very little backend waiting time, whereas a real QPU can spend most of an iteration waiting for queued or executing circuits.

`quantum_time_s` is total backend wait time. It should not be interpreted as pure physical QPU execution time because it can include queueing and other backend-side waiting.

## VQE Window Metrics — Faust tumbling-window aggregates

The second dashboard is based on the `vqe-window-metrics` topic produced by the Faust tumbling-window aggregation. The current window size is **60 seconds**.

Unlike the raw dashboard, these panels aggregate all VQE iterations belonging to the same experiment and window.

### Window Energy / Convergence

This panel shows:

- **Average** — average energy of all iterations observed in the current 60-second window.
- **Best** — lowest energy observed in that window.

The Y-axis is **energy**; for the current H₂ Hamiltonian, the unit is **Hartree (Ha)**.

Because these are window aggregates, the panel should be interpreted as:

> What energy values was this VQE workload producing during this period?

rather than:

> What was the energy at optimizer iteration N?

The **Best** value is especially useful for VQE because lower energy is better. A progressively decreasing best-energy trend suggests that the optimizer is continuing to discover better states.

The **Average** value provides a broader view of the behavior within the window. A large gap between average and best energy can indicate that the window contains iterations with substantially different energies, while a small gap suggests that iterations have stabilized around similar values.

The X-axis is **time**, not optimizer iteration. Therefore, this dashboard is intended for workload and time-series monitoring; the raw VQE dashboard is the better view for analyzing the optimizer's iteration-by-iteration convergence.

### Quantum / Classical Ratio

This panel shows:

```text
avg_quantum_time_s / avg_classical_time_s
```

The value is **dimensionless**.

For example:

```text
ratio = 10
```

means that, on average, the quantum/backend interaction portion of the iteration took approximately 10 times as long as the classical portion.

A high ratio means the workload is dominated by backend interaction. A ratio close to 1 means quantum and classical portions take approximately the same amount of time. A ratio below 1 means the classical part is taking longer.

This is a useful high-level indicator when evaluating where VQE execution time is going. It does not measure quantum advantage or algorithmic quality; it is purely an execution-time ratio.

### Windowed Execution Time

This panel shows the average execution times aggregated over each 60-second window:

- **Quantum** — average `quantum_time_s`, in **seconds**.
- **Classical** — average `classical_time_s`, in **seconds**.

Use this panel to identify changes in the execution profile over time.

For example, if classical time remains stable while quantum time suddenly increases, the change is likely associated with backend interaction rather than the optimizer itself. Conversely, an increase in classical time with stable quantum time points toward software-side processing or optimizer overhead.

The panel is complementary to the Quantum / Classical Ratio panel: the ratio shows the relative difference, while this panel shows the actual time spent.

### Window Metrics

The table provides the numerical values behind the dashboard's aggregate charts.

Important fields are:

- **Iterations** — number of VQE iterations observed in the 60-second window.
- **Retries** — total transient backend retries across those iterations.
- **Circuit Breaker Trips** — number of circuits that encountered an already open circuit breaker.
- **Avg Energy** — average iteration energy, in **Ha** for the current H₂ Hamiltonian.
- **Best Energy** — lowest energy observed in the window, in **Ha**.
- **Avg Quantum Time** — average backend interaction time, in **seconds**.
- **Avg Classical Time** — average classical/software iteration time, in **seconds**.
- **Quantum / Classical Ratio** — dimensionless ratio of the two average execution times.

The retry and circuit-breaker fields are primarily **operational metrics**. A rising retry count can indicate transient backend instability. Circuit breaker trips indicate that the backend was already considered unavailable by the polling layer and therefore circuits were prevented from executing.

A healthy VQE run would generally show:

- energy improving and eventually stabilizing;
- quantum/classical execution times remaining reasonably stable;
- few or no transient retries;
- zero circuit-breaker trips.

These are guidelines rather than strict pass/fail criteria. In particular, energy convergence depends on the optimizer, Hamiltonian, ansatz, initial parameters, and measurement noise, while retry behavior depends on the backend implementation.

## Recommended dashboard labeling

Explicitly label the Y-axis of both energy panels as **Energy (Ha)**. The documentation explains the unit, but the dashboard should communicate it without requiring the user to consult external documentation.
