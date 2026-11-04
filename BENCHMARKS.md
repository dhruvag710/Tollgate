# Benchmarks

Results of the v1 benchmarks in [SPEC.md §9](SPEC.md): **B1** (gateway overhead), **B1b**
(streaming time to first chunk) and **B2** (throughput ceiling, 1 and 3 replicas). Every
number below was produced by `bash bench/run_all.sh` and computed from k6's raw output by
[bench/report.py](bench/report.py) (the results) and [bench/compare.py](bench/compare.py)
(the before/after table). The raw output is committed:

- [bench/results/20261104T102940Z/](bench/results/20261104T102940Z/): the current results,
  with the healthcheck fix.
- [bench/results/20261103T190904Z/](bench/results/20261103T190904Z/): the baseline before
  it, kept for the [before/after comparison](#the-healthcheck-fix-before-and-after).

Each benchmark ran 3 times; tables give the **median** of the 3 runs and, in brackets, the
**range** (lowest to highest run), so one noisy run can't skew a result.

B3 to B6 need v2 features (failover, the semantic cache, the sliding-window limiter) and
haven't been run.

## Summary

| Benchmark | SPEC target | Result (median, range) | Met? |
|---|---|---|---|
| B1 overhead p50, non-streamed | < 5 ms at 500 RPS | 2.61 ms (2.58 to 2.61) at **100 RPS** | at 100 RPS only (see the deviation below) |
| B1 overhead p99, non-streamed | < 20 ms at 500 RPS | 13.03 ms (12.45 to 13.61) at 100 RPS | at 100 RPS only |
| B1b time-to-first-chunk overhead p99 | < 20 ms, same load as B1 | 10.56 ms (9.98 to 11.19) at 100 RPS | at 100 RPS only |
| B2 max sustained RPS, 1 replica | report it | 250 (225 to 250) | – |
| B2 max sustained RPS, 3 replicas | report it | 275 (250 to 325) | – |

**Deviation from SPEC:** B1 and B1b ran at 100 RPS, not 500. One gateway replica limited
to one CPU cannot sustain 500 RPS on this machine (its B2 ceiling is 250 RPS), so at 500
RPS B1 would measure queueing rather than overhead. The rate stayed at 100 RPS after the
healthcheck fix so the before/after compares like with like. B2 reports the real ceilings.

## Environment

| | |
|---|---|
| Machine | Laptop: Intel Core i7-1165G7 (4 cores, 8 threads, 2.8 GHz base), 15.7 GB RAM |
| OS | Windows 11 Home, build 26300; power plan: Balanced |
| Docker | Docker Desktop 29.6.1 on WSL2 (kernel 6.18.33.2-microsoft-standard-WSL2); VM: 8 CPUs, 7.6 GiB (WSL defaults, no `.wslconfig`) |
| Code | commit `42544e4` plus the uncommitted changes listed in [environment.txt](bench/results/20261104T102940Z/environment.txt): the healthcheck change in `docker-compose.yml`, and the harness changes in `bench/` (status codes, throttling counters). The gateway's code is the same as in the baseline. |
| Load generator | k6 v1.0.0 (`grafana/k6:1.0.0`), in a container on the same Docker network as the stack |
| Run | 2026-11-04, 10:29 to 11:47 UTC (baseline: 2026-11-03, 19:09 to 20:10 UTC) |

Fixed resource limits ([bench/docker-compose.bench.yml](bench/docker-compose.bench.yml)),
checked on the running containers by `docker inspect`, with the CFS quota each limit
became (the container's `/sys/fs/cgroup/cpu.max`: µs of CPU per period, then the period
in µs), all recorded in the results:

| Service | CPUs | CFS quota (`cpu.max`) | Memory | Settings |
|---|---|---|---|---|
| gateway (each replica) | 1.0 | 100000 per 100000 | 512 MiB | 1 uvicorn worker (`WEB_CONCURRENCY=1`), `LOG_LEVEL=WARNING`, mock-only providers config; healthcheck in bash builtins |
| mock_provider | 1.5 | 150000 per 100000 | 512 MiB | 1 worker, access log off, log level warning; no latency, no TTFT delay, unpaced streams, 32 output tokens; healthcheck in bash builtins |
| postgres | 1.0 | 100000 per 100000 | 1 GiB | `max_wal_size=512MB`; the ledger table is emptied before every step |
| redis | 0.5 | 50000 per 100000 | 256 MiB | defaults |
| nginx (B2 only) | 0.5 | 50000 per 100000 | 128 MiB | [nginx/nginx.conf](nginx/nginx.conf) |
| k6 | 1.5 | not recorded | 1.5 GiB | |

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
- **B2** goes through nginx. Steps start at 50 RPS and rise by 25 (5 s warm-up and 20 s
  measured per phase) until a step fails: p99 overhead above 50 ms, more than 0.1% of
  requests failed, or k6 unable to start requests on time. The last step that held is the
  ceiling. The mock is measured at each step's rate as that step's baseline.
- **Failures are explained, not only counted.** Every response other than a 200, and every
  stream that ends without `data: [DONE]`, is written to the step's log with its status,
  k6's error code, its duration and the start of its body. Responses are also counted by
  status (0 means no response at all: refused, reset or timed out). The "other than a
  complete 200" columns come from those counts.
- **CPU and throttling** come from the cgroup counters (`cpu.stat`) of the gateways, the
  mock and nginx, read before and after every step:
  - CPU used.
  - CFS periods: how many there were, in how many the container was throttled, and for
    how long.

  "17 of 362 (avg 43 ms each)" means the container was stopped for lack of quota in 17 of
  the step's 362 100-ms periods, for 43 ms on average each time. **Gateway CPU per
  request** is the gateways' CPU across the step divided by the requests they served in
  it, warm-up included. It includes fixed costs (the benchmark's setup calls, the
  healthcheck), so it reads higher at low rates.

## Results

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

Highest RPS step that held (p99 overhead ≤ 50 ms, ≤ 0.1% failed, every request started
on time): median of runs (range across runs).

| Gateway replicas | Max sustained RPS | Per run | What ended each ramp |
|---|---|---|---|
| 1 | 250 (225 to 250) | 250, 225, 250 | 275 RPS: p99 overhead 63 ms; 250 RPS: p99 overhead 89 ms; 275 RPS: p99 overhead 72 ms |
| 3 | 275 (250 to 325) | 325, 275, 250 | 350 RPS: p99 overhead 2321 ms, 245 requests not started on time; 300 RPS: p99 overhead 1315 ms, 142 requests not started on time; 275 RPS: p99 overhead 228 ms, 19 requests not started on time |

3 replicas sustain 1.10 times what one does (medians).

Every step of every ramp is in
[report.md](bench/results/20261104T102940Z/report.md). Each row has the gateway p50 and
p99, the mock baseline, statuses, CPU per request and throttled periods.

## The healthcheck fix: before and after

**Before:** the compose healthcheck of the gateway and the mock was
`python -c "import urllib.request; urllib.request.urlopen(...)"`, run every 5 s. Each
check started a new Python interpreter inside the container, so its CPU came out of the
gateway's 1-CPU quota.

**After:** the check is bash builtins. It opens `/dev/tcp`, sends `GET /healthz`, and
checks the status line for 200, without starting a new interpreter.
[tests/unit/test_compose.py](tests/unit/test_compose.py) fails if any healthcheck starts
Python. On Linux it also checks that the check passes on a 200, and fails on a 503 or with
nothing listening.

Both runs used the same machine, limits, workload and gateway code. The after run also
has the harness changes above, which change what k6 records and logs, not the load. From
`python bench/compare.py bench/results/20261103T190904Z bench/results/20261104T102940Z`:

| | Before | After | Change (medians) |
|---|---|---|---|
| B1 mock direct p50 (ms) | 1.08 (1.04 to 1.13) | 1.21 (1.18 to 1.22) | +0.13 |
| B1 mock direct p99 (ms) | 2.14 (2.12 to 2.20) | 2.18 (2.17 to 2.25) | +0.05 |
| B1 overhead p50 (ms) | 2.66 (2.57 to 2.68) | 2.61 (2.58 to 2.61) | -0.05 |
| B1 overhead p95 (ms) | 11.16 (8.63 to 12.05) | 6.07 (6.05 to 6.15) | -5.08 |
| B1 overhead p99 (ms) | 43.00 (35.38 to 180.19) | 13.03 (12.45 to 13.61) | -29.97 |
| B1 gateway CPU per request (ms) | 4.30 (4.24 to 4.42) | 3.39 (3.37 to 3.40) | -0.90 |
| B1 gateway throttled periods per step | 38 (37 to 43) | 0 (0 to 0) | -38 |
| B1b mock direct p50 (ms) | 0.91 (0.82 to 1.04) | 1.18 (1.11 to 1.20) | +0.26 |
| B1b mock direct p99 (ms) | 1.95 (1.83 to 1.98) | 2.00 (1.97 to 2.02) | +0.05 |
| B1b overhead p50 (ms) | 1.54 (1.39 to 1.84) | 1.13 (1.10 to 1.21) | -0.41 |
| B1b overhead p95 (ms) | 34.04 (27.47 to 49.44) | 4.25 (4.14 to 4.38) | -29.79 |
| B1b overhead p99 (ms) | 142.34 (90.08 to 150.17) | 10.56 (9.98 to 11.19) | -131.78 |
| B1b gateway CPU per request (ms) | 6.02 (5.95 to 6.56) | 4.76 (4.73 to 4.78) | -1.26 |
| B1b gateway throttled periods per step | 59 (56 to 80) | 0 (0 to 0) | -59 |
| B2 max sustained RPS, 1 replica(s) | 125 (50 to 150) | 250 (225 to 250) | +125 |
| B2 max sustained RPS, 3 replica(s) | 250 (100 to 275) | 275 (250 to 325) | +25 |

- **Throttling stopped.** The gateway went from 37 to 80 throttled periods per B1 and B1b
  step to none in any of them.
- **The CPU it frees is the healthcheck's.** Gateway CPU per request fell by 0.90 ms (B1)
  and 1.26 ms (B1b). The gateway's code didn't change, and the figure spreads all of the
  container's CPU over the requests it served. So the drop is CPU the healthcheck's
  interpreters had been using inside the quota.
- **The tail moved, the median didn't.** p99 overhead fell from 43.00 to 13.03 ms (B1)
  and from 142.34 to 10.56 ms (B1b). The p99 overhead ranges across runs narrowed from
  145 ms (B1) and 60 ms (B1b) wide to about 1 ms. p50 overhead moved by 0.05 and 0.41 ms. That is the pattern throttling
  produces: it delays only the requests in flight when it happens.
- **The control barely moved.** The mock, reached directly, got 0.13 to 0.26 ms slower at
  p50 and 0.05 ms slower at p99. k6 now keeps response bodies so it can log failures. The
  harness change doesn't account for the improvement.
- **B2:** the 1-replica ceiling doubled, from 125 to 250 RPS, and its range narrowed from
  "50 to 150" to "225 to 250". The 3-replica ceiling went from 250 to 275; see the
  [open items](#open-items).

## p99 spikes near 100 ms match Docker's CFS quota period

Docker enforces `--cpus` with the kernel's CFS bandwidth control. The gateway's `cpu.max`
is `100000 100000`: 100 ms of CPU per 100 ms period. A container that uses up its quota
before a period ends doesn't run again until the next period starts. The gateway is one
process with one event loop, so every request in flight at that moment waits with it, for
up to the rest of the period. A request caught by two throttles can wait up to about two
periods.

The baseline's tails sit at that scale:

- B1b p99 overhead was 90.08 to 150.17 ms across runs, and B1's worst run was 180.19 ms.
- The gateway was throttled in 37 to 80 periods per B1 and B1b step, for 47 to 54 ms (B1)
  and 67 to 73 ms (B1b) on average each time.
- Every baseline B2 ramp ended on a step where the gateway was throttled. For one replica,
  that was 17 periods in each failing step, averaging 43 to 54 ms each, with p99 overhead
  of 64, 92 and 72 ms. With three, the gateways were throttled in 66 to 74 periods, and
  p99 overhead was 102.5, 182.3 and 55.1 ms.

These throttle figures are in the baseline's per-run and per-step tables, printed by
`python bench/report.py bench/results/20261103T190904Z`. With throttling gone (0 periods
in every B1 and B1b step), B1 p99 overhead is 12.45 to 13.61 ms and B1b's 9.98 to 11.19
ms.

Not every tail near 100 ms is throttling. B2 run 2 with one replica ended at 250 RPS on 89
ms p99 overhead, while the gateway was throttled in 1 of 336 periods, for about 1 ms.
Throttling is now recorded at every step, so the two cases can be told apart.

## What the numbers say

- **Overhead is small at 100 RPS, in the tail too.** The gateway adds 2.61 ms at p50 and
  13.03 ms at p99 (non-streamed). To the first chunk of a stream, it adds 1.13 ms and
  10.56 ms. That is under SPEC's 5 ms and 20 ms targets, but at a fifth of SPEC's 500 RPS
  (see the deviation).
- **Streams cost more.** About 4.7 to 4.8 ms of gateway CPU per streamed request against
  3.4 ms non-streamed (relaying each SSE chunk). CPU per request falls as the rate rises
  and fixed costs spread out: one replica in B2 used 4.76 to 5.08 ms at 50 RPS and 2.25 to
  2.45 ms at 250 RPS.
- **One replica's ceiling is not its CPU quota.** Its p99 overhead climbs with load: run 1
  went from 11.4 ms at 50 RPS to 30.2 at 225, 48.6 at 250 and 62.5 at 275. In the steps
  that ended its ramps, it was throttled in at most 1 period. A `docker stats` sample at
  275 RPS showed it at 59% of a CPU.
- **Three replicas fail differently, and barely scale.** Their ramps ended in one step on
  a collapse rather than a climb:
  - p99 overhead went from 10.5 to 14.8 ms at the last step that held, to 228 to 2321 ms
    at the next.
  - k6 couldn't start 19 to 245 requests on time.
  - No gateway had been throttled at the step before.
  - 3 replicas sustain 1.10 times what one does. The counters recorded here don't identify
    the bottleneck (see the open items).
- **No failures.** Every request in every B1, B1b and B2 step got a complete 200, in all 3
  runs. The baseline's one failed stream (B1b run 1, 1 of 6001) came before status codes
  were recorded, and its log holds nothing more, so its cause stays unknown. A repeat
  would now be logged with its status, k6 error code and body.

### Fixed before these runs

- **`sniffio`** (also in the baseline). Profiling the gateway in its container showed
  httpcore attempting `import sniffio` several times on every request, and the package
  wasn't installed in the image. A failed import isn't cached, so each attempt searched
  `sys.path` again. `sniffio` is now a declared dependency, and a test checks that a
  request triggers no import searches.
- **The healthcheck** (current results only): see
  [the before/after](#the-healthcheck-fix-before-and-after).

### Open items

- **The 3-replica bottleneck.** No gateway, nor the mock or nginx, was throttled before the
  3-replica collapse. The one `docker stats` sample per step showed no container near its
  limit. At 350 RPS in run 1, the gateways used 30 to 37% of a CPU, the mock 17%, Redis
  8%, nginx 7% and k6 19%. The harness doesn't record cgroup counters for Redis, Postgres
  or k6, or the host's own CPU. The limits fill all 8 hyperthreads of a 4-core laptop that
  also runs Windows, so recording these is the next step before reading anything into the
  1.10x.
- **B1 at SPEC's 500 RPS** needs either less CPU per request or more gateway processes
  than this machine can give fixed limits to.

## Reproducing

```sh
PYTHON=.venv/bin/python bash bench/run_all.sh   # ~80 minutes; results in bench/results/<UTC timestamp>/
python bench/report.py bench/results/<timestamp>  # the result tables, from the raw output
python bench/compare.py bench/results/<before> bench/results/<after>  # a before/after table
```

Each step leaves these files:

- `<step>.json`: k6's end-of-test summary, minus `setup_data`, which holds the throwaway
  benchmark tenant's API key.
- `<step>.verdict.json`: B2's stopping rule.
- `<step>.log`: k6's console output, including a line for every failed response.
- `<step>.docker-stats.txt`: one `docker stats` sample mid-step.
- `<step>.cpu-before.txt` and `<step>.cpu-after.txt`: the cgroup CPU counters of the
  gateways, mock and nginx.

`environment.txt` records the host, Docker, k6, the parameters and each container's
limits, `cpu.max` and healthcheck. Never run benchmarks against real providers: the bench
stack is pinned to `config/providers.test.yaml`.
