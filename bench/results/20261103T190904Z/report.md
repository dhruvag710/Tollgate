### Results: `bench/results/20261103T190904Z`

```
started_utc: 20261103T190904Z
git: 47bb1bfd69a0797555e46ba493bac65b9d761256 + uncommitted changes:
     M migrations/env.py
     M pyproject.toml
     M tests/conftest.py
     M tests/helpers.py
     M tests/integration/test_migrations.py
     M tests/integration/test_passthrough.py
```

#### B1: gateway overhead, non-streamed

`http_req_duration`, ms: median of 3 runs (range across runs).

| | p50 | p95 | p99 |
|---|---|---|---|
| Mock, direct | 1.08 (1.04 to 1.13) | 1.95 (1.89 to 1.98) | 2.14 (2.12 to 2.20) |
| Through the gateway | 3.70 (3.70 to 3.77) | 13.11 (10.61 to 13.94) | 45.12 (37.51 to 182.39) |
| **Overhead** | 2.66 (2.57 to 2.68) | 11.16 (8.63 to 12.05) | 43.00 (35.38 to 180.19) |

Requests through the gateway per run: 6000, 6001, 6001; failed: 0.00%, 0.00%, 0.00%; requests k6 couldn't start on time (both sides): 0, 0, 0.

Gateway CPU per request, from its cgroup counters across the step: 4.42 ms, 4.30 ms, 4.24 ms; 100 ms CPU-quota periods in which it was throttled: 37 of 951, 43 of 948, 38 of 945.

#### B1b: time to first chunk, streamed

`http_req_waiting`, ms: median of 3 runs (range across runs).

| | p50 | p95 | p99 |
|---|---|---|---|
| Mock, direct | 0.91 (0.82 to 1.04) | 1.63 (1.24 to 1.82) | 1.95 (1.83 to 1.98) |
| Through the gateway | 2.46 (2.42 to 2.66) | 35.67 (29.29 to 50.67) | 144.18 (92.06 to 152.13) |
| **Overhead** | 1.54 (1.39 to 1.84) | 34.04 (27.47 to 49.44) | 142.34 (90.08 to 150.17) |

Requests through the gateway per run: 6001, 6000, 6000; failed: 0.02%, 0.00%, 0.00%; requests k6 couldn't start on time (both sides): 0, 0, 0.

Gateway CPU per request, from its cgroup counters across the step: 6.56 ms, 5.95 ms, 6.02 ms; 100 ms CPU-quota periods in which it was throttled: 80 of 912, 56 of 946, 59 of 924.

#### B2: throughput ceiling (through nginx)

Highest RPS step that held (p99 overhead ≤ 50 ms, ≤ 0.1% failed, every request started on time): median of runs (range across runs).

| Gateway replicas | Max sustained RPS | Per run | What ended each ramp |
|---|---|---|---|
| 1 | 125 (50 to 150) | 50, 125, 150 | 75 RPS: p99 overhead 64 ms; 150 RPS: p99 overhead 92 ms; 175 RPS: p99 overhead 72 ms |
| 3 | 250 (100 to 275) | 275, 100, 250 | 300 RPS: p99 overhead 103 ms; 125 RPS: p99 overhead 182 ms; 275 RPS: p99 overhead 55 ms |

3 replicas sustain 2.00 times what one does (medians).

#### B2 steps, every run

run-1, 1 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Gateway CPU per request (ms) | Held |
|---|---|---|---|---|---|---|---|
| 50 | 4.6 / 17.5 | 2.2 | 15.3 | 0.00% | 0 | 6.50 | yes |
| 75 | 4.4 / 66.2 | 2.1 | 64.1 | 0.00% | 0 | 5.27 | no |

run-2, 1 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Gateway CPU per request (ms) | Held |
|---|---|---|---|---|---|---|---|
| 50 | 4.6 / 14.3 | 2.3 | 12.0 | 0.00% | 0 | 6.50 | yes |
| 75 | 4.5 / 20.4 | 2.2 | 18.1 | 0.00% | 0 | 5.14 | yes |
| 100 | 4.0 / 22.0 | 2.1 | 19.9 | 0.00% | 0 | 4.20 | yes |
| 125 | 3.6 / 44.7 | 2.1 | 42.7 | 0.00% | 0 | 3.81 | yes |
| 150 | 3.4 / 93.9 | 2.0 | 91.9 | 0.00% | 0 | 3.52 | no |

run-3, 1 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Gateway CPU per request (ms) | Held |
|---|---|---|---|---|---|---|---|
| 50 | 4.4 / 13.4 | 2.7 | 10.7 | 0.00% | 0 | 6.01 | yes |
| 75 | 4.6 / 32.7 | 2.3 | 30.4 | 0.00% | 0 | 5.34 | yes |
| 100 | 3.9 / 39.4 | 2.2 | 37.2 | 0.00% | 0 | 4.33 | yes |
| 125 | 3.5 / 25.2 | 2.2 | 23.0 | 0.00% | 0 | 3.77 | yes |
| 150 | 3.4 / 33.3 | 2.1 | 31.2 | 0.00% | 0 | 3.50 | yes |
| 175 | 3.4 / 74.0 | 2.0 | 72.0 | 0.00% | 0 | 3.43 | no |

run-1, 3 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Gateway CPU per request (ms) | Held |
|---|---|---|---|---|---|---|---|
| 50 | 4.4 / 10.2 | 2.2 | 8.0 | 0.00% | 0 | 10.79 | yes |
| 75 | 4.5 / 9.4 | 2.2 | 7.2 | 0.00% | 0 | 8.28 | yes |
| 100 | 3.9 / 9.5 | 2.1 | 7.4 | 0.00% | 0 | 7.00 | yes |
| 125 | 3.6 / 14.9 | 2.1 | 12.7 | 0.00% | 0 | 6.96 | yes |
| 150 | 3.4 / 10.5 | 2.1 | 8.4 | 0.00% | 0 | 5.53 | yes |
| 175 | 3.5 / 19.2 | 3.3 | 15.9 | 0.00% | 0 | 6.03 | yes |
| 200 | 3.3 / 20.7 | 2.0 | 18.8 | 0.00% | 0 | 4.71 | yes |
| 225 | 3.3 / 29.2 | 1.9 | 27.3 | 0.00% | 0 | 4.67 | yes |
| 250 | 3.3 / 25.8 | 1.9 | 23.9 | 0.00% | 0 | 4.47 | yes |
| 275 | 3.3 / 29.1 | 1.9 | 27.3 | 0.00% | 0 | 4.31 | yes |
| 300 | 3.4 / 104.3 | 1.8 | 102.5 | 0.00% | 0 | 4.55 | no |

run-2, 3 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Gateway CPU per request (ms) | Held |
|---|---|---|---|---|---|---|---|
| 50 | 4.7 / 10.7 | 2.1 | 8.6 | 0.00% | 0 | 11.30 | yes |
| 75 | 4.3 / 9.2 | 2.1 | 7.0 | 0.00% | 0 | 8.62 | yes |
| 100 | 4.0 / 9.5 | 1.9 | 7.6 | 0.00% | 0 | 8.06 | yes |
| 125 | 4.6 / 185.1 | 2.7 | 182.3 | 0.00% | 0 | 10.40 | no |

run-3, 3 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Gateway CPU per request (ms) | Held |
|---|---|---|---|---|---|---|---|
| 50 | 4.6 / 11.5 | 2.3 | 9.2 | 0.00% | 0 | 10.97 | yes |
| 75 | 4.1 / 9.3 | 2.2 | 7.1 | 0.00% | 0 | 7.98 | yes |
| 100 | 4.0 / 8.9 | 2.1 | 6.8 | 0.00% | 0 | 7.58 | yes |
| 125 | 3.5 / 9.6 | 2.0 | 7.6 | 0.00% | 0 | 5.82 | yes |
| 150 | 3.4 / 13.0 | 2.0 | 11.0 | 0.00% | 0 | 5.23 | yes |
| 175 | 3.3 / 10.2 | 1.9 | 8.3 | 0.00% | 0 | 5.07 | yes |
| 200 | 3.3 / 23.4 | 1.9 | 21.5 | 0.00% | 0 | 4.88 | yes |
| 225 | 3.3 / 37.8 | 1.9 | 35.9 | 0.00% | 0 | 4.70 | yes |
| 250 | 3.3 / 36.2 | 1.8 | 34.4 | 0.00% | 0 | 4.49 | yes |
| 275 | 3.5 / 57.1 | 1.9 | 55.1 | 0.00% | 0 | 4.62 | no |

