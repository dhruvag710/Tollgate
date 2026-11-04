### Results: `bench/results/20261104T102940Z`

```
started_utc: 20261104T102940Z
git: 42544e45c1e46561cbf63c5b453cf950670d4e7a + uncommitted changes:
     M bench/k6/chat.js
     M bench/report.py
     M bench/run_all.sh
     M docker-compose.yml
    ?? bench/compare.py
    ?? tests/unit/test_compose.py
shell host: MINGW64_NT-10.0-26300 3.5.7-463ebcdc.x86_64 x86_64
docker: client 29.6.1, server 29.6.1
docker VM: 8 CPUs, 8140742656 bytes memory, Docker Desktop, kernel 6.18.33.2-microsoft-standard-WSL2
CPU (as the VM sees it): 11th Gen Intel(R) Core(TM) i7-1165G7 @ 2.80GHz
k6: k6 v1.0.0 (commit/41b4984b75-dirty, go1.24.2, linux/amd64)
parameters: RUNS=3 B1_RATE=100 B1_WARMUP_S=10 B1_DURATION_S=60 B2_START=50 B2_STEP=25 B2_MAX=2000 B2_WARMUP_S=5 B2_DURATION_S=20
```

#### B1: gateway overhead, non-streamed

`http_req_duration`, ms: median of 3 runs (range across runs).

| | p50 | p95 | p99 |
|---|---|---|---|
| Mock, direct | 1.21 (1.18 to 1.22) | 1.99 (1.99 to 2.00) | 2.18 (2.17 to 2.25) |
| Through the gateway | 3.80 (3.79 to 3.83) | 8.08 (8.04 to 8.15) | 15.28 (14.62 to 15.80) |
| **Overhead** | 2.61 (2.58 to 2.61) | 6.07 (6.05 to 6.15) | 13.03 (12.45 to 13.61) |

| Run | Requests (gateway) | Failed | Not started on time | Responses other than a complete 200 | Gateway CPU per request | Gateway throttled periods | Mock throttled periods |
|---|---|---|---|---|---|---|---|
| 1 | 6001 | 0.00% | 0 | none | 3.40 ms | 0 of 945 | 0 of 1420 |
| 2 | 6001 | 0.00% | 0 | none | 3.39 ms | 0 of 951 | 0 of 1422 |
| 3 | 6001 | 0.00% | 0 | none | 3.37 ms | 0 of 947 | 0 of 1420 |

#### B1b: time to first chunk, streamed

`http_req_waiting`, ms: median of 3 runs (range across runs).

| | p50 | p95 | p99 |
|---|---|---|---|
| Mock, direct | 1.18 (1.11 to 1.20) | 1.84 (1.83 to 1.87) | 2.00 (1.97 to 2.02) |
| Through the gateway | 2.30 (2.30 to 2.32) | 6.12 (5.98 to 6.21) | 12.58 (11.95 to 13.19) |
| **Overhead** | 1.13 (1.10 to 1.21) | 4.25 (4.14 to 4.38) | 10.56 (9.98 to 11.19) |

| Run | Requests (gateway) | Failed | Not started on time | Responses other than a complete 200 | Gateway CPU per request | Gateway throttled periods | Mock throttled periods |
|---|---|---|---|---|---|---|---|
| 1 | 6000 | 0.00% | 0 | none | 4.76 ms | 0 of 921 | 0 of 1425 |
| 2 | 6000 | 0.00% | 0 | none | 4.78 ms | 0 of 925 | 0 of 1423 |
| 3 | 6000 | 0.00% | 0 | none | 4.73 ms | 0 of 928 | 0 of 1422 |

#### B2: throughput ceiling (through nginx)

Highest RPS step that held (p99 overhead ≤ 50 ms, ≤ 0.1% failed, every request started on time): median of runs (range across runs).

| Gateway replicas | Max sustained RPS | Per run | What ended each ramp |
|---|---|---|---|
| 1 | 250 (225 to 250) | 250, 225, 250 | 275 RPS: p99 overhead 63 ms; 250 RPS: p99 overhead 89 ms; 275 RPS: p99 overhead 72 ms |
| 3 | 275 (250 to 325) | 325, 275, 250 | 350 RPS: p99 overhead 2321 ms, 245 requests not started on time; 300 RPS: p99 overhead 1315 ms, 142 requests not started on time; 275 RPS: p99 overhead 228 ms, 19 requests not started on time |

3 replicas sustain 1.10 times what one does (medians).

#### B2 steps, every run

run-1, 1 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Other than 200 | Gateway CPU per request (ms) | Gateway throttled periods | Held |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 4.8 / 13.6 | 2.3 | 11.4 | 0.00% | 0 | none | 4.76 | 0 of 367 | yes |
| 75 | 4.6 / 17.8 | 2.3 | 15.4 | 0.00% | 0 | none | 4.04 | 0 of 351 | yes |
| 100 | 4.2 / 17.6 | 2.2 | 15.4 | 0.00% | 0 | none | 3.59 | 0 of 347 | yes |
| 125 | 3.6 / 25.5 | 2.0 | 23.5 | 0.00% | 0 | none | 3.24 | 0 of 356 | yes |
| 150 | 3.4 / 25.4 | 2.1 | 23.3 | 0.00% | 0 | none | 2.70 | 0 of 352 | yes |
| 175 | 3.3 / 21.8 | 2.0 | 19.8 | 0.00% | 0 | none | 2.49 | 1 of 351 (avg 3 ms each) | yes |
| 200 | 3.1 / 26.7 | 2.0 | 24.7 | 0.00% | 0 | none | 2.40 | 0 of 343 | yes |
| 225 | 2.6 / 32.3 | 2.0 | 30.2 | 0.00% | 0 | none | 2.30 | 1 of 342 (avg 3 ms each) | yes |
| 250 | 2.6 / 50.7 | 2.1 | 48.6 | 0.00% | 0 | none | 2.31 | 1 of 348 (avg 0 ms each) | yes |
| 275 | 2.6 / 64.5 | 2.0 | 62.5 | 0.00% | 0 | none | 2.28 | 1 of 347 (avg 15 ms each) | no |

run-2, 1 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Other than 200 | Gateway CPU per request (ms) | Gateway throttled periods | Held |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 5.7 / 17.9 | 2.3 | 15.5 | 0.00% | 0 | none | 5.08 | 0 of 359 | yes |
| 75 | 4.7 / 18.6 | 2.3 | 16.3 | 0.00% | 0 | none | 4.20 | 0 of 356 | yes |
| 100 | 4.1 / 16.7 | 2.2 | 14.6 | 0.00% | 0 | none | 3.45 | 0 of 353 | yes |
| 125 | 3.5 / 15.5 | 2.2 | 13.4 | 0.00% | 0 | none | 2.99 | 0 of 351 | yes |
| 150 | 3.4 / 19.2 | 2.0 | 17.2 | 0.00% | 0 | none | 2.76 | 0 of 345 | yes |
| 175 | 3.3 / 24.4 | 2.0 | 22.5 | 0.00% | 0 | none | 2.57 | 0 of 341 | yes |
| 200 | 3.0 / 40.2 | 2.0 | 38.2 | 0.00% | 0 | none | 2.65 | 0 of 345 | yes |
| 225 | 2.7 / 38.2 | 2.0 | 36.2 | 0.00% | 0 | none | 2.33 | 0 of 347 | yes |
| 250 | 2.9 / 90.8 | 1.9 | 88.9 | 0.00% | 0 | none | 2.45 | 1 of 336 (avg 1 ms each) | no |

run-3, 1 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Other than 200 | Gateway CPU per request (ms) | Gateway throttled periods | Held |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 5.8 / 16.6 | 2.4 | 14.1 | 0.00% | 0 | none | 5.06 | 0 of 356 | yes |
| 75 | 4.7 / 17.3 | 2.2 | 15.1 | 0.00% | 0 | none | 4.06 | 0 of 357 | yes |
| 100 | 4.2 / 18.8 | 2.2 | 16.6 | 0.00% | 0 | none | 3.40 | 0 of 355 | yes |
| 125 | 3.5 / 16.5 | 2.1 | 14.4 | 0.00% | 0 | none | 3.02 | 0 of 359 | yes |
| 150 | 3.4 / 18.6 | 2.1 | 16.6 | 0.00% | 0 | none | 2.65 | 0 of 350 | yes |
| 175 | 3.3 / 23.9 | 1.9 | 22.0 | 0.00% | 0 | none | 2.50 | 0 of 347 | yes |
| 200 | 2.8 / 32.5 | 2.0 | 30.5 | 0.00% | 0 | none | 2.37 | 0 of 346 | yes |
| 225 | 2.7 / 35.5 | 2.0 | 33.5 | 0.00% | 0 | none | 2.31 | 0 of 338 | yes |
| 250 | 2.6 / 45.3 | 2.1 | 43.2 | 0.00% | 0 | none | 2.25 | 0 of 339 | yes |
| 275 | 2.7 / 73.8 | 2.0 | 71.7 | 0.00% | 0 | none | 2.28 | 1 of 344 (avg 3 ms each) | no |

run-1, 3 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Other than 200 | Gateway CPU per request (ms) | Gateway throttled periods | Held |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 4.6 / 9.7 | 2.3 | 7.4 | 0.00% | 0 | none | 5.55 | 0 of 1071 | yes |
| 75 | 4.5 / 9.3 | 2.4 | 6.9 | 0.00% | 0 | none | 4.60 | 0 of 1046 | yes |
| 100 | 3.9 / 8.8 | 2.2 | 6.6 | 0.00% | 0 | none | 3.66 | 0 of 1058 | yes |
| 125 | 3.5 / 8.9 | 2.2 | 6.7 | 0.00% | 0 | none | 3.36 | 0 of 1062 | yes |
| 150 | 3.3 / 8.5 | 2.1 | 6.4 | 0.00% | 0 | none | 3.09 | 0 of 1038 | yes |
| 175 | 3.2 / 9.1 | 2.0 | 7.1 | 0.00% | 0 | none | 2.93 | 0 of 1045 | yes |
| 200 | 3.2 / 9.5 | 2.0 | 7.5 | 0.00% | 0 | none | 2.90 | 0 of 1017 | yes |
| 225 | 3.1 / 10.2 | 2.0 | 8.2 | 0.00% | 0 | none | 2.78 | 0 of 1014 | yes |
| 250 | 3.1 / 11.5 | 2.0 | 9.5 | 0.00% | 0 | none | 2.75 | 0 of 1007 | yes |
| 275 | 3.1 / 13.9 | 2.0 | 12.0 | 0.00% | 0 | none | 2.78 | 0 of 1008 | yes |
| 300 | 3.1 / 14.0 | 1.8 | 12.2 | 0.00% | 0 | none | 2.79 | 0 of 994 | yes |
| 325 | 3.2 / 16.6 | 1.8 | 14.8 | 0.00% | 0 | none | 2.96 | 0 of 1013 | yes |
| 350 | 4.4 / 2322.8 | 1.8 | 2321.0 | 0.00% | 245 | none | 4.84 | 15 of 1003 (avg 24 ms each) | no |

run-2, 3 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Other than 200 | Gateway CPU per request (ms) | Gateway throttled periods | Held |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 5.5 / 10.8 | 2.3 | 8.5 | 0.00% | 0 | none | 5.52 | 0 of 1062 | yes |
| 75 | 4.5 / 8.9 | 2.3 | 6.6 | 0.00% | 0 | none | 4.42 | 0 of 1065 | yes |
| 100 | 4.0 / 9.0 | 2.2 | 6.8 | 0.00% | 0 | none | 3.78 | 0 of 1059 | yes |
| 125 | 3.4 / 8.8 | 2.1 | 6.7 | 0.00% | 0 | none | 3.29 | 0 of 1047 | yes |
| 150 | 3.5 / 13.8 | 2.1 | 11.7 | 0.00% | 0 | none | 3.43 | 0 of 1037 | yes |
| 175 | 3.3 / 9.3 | 2.0 | 7.3 | 0.00% | 0 | none | 2.97 | 0 of 1025 | yes |
| 200 | 3.2 / 9.2 | 2.0 | 7.2 | 0.00% | 0 | none | 2.87 | 0 of 1017 | yes |
| 225 | 3.1 / 11.2 | 2.1 | 9.1 | 0.00% | 0 | none | 2.80 | 0 of 1027 | yes |
| 250 | 3.1 / 12.7 | 2.1 | 10.5 | 0.00% | 0 | none | 2.82 | 0 of 1024 | yes |
| 275 | 3.2 / 14.8 | 2.0 | 12.8 | 0.00% | 0 | none | 2.94 | 0 of 1024 | yes |
| 300 | 3.9 / 1317.2 | 1.9 | 1315.3 | 0.00% | 142 | none | 4.96 | 23 of 1000 (avg 16 ms each) | no |

run-3, 3 replica(s):

| RPS | Gateway p50 / p99 (ms) | Mock p99 (ms) | p99 overhead (ms) | Failed | Not started | Other than 200 | Gateway CPU per request (ms) | Gateway throttled periods | Held |
|---|---|---|---|---|---|---|---|---|---|
| 50 | 5.6 / 13.0 | 2.4 | 10.6 | 0.00% | 0 | none | 6.31 | 0 of 1097 | yes |
| 75 | 4.7 / 10.3 | 2.4 | 7.9 | 0.00% | 0 | none | 4.61 | 0 of 1068 | yes |
| 100 | 4.1 / 9.0 | 2.1 | 6.9 | 0.00% | 0 | none | 3.87 | 0 of 1049 | yes |
| 125 | 3.5 / 8.8 | 2.2 | 6.6 | 0.00% | 0 | none | 3.46 | 0 of 1076 | yes |
| 150 | 3.3 / 8.6 | 2.0 | 6.6 | 0.00% | 0 | none | 3.03 | 0 of 1031 | yes |
| 175 | 3.3 / 9.4 | 2.0 | 7.4 | 0.00% | 0 | none | 2.85 | 0 of 1021 | yes |
| 200 | 3.2 / 9.2 | 1.9 | 7.2 | 0.00% | 0 | none | 2.89 | 0 of 1023 | yes |
| 225 | 3.2 / 10.9 | 2.0 | 8.9 | 0.00% | 0 | none | 2.82 | 0 of 1022 | yes |
| 250 | 3.2 / 12.5 | 2.0 | 10.5 | 0.00% | 0 | none | 2.86 | 0 of 1018 | yes |
| 275 | 3.8 / 230.0 | 2.0 | 228.0 | 0.00% | 19 | none | 4.54 | 9 of 1006 (avg 37 ms each) | no |

