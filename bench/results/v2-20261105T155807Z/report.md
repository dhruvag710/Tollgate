## Results: `bench/results/v2-20261105T155807Z`

```
started_utc: 20261105T155807Z
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
parameters: PARTS="b5 b3" B5_RATE=20 B5_PROBE_RATE=20 B5_IDLE_S=60 B5_LIMIT=0 B5_MOCK_LATENCY_MS=1000 B5_OUTPUT_TOKENS=128 B5_CONFIGS="off on" B3_RATES="300 200" B3_RUNS=3 B3_WARMUP_S=10 B3_DURATION_S=120 B3_KILL_AT_S=40
host cpu: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
host cores_physical: 4
host cores_logical: 8
host cpu_max_mhz: 2701
host memory_bytes: 16805040128
```

### B5: cache impact

Models in the gateway image against B4's numbers, 500 pairs: largest similarity difference 2.6e-07, largest check-score difference 7.7e-06; decisions that differ: 0.

20,000 requests at 20/s; the mock answers each in 1000 ms, 128 tokens. Probes: 20/s each way.

| | Layer off | Layer on |
|---|---|---|
| Hit rate, all | 61.6% (12,315/20,000) | 64.2% (12,838/20,000) |
| Exact-layer hits | 61.6% (12,315/20,000) | 61.5% (12,298/20,000) |
| Semantic-layer hits | 0.0% (0/20,000) | 2.7% (540/20,000) |
| Cost saved (share of what the replay would cost uncached) | 61.5% ($0.9734 of $1.5831) | 64.1% ($1.0149 of $1.5831) |
| Latency p50 / p99, exact hits (ms) | 2.8 / 4.7 | 3.0 / 7.9 |
| Latency p50 / p99, semantic hits (ms) | n/a / n/a | 177.9 / 1,010.6 |
| Latency p50 / p99, misses (ms) | 1,006.1 / 1,011.0 | 1,036.3 / 1,911.1 |
| Miss path p50 / p95 / p99 (ms): embed, its wait, lookup | n/a | 25.2 / 65.7 / 91.4 |
| Embed p50 / p99 (ms) | n/a | 12.7 / 56.2 |
| Wait for the embedding thread p50 / p99 (ms) | n/a | 0.6 / 5.3 |
| Lookup p50 / p99 (ms) | n/a | 11.8 / 52.8 |
| Hit path p50 / p95 / p99 (ms): miss path, then the check | n/a | 173.2 / 815.6 / 1,001.8 |
| Rejected candidates (share of semantic lookups) | n/a | 26.1% (2,009/7,702) |
| Their check, its wait included, p50 / p99 (ms) | n/a | 140.3 / 872.2 |
| Their whole semantic lookup p50 / p99 (ms) | n/a | 172.1 / 956.2 |
| Skipped: pool full / timed out / failed | 0 / 0 / 0 | 72 / 68 / 0 |
| Non-cacheable overhead p50 / p95 / p99 (ms), probes alone | 3.10 / 4.37 / 6.92 | 3.22 / 4.22 / 5.37 |
| Probe requests (gateway / mock), probes alone | 1,200 / 1,201, failed 0 | 1,201 / 1,200, failed 0 |
| Non-cacheable overhead p50 / p95 / p99 (ms), during the replay | 4.30 / 5.90 / 11.06 | 4.79 / 8.95 / 19.26 |
| Probe requests (gateway / mock), during the replay | 20,000 / 20,001, failed 0 | 20,001 / 20,001, failed 0 |
| Replay requests failed | 0.00% (0/20,000) | 0.00% (0/20,000) |
| Not started on time (k6) | 0 | 0 |
| Cache entries at the end | 0 | 7,022 |
| Gateway CPU (cgroup) | 130 CPU s, throttled in 0 of 10,807 periods (0.0 s) | 534 CPU s, throttled in 2,490 of 10,821 periods (55.8 s) |
| Gateway memory at the end: held by processes / file cache (MiB) | 150 / 5 | 644 / 480 |
| Gateway memory peak / at the end (MiB), file cache included | 159 / 158 | 1,420 / 1,132 |
| Host | host 12% busy on average (max 38%), fastest core median 2,701 MHz of 2,701; flags: impossible clock reading (under 100 MHz) in 1 of 537 samples; clock limit under 85% of max in 3 of 536 samples (lowest 594 MHz) | host 18% busy on average (max 61%), fastest core median 2,701 MHz of 2,701; flags: clock limit under 85% of max in 3 of 537 samples (lowest 1593 MHz) |

Semantic hits by what they served (layer on):

| Request | Semantic hits | Served |
|---|---|---|
| MS MARCO first ask | 3 | another question: 3 |
| adversarial near-miss | 3 | false: its near-miss: 3 |
| QQP original | 54 | another question: 54 |
| QQP paraphrase | 480 | its pair's first question: 431; another question: 49 |

- QQP paraphrases answered with their pair's answer: 21.0% (431/2,052).
- Adversarial near-misses answered with their partner's answer (false hits): 1.57% (3/191).
- Exact-layer hits that served another question's answer: 0.

Outcomes of the semantic lookups (layer on): miss: 5,013, rejected: 2,009, hit: 540, verify-full: 72, verify-timeout: 36, embed-timeout: 32.

Rejected candidates by request: QQP paraphrase 1,363, QQP original 377, adversarial near-miss 163, MS MARCO first ask 92, adversarial first ask 14.

The lookup as the cache grows (layer on), by quarter of the replay:

| Quarter | Entries stored by its end | Checks a second | Embed p50 / p99 (ms) | Lookup p50 / p99 (ms) | Miss path p50 / p99 (ms) |
|---|---|---|---|---|---|
| 1 | 2,011 | 0.7 | 11.4 / 23.4 | 5.2 / 10.8 | 17.3 / 32.1 |
| 2 | 3,729 | 1.9 | 11.8 / 35.9 | 9.5 / 29.7 | 21.9 / 52.4 |
| 3 | 5,383 | 3.0 | 12.9 / 61.4 | 14.3 / 59.6 | 28.3 / 117.3 |
| 4 | 7,022 | 4.6 | 15.7 / 61.7 | 22.8 / 67.4 | 40.0 / 104.0 |

The gateway's own counters (tollgate_cache_lookups_total, semantic): full 72, hit 540, miss 5,013, rejected 2,009, timeout 68.

### B3: failover

Steady state: the first second after the kill from which every 1 s bucket of a 10 s window has no failed request and a p99 within twice the 30 s pre-kill p99 (or 10 ms of it). Requests are bucketed by when they started. The failover window runs from the kill to steady state, or to the end of the run if it never came.

| Run | Requests | Failed, whole run | Failed in the failover window | Time to steady state | Primary's breaker open after | Secondary's breaker opened | Pre-kill p99 (ms) | p99, first 10 s after (ms) | Retried on the secondary | Not started on time (k6) | Host |
|---|---|---|---|---|---|---|---|---|---|---|---|
| rate-200-run-1 | 23,556 | 1.84% (433/23,556) | 2.87% (433/15,066) | not reached | 7.6 s | no | 14.5 | 6,020.0 | 1,023 | 444 | no flags |
| rate-200-run-2 | 23,561 | 1.26% (296/23,561) | 1.96% (296/15,076) | not reached | 7.6 s | no | 14.4 | 6,017.4 | 1,023 | 440 | no flags |
| rate-200-run-3 | 23,562 | 0.85% (200/23,562) | 1.33% (200/15,074) | not reached | 7.6 s | no | 14.1 | 6,008.9 | 1,025 | 439 | no flags |
| rate-300-run-1 | 34,921 | 24.22% (8,457/34,921) | 38.08% (8,457/22,206) | not reached | 7.6 s | no | 37.2 | 6,045.0 | 1,220 | 1,080 | no flags |
| rate-300-run-2 | 34,968 | 23.59% (8,250/34,968) | 37.11% (8,250/22,233) | not reached | 7.6 s | no | 45.9 | 6,051.0 | 1,228 | 1,032 | no flags |
| rate-300-run-3 | 34,457 | 60.21% (20,748/34,457) | 95.65% (20,748/21,691) | not reached | 7.4 s | at 12.9 s | 42.4 | 6,037.6 | 1,084 | 1,544 | no flags |

Each replica's attempts at the secondary (successes / timeouts), from its own counters, and the run's failures in each 10 s after the kill:

| Run | Replica 1 | Replica 2 | Replica 3 | Failures per 10 s after the kill |
|---|---|---|---|---|
| rate-200-run-1 | 4,862 / 156 | 4,892 / 126 | 4,882 / 151 | 228, 38, 49, 15, 13, 17, 35, 38 |
| rate-200-run-2 | 4,904 / 122 | 4,899 / 133 | 4,980 / 41 | 166, 23, 17, 22, 20, 16, 19, 13 |
| rate-200-run-3 | 4,887 / 140 | 4,988 / 37 | 5,003 / 23 | 96, 16, 19, 12, 21, 3, 12, 21 |
| rate-300-run-1 | 6,801 / 608 | 6,849 / 564 | 107 / 7,285 | 711, 1204, 1145, 1133, 1137, 1137, 1115, 875 |
| rate-300-run-2 | 7,182 / 226 | 6,709 / 710 | 104 / 7,314 | 674, 1154, 1137, 1123, 1124, 1100, 1090, 848 |
| rate-300-run-3 | 726 / 55 | 112 / 668 | 112 / 669 | 855, 2659, 2999, 3001, 3000, 3000, 3001, 2233 |

- 200 RPS, 3 runs: failed 1.314% (929/70,679) in all; time to steady state never.
- 300 RPS, 3 runs: failed 35.895% (37,455/104,346) in all; time to steady state never.
- rate-200-run-1 failures: 504 1504: 433
- rate-200-run-2 failures: 504 1504: 296
- rate-200-run-3 failures: 504 1504: 200
- rate-300-run-1 failures: 504 1504: 8457
- rate-300-run-2 failures: 504 1504: 8250
- rate-300-run-3 failures: 504 1504: 1394, 503 1503: 19354

Each run's first seconds after the kill (second 0 starts at the kill):

rate-200-run-1:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 3.8 | 11.1 | 16 | 0 |
| -2 | 200 | 0 | 4.0 | 18.3 | 22 | 0 |
| -1 | 200 | 0 | 4.6 | 23.7 | 48 | 3 |
| 0 | 137 | 7 | 3006.2 | 6032.6 | 6064 | 137 |
| 1 | 100 | 2 | 3005.7 | 6007.4 | 6016 | 100 |
| 2 | 100 | 0 | 3006.1 | 3010.9 | 3016 | 100 |
| 3 | 152 | 1 | 3005.7 | 3014.5 | 6004 | 152 |
| 4 | 149 | 39 | 3007.5 | 6062.5 | 6082 | 149 |
| 5 | 150 | 47 | 3007.3 | 6015.7 | 6024 | 150 |
| 6 | 178 | 50 | 3007.4 | 6013.8 | 6030 | 178 |
| 7 | 190 | 41 | 6.1 | 6012.3 | 6022 | 190 |
| 8 | 200 | 15 | 4.8 | 3010.5 | 3014 | 200 |
| 9 | 200 | 26 | 5.3 | 3022.2 | 3027 | 200 |
| 10 | 200 | 9 | 5.2 | 3005.3 | 3025 | 200 |
| 11 | 200 | 7 | 5.5 | 3008.6 | 3013 | 200 |
| 12 | 200 | 5 | 4.2 | 3019.5 | 3048 | 200 |
| 13 | 200 | 7 | 4.8 | 3010.8 | 3014 | 200 |
| 14 | 200 | 2 | 4.2 | 47.0 | 3009 | 200 |
| 15 | 200 | 3 | 4.3 | 3007.9 | 3013 | 200 |

rate-200-run-2:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 3.9 | 13.8 | 18 | 0 |
| -2 | 200 | 0 | 3.9 | 7.8 | 15 | 0 |
| -1 | 200 | 0 | 4.5 | 28.6 | 35 | 3 |
| 0 | 138 | 0 | 3005.8 | 3014.9 | 3046 | 138 |
| 1 | 101 | 0 | 3005.7 | 3008.0 | 3008 | 101 |
| 2 | 99 | 0 | 3005.7 | 3030.1 | 3057 | 99 |
| 3 | 155 | 5 | 3005.8 | 6040.3 | 6059 | 155 |
| 4 | 150 | 22 | 3006.2 | 6013.8 | 6018 | 150 |
| 5 | 151 | 34 | 3007.6 | 6033.9 | 6041 | 151 |
| 6 | 175 | 40 | 3006.9 | 6025.8 | 6042 | 175 |
| 7 | 192 | 19 | 5.2 | 6006.0 | 6010 | 192 |
| 8 | 199 | 24 | 5.6 | 3036.6 | 3040 | 199 |
| 9 | 200 | 22 | 5.0 | 3021.7 | 3029 | 200 |
| 10 | 201 | 2 | 4.3 | 25.4 | 3007 | 201 |
| 11 | 199 | 3 | 4.9 | 3006.4 | 3023 | 199 |
| 12 | 200 | 5 | 4.4 | 3007.3 | 3026 | 200 |
| 13 | 201 | 3 | 4.2 | 3005.3 | 3013 | 201 |
| 14 | 200 | 2 | 3.9 | 55.3 | 3020 | 200 |
| 15 | 200 | 1 | 3.9 | 11.9 | 3004 | 200 |

rate-200-run-3:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 200 | 0 | 4.2 | 10.6 | 17 | 0 |
| -2 | 200 | 0 | 3.9 | 16.0 | 62 | 0 |
| -1 | 200 | 0 | 4.9 | 35.0 | 50 | 4 |
| 0 | 136 | 0 | 3006.2 | 3015.8 | 3046 | 136 |
| 1 | 100 | 0 | 3006.6 | 3012.3 | 3020 | 100 |
| 2 | 100 | 0 | 3006.7 | 3013.9 | 3037 | 100 |
| 3 | 156 | 2 | 3006.0 | 4380.2 | 6026 | 156 |
| 4 | 150 | 14 | 3007.7 | 6022.8 | 6029 | 150 |
| 5 | 150 | 22 | 3007.4 | 6013.0 | 6019 | 150 |
| 6 | 177 | 19 | 3006.7 | 6014.5 | 6027 | 177 |
| 7 | 192 | 14 | 6.7 | 6005.7 | 6010 | 192 |
| 8 | 200 | 11 | 5.0 | 3018.5 | 3024 | 200 |
| 9 | 200 | 14 | 5.2 | 3023.1 | 3026 | 200 |
| 10 | 200 | 4 | 4.7 | 3004.6 | 3007 | 200 |
| 11 | 200 | 1 | 4.7 | 21.8 | 3015 | 200 |
| 12 | 200 | 3 | 4.1 | 3004.5 | 3011 | 200 |
| 13 | 200 | 0 | 4.2 | 15.1 | 18 | 200 |
| 14 | 200 | 1 | 4.1 | 19.7 | 3004 | 200 |
| 15 | 200 | 2 | 3.9 | 47.2 | 3011 | 200 |

rate-300-run-1:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 300 | 0 | 4.2 | 22.9 | 25 | 0 |
| -2 | 300 | 0 | 4.2 | 73.9 | 85 | 0 |
| -1 | 300 | 0 | 7.7 | 109.6 | 130 | 8 |
| 0 | 195 | 1 | 3007.6 | 3061.2 | 6026 | 195 |
| 1 | 150 | 9 | 3007.1 | 6018.3 | 6045 | 150 |
| 2 | 150 | 51 | 3006.4 | 6060.4 | 6090 | 150 |
| 3 | 224 | 92 | 3007.7 | 6044.5 | 6052 | 224 |
| 4 | 215 | 100 | 3013.3 | 6053.9 | 6076 | 215 |
| 5 | 99 | 49 | 3022.8 | 6090.8 | 6095 | 99 |
| 6 | 133 | 74 | 6003.6 | 6055.0 | 6083 | 133 |
| 7 | 214 | 99 | 3002.6 | 6016.8 | 6054 | 214 |
| 8 | 249 | 101 | 6.2 | 3087.6 | 3105 | 249 |
| 9 | 291 | 135 | 9.1 | 3097.8 | 3113 | 291 |
| 10 | 300 | 131 | 8.6 | 3050.5 | 3075 | 300 |
| 11 | 300 | 132 | 9.7 | 3121.9 | 3147 | 300 |
| 12 | 300 | 121 | 8.4 | 3096.3 | 3108 | 300 |
| 13 | 300 | 116 | 6.6 | 3026.6 | 3046 | 300 |
| 14 | 300 | 119 | 5.6 | 3097.4 | 3120 | 300 |
| 15 | 300 | 120 | 7.2 | 3072.1 | 3114 | 300 |

rate-300-run-2:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 300 | 0 | 5.0 | 30.5 | 39 | 0 |
| -2 | 300 | 0 | 5.1 | 29.6 | 35 | 0 |
| -1 | 300 | 0 | 6.4 | 103.8 | 136 | 12 |
| 0 | 194 | 2 | 3007.2 | 3258.4 | 6019 | 194 |
| 1 | 150 | 10 | 3006.5 | 6007.5 | 6009 | 150 |
| 2 | 150 | 53 | 3006.7 | 6024.0 | 6034 | 150 |
| 3 | 226 | 87 | 3007.8 | 6115.7 | 6132 | 226 |
| 4 | 211 | 92 | 3009.9 | 6035.8 | 6042 | 211 |
| 5 | 97 | 43 | 3008.5 | 6060.8 | 6065 | 97 |
| 6 | 140 | 60 | 3008.4 | 6086.7 | 6106 | 140 |
| 7 | 225 | 101 | 3003.8 | 6106.2 | 6151 | 225 |
| 8 | 279 | 107 | 6.7 | 3067.4 | 3079 | 279 |
| 9 | 296 | 119 | 7.3 | 3135.0 | 3170 | 296 |
| 10 | 300 | 128 | 10.2 | 3133.2 | 3150 | 300 |
| 11 | 300 | 136 | 22.3 | 3073.5 | 3094 | 300 |
| 12 | 300 | 119 | 8.2 | 3035.5 | 3041 | 300 |
| 13 | 300 | 112 | 8.0 | 3127.2 | 3154 | 300 |
| 14 | 300 | 106 | 6.0 | 3019.3 | 3027 | 300 |
| 15 | 301 | 115 | 7.3 | 3033.6 | 3049 | 301 |

rate-300-run-3:

| Second | Requests | Failed | p50 (ms) | p99 (ms) | Max (ms) | Served by the secondary |
|---|---|---|---|---|---|---|
| -3 | 300 | 0 | 5.1 | 29.3 | 35 | 0 |
| -2 | 300 | 0 | 4.4 | 30.9 | 39 | 0 |
| -1 | 300 | 0 | 7.0 | 69.8 | 86 | 7 |
| 0 | 195 | 0 | 3006.8 | 3048.9 | 3061 | 195 |
| 1 | 149 | 6 | 3007.0 | 6006.4 | 6007 | 149 |
| 2 | 150 | 102 | 6005.1 | 6024.7 | 6037 | 150 |
| 3 | 225 | 155 | 6004.3 | 6040.6 | 6062 | 225 |
| 4 | 218 | 157 | 6004.3 | 6045.3 | 6065 | 218 |
| 5 | 49 | 31 | 6004.2 | 6101.8 | 6124 | 49 |
| 6 | 70 | 54 | 6004.3 | 6066.6 | 6071 | 70 |
| 7 | 87 | 61 | 3004.2 | 6009.3 | 6021 | 87 |
| 8 | 171 | 117 | 3002.8 | 3117.0 | 3146 | 171 |
| 9 | 246 | 172 | 3004.0 | 3087.3 | 3112 | 246 |
| 10 | 285 | 198 | 3004.2 | 3034.9 | 3040 | 285 |
| 11 | 232 | 161 | 3004.7 | 3092.8 | 3145 | 232 |
| 12 | 280 | 200 | 3003.4 | 3084.6 | 3110 | 256 |
| 13 | 300 | 300 | 2.0 | 6.9 | 17 | 0 |
| 14 | 300 | 300 | 2.5 | 14.9 | 22 | 0 |
| 15 | 300 | 300 | 1.9 | 14.9 | 18 | 0 |

