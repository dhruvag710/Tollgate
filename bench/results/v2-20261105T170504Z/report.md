## Results: `bench/results/v2-20261105T170504Z`

```
started_utc: 20261105T170504Z
git: 87a27ede4198431b0b7ce2d613b63534f7319bdd + uncommitted changes:
     M .env.example
     M .gitignore
     M Dockerfile
     M SPEC.md
     M bench/cache_eval/pairs.py
     M src/tollgate/api/v1/chat.py
     M src/tollgate/cache/embedder.py
     M src/tollgate/cache/semantic.py
     M src/tollgate/config.py
     M src/tollgate/main.py
     M src/tollgate/observability/metrics.py
     M src/tollgate/observability/timing.py
     M tests/helpers.py
     M tests/integration/test_semantic_cache.py
     M tests/unit/test_semantic.py
    ?? bench/b3_dns_check.sh
    ?? bench/cache_eval/check_gateway_models.py
    ?? bench/docker-compose.b3-by-ip.yml
    ?? bench/docker-compose.b5.yml
    ?? bench/k6/cache_workload.js
    ?? bench/k6/failover.js
    ?? bench/run_v2.sh
    ?? bench/v2_report.py
    ?? bench/watch_host.py
    ?? bench/workloads/
    ?? docker-compose.semantic.yml
    ?? scripts/fetch_models.py
    ?? src/tollgate/cache/pool.py
    ?? src/tollgate/cache/verifier.py
    ?? tests/unit/test_pool.py
shell host: MINGW64_NT-10.0-26300 3.5.7-463ebcdc.x86_64 x86_64
docker: client 29.6.1, server 29.6.1
docker VM: 8 CPUs, 8140746752 bytes memory, Docker Desktop, kernel 6.18.33.2-microsoft-standard-WSL2
parameters: PARTS="b3" B5_RATE=20 B5_PROBE_RATE=20 B5_IDLE_S=60 B5_LIMIT=0 B5_MOCK_LATENCY_MS=1000 B5_OUTPUT_TOKENS=128 B5_CONFIGS="off on" B3_RATES="300" B3_RUNS=2 B3_WARMUP_S=10 B3_DURATION_S=120 B3_KILL_AT_S=40
host cpu: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
host cores_physical: 4
host cores_logical: 8
host cpu_max_mhz: 2701
```

### B3: failover

Steady state: the first second after the kill from which every 1 s bucket of a 10 s window has no failed request and a p99 within twice the 30 s pre-kill p99 (or 10 ms of it). Requests are bucketed by when they started. The failover window runs from the kill to steady state, or to the end of the run if it never came.

| Run | Requests | Failed, whole run | Failed in the failover window | Time to steady state | Primary's breaker open after | Secondary's breaker opened | Pre-kill p99 (ms) | p99, first 10 s after (ms) | Retried on the secondary | Not started on time (k6) | Host |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rate-300-by-ip-run-1 | 34,496 | 0.00% (0/34,496) | 0.00% (0/2,695) | 14 s | 7.4 s | no | 76.3 | 7,868.0 | 1,269 | 1,505 | impossible clock reading (under 100 MHz) in 3 of 66 samples; clock limit under 85% of max in 12 of 63 samples (lowest 486 MHz) |
| rate-300-by-ip-run-2 | 35,159 | 0.00% (0/35,159) | 0.00% (0/2,015) | 9 s | 7.6 s | no | 1074.6 | 4,378.9 | 1,459 | 841 | impossible clock reading (under 100 MHz) in 1 of 66 samples; clock limit under 85% of max in 6 of 65 samples (lowest 189 MHz) |

Each replica's attempts at the secondary (successes / timeouts), from its own counters, and the run's failures in each 10 s after the kill:

| Run | Replica 1 | Replica 2 | Replica 3 | Failures per 10 s after the kill |
|---|---|---|---|---|
| rate-300-by-ip-run-1 | 7,248 / 0 | 7,239 / 0 | 7,239 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |
| rate-300-by-ip-run-2 | 7,462 / 0 | 7,516 / 0 | 7,473 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |

- 300 RPS, 2 runs: failed 0.000% (0/69,655) in all; time to steady state 14 s, 9 s (median 11.5 s).

Each run's first seconds after the kill (second 0 starts at the kill):

rate-300-by-ip-run-1:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 300 | 0 | 3.6 | 17.5 | 34 | 0 |
| -2 | 301 | 0 | 3.4 | 16.3 | 20 | 0 |
| -1 | 300 | 0 | 4.1 | 35.3 | 43 | 7 |
| 0 | 191 | 0 | 3017.1 | 3265.4 | 3324 | 191 |
| 1 | 150 | 0 | 3031.3 | 3126.7 | 3140 | 150 |
| 2 | 150 | 0 | 3031.8 | 3281.9 | 3369 | 150 |
| 3 | 227 | 0 | 3422.1 | 8181.1 | 8730 | 227 |
| 4 | 207 | 0 | 4410.6 | 8274.3 | 8774 | 207 |
| 5 | 149 | 0 | 4918.9 | 7437.2 | 7686 | 149 |
| 6 | 125 | 0 | 4811.9 | 6812.3 | 7042 | 125 |
| 7 | 122 | 0 | 3904.6 | 5691.3 | 5925 | 122 |
| 8 | 127 | 0 | 1776.8 | 4295.1 | 4353 | 127 |
| 9 | 164 | 0 | 1779.8 | 3738.3 | 3931 | 164 |
| 10 | 186 | 0 | 1176.4 | 2689.5 | 2991 | 186 |
| 11 | 297 | 0 | 489.9 | 1649.4 | 1930 | 297 |
| 12 | 300 | 0 | 158.3 | 1016.9 | 1132 | 300 |
| 13 | 300 | 0 | 5.6 | 188.5 | 239 | 300 |
| 14 | 300 | 0 | 3.9 | 24.7 | 29 | 300 |
| 15 | 300 | 0 | 4.2 | 62.2 | 72 | 300 |

rate-300-by-ip-run-2:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 300 | 0 | 3.6 | 24.0 | 29 | 0 |
| -2 | 300 | 0 | 3.7 | 20.8 | 28 | 0 |
| -1 | 300 | 0 | 8.1 | 68.4 | 111 | 6 |
| 0 | 267 | 0 | 3008.5 | 3067.8 | 3071 | 267 |
| 1 | 150 | 0 | 3007.8 | 3032.2 | 3057 | 150 |
| 2 | 149 | 0 | 3007.5 | 3020.6 | 3024 | 149 |
| 3 | 262 | 0 | 3009.1 | 3061.3 | 3080 | 262 |
| 4 | 187 | 0 | 3035.5 | 4486.5 | 4952 | 187 |
| 5 | 150 | 0 | 3073.6 | 4544.2 | 5239 | 150 |
| 6 | 261 | 0 | 3247.1 | 4879.3 | 5127 | 261 |
| 7 | 289 | 0 | 60.4 | 3952.0 | 4399 | 289 |
| 8 | 300 | 0 | 72.6 | 2186.2 | 2405 | 300 |
| 9 | 300 | 0 | 234.6 | 1889.6 | 2453 | 300 |
| 10 | 300 | 0 | 99.8 | 1129.6 | 1432 | 300 |
| 11 | 300 | 0 | 8.0 | 519.8 | 680 | 300 |
| 12 | 300 | 0 | 5.0 | 53.4 | 66 | 300 |
| 13 | 300 | 0 | 5.4 | 33.2 | 36 | 300 |
| 14 | 300 | 0 | 5.5 | 77.7 | 98 | 300 |
| 15 | 300 | 0 | 4.9 | 30.1 | 38 | 300 |

