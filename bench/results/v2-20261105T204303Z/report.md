## Results: `bench/results/v2-20261105T204303Z`

```
started_utc: 20261105T204303Z
git: c5baab8d98228e40e8223e03cb7bef0a40cc348c + uncommitted changes:
     M .env.example
     M bench/b3_dns_check.sh
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
parameters: PARTS="b3" B5_RATE=20 B5_PROBE_RATE=20 B5_IDLE_S=60 B5_LIMIT=0 B5_MOCK_LATENCY_MS=1000 B5_OUTPUT_TOKENS=128 B5_CONFIGS="off on" B3_RATES="200:3 300:1" B3_RUNS=3 B3_WARMUP_S=10 B3_DURATION_S=120 B3_KILL_AT_S=40
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
host memory: committed 15,389 of 19,400 MB
host disk free (the repository's drive): 2.7G
```

### B3: failover

Steady state: the first second after the kill from which every 1 s bucket of a 10 s window has no failed request and a p99 within twice the 30 s pre-kill p99 (or 10 ms of it). Requests are bucketed by when they started. The failover window runs from the kill to steady state, or to the end of the run if it never came.

| Run | Requests | Failed, whole run | Failed in the failover window | Time to steady state | Primary's breaker open after | Secondary's breaker opened | Pre-kill p99 (ms) | p99, first 10 s after (ms) | Retried on the secondary | Not started on time (k6) | Host |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rate-200-run-1 | 23,831 | 0.00% (0/23,831) | 0.00% (0/1,231) | 7 s | 6.3 s | no | 189.7 | 1,134.5 | 1,045 | 169 | impossible clock reading (under 100 MHz) in 6 of 66 samples; clock limit under 85% of max in 9 of 60 samples (lowest 108 MHz) |
| rate-200-run-2 | 23,833 | 0.00% (0/23,833) | 0.00% (0/1,232) | 7 s | 6.2 s | no | 13.7 | 1,141.6 | 1,026 | 168 | no flags |
| rate-200-run-3 | 23,831 | 0.00% (0/23,831) | 0.00% (0/1,031) | 6 s | 6.0 s | no | 31.9 | 1,139.6 | 1,025 | 169 | clock limit under 85% of max in 6 of 66 samples (lowest 1728 MHz) |
| rate-300-run-1 | 35,711 | 0.00% (0/35,711) | 0.00% (0/3,611) | 13 s | 6.1 s | no | 28.3 | 1,300.0 | 1,520 | 289 | no flags |

Each replica's attempts at the secondary (successes / timeouts), from its own counters, and the run's failures in each 10 s after the kill:

| Run | Replica 1 | Replica 2 | Replica 3 | Failures per 10 s after the kill |
|---|---|---|---|---|
| rate-200-run-1 | 5,062 / 0 | 5,065 / 0 | 5,063 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |
| rate-200-run-2 | 5,130 / 0 | 5,125 / 0 | 5,127 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |
| rate-200-run-3 | 5,107 / 0 | 5,108 / 0 | 5,117 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |
| rate-300-run-1 | 7,614 / 0 | 7,623 / 0 | 7,614 / 0 | 0, 0, 0, 0, 0, 0, 0, 0 |

- 200 RPS, 3 runs: failed 0.000% (0/71,495) in all; time to steady state 7 s, 7 s, 6 s (median 7 s).
- 300 RPS, 1 runs: failed 0.000% (0/35,711) in all; time to steady state 13 s (median 13 s).

Each run's first seconds after the kill (second 0 starts at the kill):

rate-200-run-1:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 10.2 | 106.2 | 149 | 0 |
| -2 | 200 | 0 | 11.2 | 64.6 | 74 | 0 |
| -1 | 200 | 0 | 14.5 | 91.4 | 104 | 3 |
| 0 | 134 | 0 | 1105.0 | 1178.4 | 1181 | 134 |
| 1 | 152 | 0 | 1105.0 | 1110.0 | 1110 | 152 |
| 2 | 174 | 0 | 1104.9 | 1139.4 | 1151 | 174 |
| 3 | 185 | 0 | 1105.3 | 1128.7 | 1141 | 185 |
| 4 | 191 | 0 | 1105.5 | 1118.6 | 1159 | 191 |
| 5 | 195 | 0 | 1105.9 | 1145.8 | 1148 | 195 |
| 6 | 200 | 0 | 4.4 | 1105.0 | 1107 | 200 |
| 7 | 200 | 0 | 3.6 | 19.7 | 24 | 200 |
| 8 | 200 | 0 | 3.5 | 12.7 | 14 | 200 |
| 9 | 200 | 0 | 3.4 | 11.9 | 14 | 200 |
| 10 | 200 | 0 | 3.6 | 12.2 | 13 | 200 |
| 11 | 200 | 0 | 3.6 | 13.2 | 19 | 200 |
| 12 | 200 | 0 | 3.6 | 14.7 | 16 | 200 |
| 13 | 200 | 0 | 3.4 | 13.7 | 41 | 200 |
| 14 | 200 | 0 | 3.5 | 13.7 | 17 | 200 |
| 15 | 200 | 0 | 3.5 | 13.5 | 41 | 200 |

rate-200-run-2:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 3.8 | 16.2 | 38 | 0 |
| -2 | 200 | 0 | 3.7 | 38.5 | 63 | 0 |
| -1 | 200 | 0 | 4.1 | 23.1 | 39 | 3 |
| 0 | 136 | 0 | 1105.6 | 1114.3 | 1123 | 136 |
| 1 | 151 | 0 | 1105.5 | 1114.9 | 1138 | 151 |
| 2 | 173 | 0 | 1105.5 | 1120.0 | 1151 | 173 |
| 3 | 185 | 0 | 1105.8 | 1164.6 | 1176 | 185 |
| 4 | 189 | 0 | 1105.8 | 1172.3 | 1179 | 189 |
| 5 | 198 | 0 | 1106.5 | 1146.2 | 1148 | 198 |
| 6 | 200 | 0 | 5.3 | 41.3 | 52 | 200 |
| 7 | 200 | 0 | 4.0 | 22.1 | 40 | 200 |
| 8 | 200 | 0 | 3.9 | 13.6 | 18 | 200 |
| 9 | 200 | 0 | 4.0 | 20.9 | 42 | 200 |
| 10 | 200 | 0 | 4.0 | 13.3 | 19 | 200 |
| 11 | 200 | 0 | 4.6 | 16.9 | 24 | 200 |
| 12 | 200 | 0 | 3.9 | 27.1 | 38 | 200 |
| 13 | 200 | 0 | 4.0 | 20.6 | 23 | 200 |
| 14 | 200 | 0 | 3.8 | 7.0 | 14 | 200 |
| 15 | 200 | 0 | 3.8 | 15.6 | 16 | 200 |

rate-200-run-3:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 3.8 | 12.0 | 15 | 0 |
| -2 | 200 | 0 | 4.0 | 68.4 | 93 | 0 |
| -1 | 200 | 0 | 4.2 | 20.4 | 22 | 3 |
| 0 | 136 | 0 | 1105.7 | 1147.0 | 1186 | 136 |
| 1 | 151 | 0 | 1105.8 | 1109.9 | 1113 | 151 |
| 2 | 173 | 0 | 1105.6 | 1136.2 | 1156 | 173 |
| 3 | 185 | 0 | 1105.7 | 1146.6 | 1163 | 185 |
| 4 | 190 | 0 | 1105.8 | 1140.1 | 1147 | 190 |
| 5 | 196 | 0 | 1107.2 | 1149.6 | 1159 | 196 |
| 6 | 200 | 0 | 5.5 | 38.3 | 40 | 200 |
| 7 | 200 | 0 | 4.1 | 23.8 | 37 | 200 |
| 8 | 200 | 0 | 3.8 | 6.3 | 16 | 200 |
| 9 | 200 | 0 | 3.8 | 14.0 | 16 | 200 |
| 10 | 200 | 0 | 4.0 | 12.9 | 16 | 200 |
| 11 | 200 | 0 | 4.2 | 22.2 | 33 | 200 |
| 12 | 200 | 0 | 4.0 | 8.0 | 15 | 200 |
| 13 | 200 | 0 | 3.8 | 8.6 | 13 | 200 |
| 14 | 200 | 0 | 3.8 | 10.6 | 20 | 200 |
| 15 | 200 | 0 | 3.9 | 33.6 | 41 | 200 |

rate-300-run-1:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 300 | 0 | 4.0 | 24.3 | 30 | 0 |
| -2 | 300 | 0 | 4.1 | 18.6 | 26 | 0 |
| -1 | 300 | 0 | 5.9 | 62.8 | 79 | 7 |
| 0 | 194 | 0 | 1106.5 | 1138.3 | 1151 | 194 |
| 1 | 218 | 0 | 1106.6 | 1132.5 | 1149 | 218 |
| 2 | 257 | 0 | 1107.6 | 1127.7 | 1135 | 257 |
| 3 | 275 | 0 | 1115.8 | 1285.5 | 1351 | 275 |
| 4 | 278 | 0 | 1113.1 | 1235.4 | 1246 | 278 |
| 5 | 289 | 0 | 1178.9 | 1451.3 | 1524 | 289 |
| 6 | 300 | 0 | 55.3 | 261.8 | 477 | 300 |
| 7 | 300 | 0 | 7.4 | 187.3 | 210 | 300 |
| 8 | 300 | 0 | 5.8 | 30.8 | 35 | 300 |
| 9 | 300 | 0 | 5.2 | 29.2 | 35 | 300 |
| 10 | 300 | 0 | 5.0 | 27.8 | 37 | 300 |
| 11 | 300 | 0 | 6.4 | 82.3 | 91 | 300 |
| 12 | 300 | 0 | 5.0 | 65.7 | 74 | 300 |
| 13 | 300 | 0 | 5.7 | 29.2 | 41 | 300 |
| 14 | 300 | 0 | 4.7 | 28.4 | 32 | 300 |
| 15 | 300 | 0 | 4.8 | 26.7 | 32 | 300 |

