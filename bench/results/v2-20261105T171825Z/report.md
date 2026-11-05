## Results: `bench/results/v2-20261105T171825Z`

```
started_utc: 20261105T171825Z
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
parameters: PARTS="b5" B5_RATE=20 B5_PROBE_RATE=20 B5_IDLE_S=60 B5_LIMIT=0 B5_MOCK_LATENCY_MS=1000 B5_OUTPUT_TOKENS=128 B5_CONFIGS="off on" B3_RATES="300 200" B3_RUNS=3 B3_WARMUP_S=10 B3_DURATION_S=120 B3_KILL_AT_S=40
host cpu: 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
host cores_physical: 4
host cores_logical: 8
host cpu_max_mhz: 2701
```

### B5: cache impact

Models in the gateway image against B4's numbers, 500 pairs: largest similarity difference 2.6e-07, largest check-score difference 7.7e-06; decisions that differ: 0.

20,000 requests at 20/s; the mock answers each in 1000 ms, 128 tokens. Probes: 20/s each way.

| | Layer off | Layer on |
|---|---|---|
| Hit rate, all | 61.6% (12,315/20,000) | 64.2% (12,831/20,000) |
| Exact-layer hits | 61.6% (12,315/20,000) | 61.5% (12,298/20,000) |
| Semantic-layer hits | 0.0% (0/20,000) | 2.7% (533/20,000) |
| Cost saved (share of what the replay would cost uncached) | 61.5% ($0.9734 of $1.5831) | 64.1% ($1.0143 of $1.5831) |
| Latency p50 / p99, exact hits (ms) | 2.8 / 5.1 | 3.0 / 31.0 |
| Latency p50 / p99, semantic hits (ms) | n/a / n/a | 191.8 / 931.4 |
| Latency p50 / p99, misses (ms) | 1,006.2 / 1,012.3 | 1,038.8 / 1,932.3 |
| Miss path p50 / p95 / p99 (ms): embed, its wait, lookup | n/a | 27.5 / 67.1 / 103.0 |
| Embed p50 / p99 (ms) | n/a | 13.5 / 51.1 |
| Wait for the embedding thread p50 / p99 (ms) | n/a | 0.6 / 6.2 |
| Lookup p50 / p99 (ms) | n/a | 12.9 / 59.1 |
| Hit path p50 / p95 / p99 (ms): miss path, then the check | n/a | 186.9 / 716.7 / 923.2 |
| Rejected candidates (share of semantic lookups) | n/a | 26.0% (2,002/7,702) |
| Their check, its wait included, p50 / p99 (ms) | n/a | 145.0 / 875.5 |
| Their whole semantic lookup p50 / p99 (ms) | n/a | 178.9 / 942.5 |
| Skipped: pool full / timed out / failed | 0 / 0 / 0 | 75 / 74 / 0 |
| Non-cacheable overhead p50 / p95 / p99 (ms), probes alone | 3.07 / 3.93 / 5.48 | 3.61 / 4.69 / 6.47 |
| Probe requests (gateway / mock), probes alone | 1,201 / 1,201, failed 0 | 1,201 / 1,201, failed 0 |
| Non-cacheable overhead p50 / p95 / p99 (ms), during the replay | 4.42 / 6.45 / 11.30 | 5.19 / 11.17 / 35.23 |
| Probe requests (gateway / mock), during the replay | 20,001 / 20,001, failed 0 | 20,001 / 20,001, failed 0 |
| Replay requests failed | 0.00% (0/20,000) | 0.00% (0/20,000) |
| Not started on time (k6) | 0 | 0 |
| Cache entries at the end | 0 | 7,020 |
| Gateway CPU (cgroup) | 133 CPU s, throttled in 1 of 10,809 periods (0.1 s) | 553 CPU s, throttled in 2,658 of 10,826 periods (60.8 s) |
| Gateway memory at the end: held by processes / file cache (MiB) | 150 / 21 | 644 / 482 |
| Gateway memory peak / at the end (MiB), file cache included | 226 / 175 | 1,418 / 1,134 |
| Host | host 15% busy on average (max 64%), fastest core median 2,701 MHz of 2,701; flags: none | host 22% busy on average (max 59%), fastest core median 2,701 MHz of 2,701; flags: within 1,024 MB of the commit limit (least headroom 669 MB) |

Semantic hits by what they served (layer on):

| Request | Semantic hits | Served |
|---|---|---|
| MS MARCO first ask | 3 | another question: 3 |
| adversarial near-miss | 2 | false: its near-miss: 2 |
| QQP original | 52 | another question: 52 |
| QQP paraphrase | 476 | its pair's first question: 427; another question: 49 |

- QQP paraphrases answered with their pair's answer: 20.8% (427/2,052).
- Adversarial near-misses answered with their partner's answer (false hits): 1.05% (2/191).
- Exact-layer hits that served another question's answer: 0.

Outcomes of the semantic lookups (layer on): miss: 5,018, rejected: 2,002, hit: 533, verify-full: 75, verify-timeout: 44, embed-timeout: 30.

Rejected candidates by request: QQP paraphrase 1,364, QQP original 375, adversarial near-miss 159, MS MARCO first ask 89, adversarial first ask 15.

The lookup as the cache grows (layer on), by quarter of the replay:

| Quarter | Entries stored by its end | Checks a second | Embed p50 / p99 (ms) | Lookup p50 / p99 (ms) | Miss path p50 / p99 (ms) |
|---|---|---|---|---|---|
| 1 | 2,010 | 0.7 | 12.1 / 34.7 | 5.6 / 14.7 | 18.4 / 44.0 |
| 2 | 3,726 | 1.9 | 12.8 / 40.4 | 10.4 / 24.0 | 24.2 / 56.6 |
| 3 | 5,408 | 3.1 | 13.5 / 41.4 | 15.1 / 51.6 | 29.6 / 83.9 |
| 4 | 7,020 | 4.4 | 17.4 / 63.9 | 23.0 / 74.5 | 44.1 / 131.7 |

The gateway's own counters (tollgate_cache_lookups_total, semantic): full 75, hit 533, miss 5,018, rejected 2,002, timeout 74.

