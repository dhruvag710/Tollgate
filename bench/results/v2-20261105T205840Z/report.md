## Results: `bench/results/v2-20261105T205840Z`

```
started_utc: 20261105T205840Z
git: c5baab8d98228e40e8223e03cb7bef0a40cc348c + uncommitted changes:
     M .env.example
     M BENCHMARKS.md
     M README.md
     M SPEC.md
     M bench/b3_dns_check.sh
     M bench/k6/failover.js
     M bench/run_v2.sh
     M bench/v2_report.py
     M config/providers.test.yaml
     M config/providers.yaml
     M pyproject.toml
     M src/tollgate/config.py
     M src/tollgate/main.py
     M src/tollgate/observability/metrics.py
     M tests/integration/test_failover.py
     M tests/unit/test_failover_policy.py
    ?? bench/b3_lookup_backlog.py
    ?? src/tollgate/providers/resolver.py
    ?? tests/integration/test_provider_dns.py
    ?? tests/unit/test_resolver.py
shell host: MINGW64_NT-10.0-26300 3.5.7-463ebcdc.x86_64 x86_64
docker: client 29.6.1, server 29.6.1
docker VM: 8 CPUs, 8140754944 bytes memory, Docker Desktop, kernel 6.18.33.2-microsoft-standard-WSL2
parameters: PARTS="b3" B5_RATE=20 B5_PROBE_RATE=20 B5_IDLE_S=60 B5_LIMIT=0 B5_MOCK_LATENCY_MS=1000 B5_OUTPUT_TOKENS=128 B5_CONFIGS="off on" B3_RATES="200:2" B3_RUNS=3 B3_WARMUP_S=10 B3_DURATION_S=120 B3_KILL_AT_S=40 B3_PRE_VUS=300
host cpu: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
host cores_physical: 4
host cores_logical: 8
host cpu_max_mhz: 2701
host memory_bytes: 16805040128
host os: Windows-11-10.0.26300-SP0
host power_plan: Balanced
host power_mode: Best performance
host on_battery: False
host python: 3.13.14
host torch: 2.15.0+cpu
host sentence_transformers: 6.2.0
host torch_threads: None
host memory: committed 15,522 of 19,400 MB
```

### B3: failover

Steady state: the first second after the kill from which every 1 s bucket of a 10 s window has no failed request and a p99 within twice the 30 s pre-kill p99 (or 10 ms of it). Requests are bucketed by when they started. The failover window runs from the kill to steady state, or to the end of the run if it never came.

| Run | Requests | Failed, whole run | Failed in the failover window | Time to steady state | Primary's breaker open after | Secondary's breaker opened | Pre-kill p99 (ms) | p99, first 10 s after (ms) | Retried on the secondary | Not started on time (k6) | Host |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rate-200-run-1 | 24,001 | 0.00% (0/24,001) | 0.00% (0/2,401) | 12 s | 6.0 s | no | 13.2 | 1,151.4 | 1,113 | 0 | no flags |
| rate-200-run-2 | 24,001 | 0.00% (0/24,001) | 0.00% (0/2,600) | 13 s | 6.0 s | no | 12.8 | 1,151.6 | 1,113 | 0 | no flags |

Each replica's attempts at the secondary (successes / timeouts), from its own counters, and the run's failures in each 10 s after the kill:

| Run | Replica 1 | Replica 2 | Replica 3 | Failures per 10 s after the kill |
|---|---|---|---|---|
| rate-200-run-1 | 5,165 / 0 | 5,167 / 0 | 5,168 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |
| rate-200-run-2 | 5,169 / 0 | 5,171 / 0 | 5,169 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |

- 200 RPS, 2 runs: failed 0.000% (0/48,002) in all; time to steady state 12 s, 13 s (median 12.5 s).

Each run's first seconds after the kill (second 0 starts at the kill):

rate-200-run-1:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 3.7 | 12.8 | 13 | 0 |
| -2 | 200 | 0 | 3.8 | 9.6 | 14 | 0 |
| -1 | 200 | 0 | 4.0 | 20.2 | 25 | 3 |
| 0 | 200 | 0 | 1105.5 | 1154.5 | 1159 | 200 |
| 1 | 200 | 0 | 1106.1 | 1194.3 | 1209 | 200 |
| 2 | 200 | 0 | 1106.1 | 1181.5 | 1189 | 200 |
| 3 | 200 | 0 | 1105.9 | 1126.5 | 1140 | 200 |
| 4 | 200 | 0 | 1106.2 | 1145.9 | 1150 | 200 |
| 5 | 200 | 0 | 1105.1 | 1125.6 | 1139 | 200 |
| 6 | 200 | 0 | 4.5 | 45.5 | 58 | 200 |
| 7 | 200 | 0 | 3.9 | 14.2 | 25 | 200 |
| 8 | 200 | 0 | 3.8 | 15.0 | 19 | 200 |
| 9 | 200 | 0 | 3.8 | 7.1 | 14 | 200 |
| 10 | 200 | 0 | 3.9 | 14.6 | 16 | 200 |
| 11 | 201 | 0 | 4.6 | 31.5 | 38 | 201 |
| 12 | 199 | 0 | 3.9 | 18.0 | 49 | 199 |
| 13 | 200 | 0 | 3.9 | 20.2 | 43 | 200 |
| 14 | 200 | 0 | 3.7 | 13.3 | 16 | 200 |
| 15 | 201 | 0 | 3.8 | 13.9 | 24 | 201 |

rate-200-run-2:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 3.8 | 10.3 | 15 | 0 |
| -2 | 200 | 0 | 3.8 | 12.8 | 14 | 0 |
| -1 | 200 | 0 | 4.1 | 15.7 | 22 | 3 |
| 0 | 200 | 0 | 1106.1 | 1188.8 | 1202 | 200 |
| 1 | 200 | 0 | 1105.9 | 1176.9 | 1182 | 200 |
| 2 | 200 | 0 | 1105.9 | 1139.6 | 1166 | 200 |
| 3 | 200 | 0 | 1106.5 | 1154.6 | 1165 | 200 |
| 4 | 200 | 0 | 1106.9 | 1156.0 | 1170 | 200 |
| 5 | 200 | 0 | 1105.2 | 1147.6 | 1159 | 200 |
| 6 | 200 | 0 | 4.7 | 34.4 | 47 | 200 |
| 7 | 200 | 0 | 3.9 | 14.9 | 17 | 200 |
| 8 | 200 | 0 | 4.0 | 10.8 | 17 | 200 |
| 9 | 200 | 0 | 3.8 | 9.0 | 12 | 200 |
| 10 | 200 | 0 | 4.0 | 15.0 | 21 | 200 |
| 11 | 200 | 0 | 4.2 | 18.2 | 25 | 200 |
| 12 | 200 | 0 | 4.2 | 26.9 | 49 | 200 |
| 13 | 200 | 0 | 4.0 | 7.4 | 16 | 200 |
| 14 | 200 | 0 | 3.8 | 14.9 | 16 | 200 |
| 15 | 200 | 0 | 3.8 | 10.1 | 12 | 200 |

