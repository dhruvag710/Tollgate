# Benchmarks

Results of the v1 benchmarks in [SPEC.md §9](SPEC.md): **B1** (gateway overhead), **B1b**
(streaming time to first chunk) and **B2** (throughput ceiling, 1 and 3 replicas). Every
number below was produced by `bash bench/run_all.sh` and computed by
[bench/report.py](bench/report.py) from k6's raw output, which is committed in
[bench/results/20261103T190904Z/](bench/results/20261103T190904Z/). Each benchmark ran 3
times; tables give the **median** of the 3 runs and, in brackets, the **range** (lowest to
highest run), so one noisy run can't skew a result.

B3 to B6 need v2 features (failover, the semantic cache, the sliding-window limiter) and
haven't been run.

## Summary

| Benchmark | SPEC target | Result (median, range) | Met? |
|---|---|---|---|
| B1 overhead p50, non-streamed | < 5 ms at 500 RPS | 2.66 ms (2.57 to 2.68) at **100 RPS** | at 100 RPS only (see the deviation below) |
| B1 overhead p99, non-streamed | < 20 ms at 500 RPS | 43.00 ms (35.38 to 180.19) at 100 RPS | no |
| B1b time-to-first-chunk overhead p99 | < 20 ms | 142.34 ms (90.08 to 150.17) at 100 RPS | no |
| B2 max sustained RPS, 1 replica | report it | 125 (50 to 150) | – |
| B2 max sustained RPS, 3 replicas | report it | 250 (100 to 275) | – |

**Deviation from SPEC:** B1 ran at 100 RPS, not 500. One gateway replica limited to one
CPU cannot sustain 500 RPS on this machine (its B2 ceiling is 125 RPS), so at 500 RPS B1
would have measured queueing rather than overhead. B2 reports the real ceilings.

## Environment

| | |
|---|---|
| Machine | Laptop: Intel Core i7-1165G7 (4 cores, 8 threads, 2.8 GHz base), 15.7 GB RAM |
| OS | Windows 11 Home, build 26300; power plan: Balanced |
| Docker | Docker Desktop 29.6.1 on WSL2 (kernel 6.18.33.2-microsoft-standard-WSL2); VM: 8 CPUs, 7.6 GiB (WSL defaults, no `.wslconfig`) |
| Code | commit `47bb1bf` plus the uncommitted changes listed in [environment.txt](bench/results/20261103T190904Z/environment.txt): the `sniffio` dependency (see below), test-only migration logging, and `bench/` itself |
| Load generator | k6 v1.0.0 (`grafana/k6:1.0.0`), in a container on the same Docker network as the stack |
| Run | 2026-11-03, 19:09 to 20:10 UTC |

Fixed resource limits ([bench/docker-compose.bench.yml](bench/docker-compose.bench.yml)),
checked on the running containers by `docker inspect` (recorded in the results):

| Service | CPUs | Memory | Settings |
|---|---|---|---|
| gateway (each replica) | 1.0 | 512 MiB | 1 uvicorn worker (`WEB_CONCURRENCY=1`), `LOG_LEVEL=WARNING`, mock-only providers config |
| mock_provider | 1.5 | 512 MiB | 1 worker, access log off, log level warning; no latency, no TTFT delay, unpaced streams, 32 output tokens |
| postgres | 1.0 | 1 GiB | `max_wal_size=512MB`; the ledger table is emptied before every step |
| redis | 0.5 | 256 MiB | defaults |
| nginx (B2 only) | 0.5 | 128 MiB | [nginx/nginx.conf](nginx/nginx.conf) |
| k6 | 1.5 | 1.5 GiB | |

With 3 replicas the limits add up to the VM's 8 CPUs, and those are 8 hyperthreads on 4
physical cores: the 3-replica figures are for a fully committed laptop.

## Method

Each **step** is one k6 run ([bench/k6/chat.js](bench/k6/chat.js)) with two phases: the load
sent straight to the mock, then the same load through the gateway. Each phase is a warm-up
followed by a measured period; only the measured period is reported. Load is open-model
(k6 `constant-arrival-rate`: 50 pre-allocated VUs, up to 400), so a slow server can't slow
the arrival rate down. Requests are `POST /v1/chat/completions` for `gpt-4o-mini` with a
short prompt and `max_tokens: 32`, from a benchmark tenant with limits no test reaches.

- **Overhead** is the gateway's latency minus the mock's at the same percentile: a
  difference of percentiles, not a percentile of per-request differences.
- **B1** times non-streamed requests by `http_req_duration`. **B1b** times streamed ones by
  `http_req_waiting` (time to first byte): the gateway sends its response headers with the
  first SSE chunk, and the mock right before its first chunk, so this is time to first
  chunk. 100 RPS, 10 s warm-up, 60 s measured, one replica reached directly.
- **Gateway CPU per request** is the gateways' cgroup CPU time across a step divided by
  the requests they served in it. It includes their fixed costs (the benchmark's setup
  calls, the compose healthcheck), so it reads higher at low rates.
- **B2** goes through nginx. Steps start at 50 RPS and rise by 25 (5 s warm-up and 20 s
  measured per phase) until a step fails: p99 overhead above 50 ms, more than 0.1% of
  requests failed, or k6 unable to start requests on time. The last step that held is the
  ceiling. The mock is measured at each step's rate as that step's baseline.

## Results

#### B1: gateway overhead, non-streamed

`http_req_duration`, ms: median of 3 runs (range across runs).

| | p50 | p95 | p99 |
|---|---|---|---|
| Mock, direct | 1.08 (1.04 to 1.13) | 1.95 (1.89 to 1.98) | 2.14 (2.12 to 2.20) |
| Through the gateway | 3.70 (3.70 to 3.77) | 13.11 (10.61 to 13.94) | 45.12 (37.51 to 182.39) |
| **Overhead** | 2.66 (2.57 to 2.68) | 11.16 (8.63 to 12.05) | 43.00 (35.38 to 180.19) |

Requests through the gateway per run: 6000, 6001, 6001; failed: 0.00%, 0.00%, 0.00%;
requests k6 couldn't start on time (both sides): 0, 0, 0.

Gateway CPU per request, from its cgroup counters across the step: 4.42 ms, 4.30 ms,
4.24 ms; 100 ms CPU-quota periods in which it was throttled: 37 of 951, 43 of 948, 38 of 945.

#### B1b: time to first chunk, streamed

`http_req_waiting`, ms: median of 3 runs (range across runs).

| | p50 | p95 | p99 |
|---|---|---|---|
| Mock, direct | 0.91 (0.82 to 1.04) | 1.63 (1.24 to 1.82) | 1.95 (1.83 to 1.98) |
| Through the gateway | 2.46 (2.42 to 2.66) | 35.67 (29.29 to 50.67) | 144.18 (92.06 to 152.13) |
| **Overhead** | 1.54 (1.39 to 1.84) | 34.04 (27.47 to 49.44) | 142.34 (90.08 to 150.17) |

Requests through the gateway per run: 6001, 6000, 6000; failed: 0.02%, 0.00%, 0.00%;
requests k6 couldn't start on time (both sides): 0, 0, 0.

Gateway CPU per request, from its cgroup counters across the step: 6.56 ms, 5.95 ms,
6.02 ms; 100 ms CPU-quota periods in which it was throttled: 80 of 912, 56 of 946, 59 of 924.

#### B2: throughput ceiling (through nginx)

Highest RPS step that held (p99 overhead ≤ 50 ms, ≤ 0.1% failed, every request started
on time): median of runs (range across runs).

| Gateway replicas | Max sustained RPS | Per run | What ended each ramp |
|---|---|---|---|
| 1 | 125 (50 to 150) | 50, 125, 150 | 75 RPS: p99 overhead 64 ms; 150 RPS: p99 overhead 92 ms; 175 RPS: p99 overhead 72 ms |
| 3 | 250 (100 to 275) | 275, 100, 250 | 300 RPS: p99 overhead 103 ms; 125 RPS: p99 overhead 182 ms; 275 RPS: p99 overhead 55 ms |

3 replicas sustain 2.00 times what one does (medians).

Every step of every ramp, with gateway p50/p99 and the mock baseline, is in
[report.md](bench/results/20261103T190904Z/report.md).

## What the numbers say

- **Typical overhead is small; the tail is not.** The gateway adds about 2.7 ms at p50
  (1.5 ms to the first chunk of a stream), and its p50 stayed between 3.3 and 4.7 ms
  through every B2 step. Every B2 ramp ended on p99 overhead, never on failed requests
  or requests k6 couldn't start. The gateway's 1-CPU quota throttled it in 37 to 80 of
  roughly 910 to 950 100-ms periods per B1 step, even at 100 RPS. Each throttled period pauses
  the whole process for the rest of the period, which shows up directly at p95 and p99.
- **Streams cost more.** About 6.0 to 6.6 ms of gateway CPU per streamed request against
  4.2 to 4.4 ms non-streamed (relaying each SSE chunk), with more throttled periods and
  the worst tails (B1b p99).
- **Run-to-run spread on this laptop is large.** In B2 run 2 with 3 replicas, the 125 RPS
  step cost the gateways 10.40 ms of CPU per request, against 6.96 and 5.82 ms for the
  same step in runs 1 and 3, and failed: the machine itself was slower during it. The
  ranges show this; the medians aren't pulled by it.
- **Scaling.** 3 replicas sustain twice what one does (medians), with the limits filling
  every CPU of a 4-core machine.
- One streamed request failed (B1b run 1, 1 of 6001). The harness records k6's status
  check, not the status code, so the cause isn't known.

### Fixed before these runs

Profiling the gateway in its container showed httpcore attempting `import sniffio` several
times on every request, and the package wasn't installed in the image. A failed import
isn't cached, so each attempt searched `sys.path` again. `sniffio` is now a declared
dependency, and a test checks that a request triggers no import searches. All results
here are with the fix.

### Open items

- The gateway's compose healthcheck starts a new Python interpreter inside the container
  every 5 s, sharing the gateway's 1-CPU quota. That's a likely contributor to the
  throttled periods, and it isn't measured by these scripts.
- B1 at SPEC's 500 RPS needs either less CPU per request or more gateway processes than
  this machine can give fixed limits to.

## Reproducing

```sh
PYTHON=.venv/bin/python bash bench/run_all.sh   # ~60 minutes; results in bench/results/<UTC timestamp>/
python bench/report.py bench/results/<timestamp>  # the tables above, from the raw output
```

Each step leaves `<step>.json` (k6's end-of-test summary, minus `setup_data`, which holds
the throwaway benchmark tenant's API key), `<step>.verdict.json` (B2's stopping rule),
`<step>.log` (k6's console output), `<step>.docker-stats.txt` (one `docker stats` sample
mid-step) and the gateways' cgroup CPU counters before and after. Never run benchmarks
against real providers: the bench stack is pinned to `config/providers.test.yaml`.
