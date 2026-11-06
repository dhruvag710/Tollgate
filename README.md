# Tollgate

A self-hosted, OpenAI-compatible LLM gateway. Point your OpenAI SDK at Tollgate by
changing only `base_url`. See [SPEC.md](SPEC.md) for the full design.

**Status:** v1 (phases 1.1–1.7) is complete: passthrough for streaming and non-streaming
chat completions, the mock provider, tenants with API keys, per-tenant rate limits, a
usage ledger in Postgres with monthly budgets, Prometheus metrics and JSON logs, and a
three-replica setup behind nginx. Benchmarks: [BENCHMARKS.md](BENCHMARKS.md). v2 adds
provider failover with circuit breakers shared by every replica (Phase 2.1), an
exact-match response cache (Phase 2.3), and a semantic cache (Phase 2.4), off by default:
B5 measured it over its latency budget (see below).

## Architecture

```
  client  (any OpenAI SDK: only base_url changes)
    │
    ▼
  nginx  ·························  optional, for several replicas (docker-compose.scale.yml)
    │
    ▼
  gateway replica(s)  FastAPI + uvicorn, one worker each; /metrics, JSON logs
    │  │  │
    │  │  └─►  providers  each model's, in priority order, failing over between them;
    │  │                  the mock providers by default, or any OpenAI-compatible API
    │  │                  (OpenAI, OpenRouter, Ollama) via config/providers.yaml
    │  └────►  Postgres   tenants, API keys, the usage ledger (batched inserts)
    └───────►  Redis      key cache, budget check, RPM and TPM buckets, month-to-date
                          spend, the providers' circuit breakers, cached answers
```

A chat request, with a cached key, makes one Redis call before the provider: a Lua
script that authenticates the key, checks the tenant's budget, and takes from both rate
limits, all or nothing. The provider's response streams straight through. Once its usage
is known, the request becomes one row in the ledger (queued, written in batches), and one
more Redis call, off the response path, reconciles its token reservation and adds its
cost to the tenant's spend. Any number of replicas share Postgres and Redis, so limits,
budgets and revocations hold across all of them. [SPEC.md §4](SPEC.md) has the full
lifecycle.

Streaming is a true passthrough: each SSE event is forwarded as it arrives. Tollgate
always asks the provider for the final usage chunk, but only forwards it if the client
asked for it too. If the client disconnects, the upstream request is cancelled and the
partial usage is recorded, estimated with tiktoken. An upstream failure after the first
event ends the stream with an OpenAI-style error event; a failure before it is a normal
HTTP error.

### Failover and circuit breakers

A model lists its providers in priority order (`routes` in the providers file). A
request that fails at one before any byte of the response has arrived moves on to the
next: a connection error, a timeout before the first byte (`first_byte_timeout_s`, by
default the read timeout), a 5xx, a 429, or a 401/403 (the provider refusing Tollgate's
own key). A client error (any other 4xx) is relayed, never retried, and so is anything
that fails after the provider's first byte: a stream that breaks midway ends with an
error event, since the provider may already bill what it started.

Each provider has a circuit breaker in Redis, shared by every replica. It opens once at
least half of at least 20 attempts over the last 10 s have failed. Requests then skip
the provider for a 30 s cooldown, after which one request, across all replicas, probes
it: success closes the breaker, failure opens it again. The defaults can be changed in
the providers file, and per provider. If every route is open the request gets a 503 with
`Retry-After`. Routing costs no Redis call of its own: the admission call already reads
the breakers, and the settle call each request makes counts its success.
`GET /admin/providers` shows each breaker's state.

No request ever waits on a name lookup. Each provider's hostname is looked up in the
background every 5 s (`PROVIDER_DNS_REFRESH_S`), and new connections use the cached
addresses. If a name stops resolving, attempts at that provider fail at once, count against
its breaker, and move on to the next provider. If the resolver stops answering, the cached
addresses stay in use. Connecting times out after 1.1 s (`connect_timeout_s` in the
providers file).

### Exact-match cache

A request whose answer shouldn't vary can be answered from Redis: temperature at most
0.3 (`CACHE_MAX_TEMPERATURE`; a request without one runs at the provider's default, 1, so
is never cached), one choice, no tools, the tenant's `cache_enabled` set, and no
`X-Tollgate-Cache: bypass` header. The key is every parameter that can change the answer:
change any of them, even one Tollgate doesn't know, and it misses. Each tenant's entries are
its own. Only complete answers are kept, for 24 h by default (`EXACT_CACHE_TTL_S`), and a
plain answer serves streamed requests too, replayed as SSE, and the other way round.

A hit is admitted like any request, budget and rate limits included, but costs nothing
beyond its RPM slot: its token reservation is refunded, nothing is billed, and its usage
event records `cache_status = HIT` and what the answer would have cost (`saved_cost_usd`).
Responses say `X-Tollgate-Cache: HIT`, `MISS` or `BYPASS`. The cache adds no Redis call:
the admission call reads the entry, and the settle call stores one.

### Semantic cache

Off by default. With it on, a cacheable request that misses the exact layer can be
answered by meaning, in two stages:

1. Its last user message is embedded (`all-MiniLM-L6-v2`). The nearest cached question of
   the same tenant and scope (same model, system prompt, earlier turns and parameters) is a
   candidate if their similarity is at least 0.68.
2. An NLI cross-encoder (`nli-deberta-v3-xsmall`) reads both questions. It is a hit only if
   each question entails the other, with a probability of at least 0.90.

[B4](BENCHMARKS.md#b4-semantic-cache-evaluation) chose both models and thresholds: an
embedding model alone can't tell "JSON to YAML" from "YAML to JSON".

```sh
docker compose -f docker-compose.yml -f docker-compose.semantic.yml up -d --build
```

turns it on. The command:

- builds the gateway image with PyTorch (CPU) and both models in it, at the revisions B4
  measured;
- sets the six `SEMANTIC_CACHE_*` settings B4 chose (`.env.example`);
- raises each replica's memory limit to 2 GiB, since both models load into every gateway
  process. [B5](BENCHMARKS.md#b5-cache-impact) measured a 1,420 MiB peak, against about
  200 MiB without the layer.

Each model runs in a bounded thread pool of its own, off the event loop. A request that
finds a pool full, or whose model fails or takes too long, is a miss and stores nothing in
the semantic layer. A request never fails because of the cache.

**It is off by default because B5 found it over budget.** On B5's workload it adds 2.6
points of hit rate over the exact layer, 64.2% against 61.6%. The cost:
- **Miss path over target:** 91 to 103 ms at p99, against a 25 ms target.
- **Rejected candidates:** a quarter of semantic lookups pay for a rejected check, about
  140 ms at the median.
- **Other requests slowed:** the models throttle a 1-CPU replica, which slows requests the
  cache can't serve.

See [B5](BENCHMARKS.md#b5-cache-impact).

### Usage

Every request that reaches a provider produces exactly one usage event, whatever happens:
`success`, `provider_error`, `client_disconnect`, `gateway_shutdown` (cut off by a shutdown)
or `gateway_error` (an internal error), with whatever usage is known. After a failover the
one event covers every attempt (`attempts`), adding an estimate for a provider that was
sent the prompt and never answered, since it may bill it. See SPEC.md §4.
Internal errors return an OpenAI-shaped 500 that carries only the request ID; a request
cut off by a shutdown gets a 503 with `Retry-After`, which OpenAI SDKs retry.

## Quickstart

Needs Docker. The first `--build` downloads base images and installs dependencies, so it
takes longest; with the images built, these steps took 20 s from `up` to the response.

```sh
docker compose up --build -d
# Create a tenant and its first API key. The key is printed once; only its hash is stored.
docker compose exec gateway python scripts/create_tenant.py --name demo
export TOLLGATE_KEY=tg_live_...   # the api_key printed above
curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $TOLLGATE_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}'
```

The default stack routes every model to the mock providers (`gpt-4o-mini` fails over from
`mock_provider` to `mock_provider_secondary`), so no provider keys are needed,
and runs with `DEV_MODE=true` and development-only `KEY_PEPPER` and `ADMIN_TOKEN`
values (the gateway warns about this at startup). Without `DEV_MODE` it refuses to start
with those values. Anywhere but your own machine, copy `.env.example` to `.env`, set real
secrets there and `DEV_MODE=false`.

**`KEY_PEPPER` is part of every stored key hash: changing it invalidates every API key
ever issued.** Choose it once and keep it secret; rotating it means reissuing all keys.

Tenants and keys can also be managed over the admin API (`Authorization: Bearer
$ADMIN_TOKEN`): `POST /admin/tenants`, `PATCH /admin/tenants/{id}`,
`POST /admin/tenants/{id}/keys` and `DELETE /admin/keys/{id}`. A revoked key stops
working on every replica at once.

Each tenant has a requests-per-minute and a tokens-per-minute limit, both token buckets
in Redis that refill continuously. A chat request reserves its estimated prompt tokens
plus its completion cap (`max_completion_tokens`, else `max_tokens`, else
`TPM_DEFAULT_MAX_TOKENS`, 1024 by default). Once its usage is known, the difference is
refunded or charged. Responses carry `X-RateLimit-Limit-Requests`,
`X-RateLimit-Remaining-Requests` and `X-RateLimit-Remaining-Tokens`. A request over a
limit gets an OpenAI-shaped 429 (`rpm_exceeded` or `tpm_exceeded`) with `Retry-After`
taken from the bucket. A request whose reservation exceeds the whole TPM limit gets
`request_too_large`, without `Retry-After`. If Redis is down, requests are refused with
503 rather than let through. Each gateway opens its Redis connections at startup
(`REDIS_MIN_CONNECTIONS`, the whole pool by default), so a freshly started gateway can
take a burst; it still starts if Redis is down.

Every request's usage, with its cost from the prices in the providers config, is a row
in the `usage_events` table, written in batches every 500 ms or 200 events; shutdown
writes whatever is still queued. A tenant with a `monthly_budget_usd` is admitted while
its month-to-date spend (UTC) is below it, and refused with an OpenAI-shaped 402
(`budget_exceeded`) once it isn't. Requests already in flight when the budget is crossed
still finish, so spend can overshoot by their cost. Raising the budget takes effect at
once.

## Observability

Each gateway process serves Prometheus metrics at `/metrics`:

| Metric | Type | What |
|---|---|---|
| `tollgate_requests_total{status,model,provider,cache}` | counter | chat requests, by outcome: the usage status (`success`, `provider_error`, `client_disconnect`, `rate_limited`, `budget_exceeded`, ...) or, for requests turned away before that, the error code (`auth_unavailable`, `invalid_api_key`, ...) |
| `tollgate_request_duration_seconds` | histogram | arrival to the end of the response or stream |
| `tollgate_gateway_overhead_seconds` | histogram | time a request that reached a provider spent in the gateway, not waiting on the provider, until its response started |
| `tollgate_ttft_seconds` | histogram | streamed requests: arrival to first token |
| `tollgate_ratelimit_rejections_total{limit}` | counter | `rpm`, `tpm`, `request_too_large` |
| `tollgate_ledger_queue_depth` | gauge | usage events not yet written to Postgres |

Label values come from fixed sets (configured models and providers, known statuses);
anything else is `other`, so clients can't create new series. Metrics are per process:
scrape every replica directly (nginx refuses `/metrics`), and run one uvicorn worker per
replica (`WEB_CONCURRENCY=1`), scaling with replicas.

Logs are one JSON object per line on stderr (`ts`, `level`, `logger`, `message`,
`request_id`), uvicorn's included, every line logged during a request carrying its
`X-Request-ID`. `LOG_LEVEL` sets the level for all of them. Prompts and keys are never
logged, and the access log drops query strings.

To run three gateway replicas behind nginx, sharing Postgres and Redis:
`docker compose -f docker-compose.yml -f docker-compose.scale.yml up -d`.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="tg_live_...")
client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

for chunk in client.chat.completions.create(
    model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], stream=True
):
    print(chunk.choices[0].delta.content or "", end="")
```

## Performance

Measured against the mock provider on a 4-core laptop (Intel i7-1165G7), each gateway
replica limited to one CPU; medians of 3 runs. [BENCHMARKS.md](BENCHMARKS.md) has the
environment, method, ranges across runs and the raw k6 output.

| | Result |
|---|---|
| Gateway overhead at 100 RPS, non-streamed | 2.61 ms p50, 13.03 ms p99 |
| Added time to first chunk at 100 RPS, streamed | 1.13 ms p50, 10.56 ms p99 |
| Max sustained throughput (p99 overhead ≤ 50 ms) | 250 RPS with 1 replica, 275 RPS with 3 |

At 100 RPS both p99s are within SPEC's 20 ms target. BENCHMARKS.md also has:
- **B3**, failover with the primary killed: no request failed in 5 runs at 200 RPS and 1 at
  300. Until the primary's breaker opens, about 6 s after the kill, each request waits out the
  1.1 s connect timeout, then succeeds on the secondary.
- **B5**, the cache's impact: the semantic layer adds 2.6 points of hit rate on B5's workload,
  but its miss path is over its 25 ms budget, so it stays off by default. One replica can't sustain SPEC's
500 RPS on this machine, and 3 replicas barely outscale one here. BENCHMARKS.md has the
details, including how a cheaper healthcheck took the p99s down from 43 ms and 142 ms.

## Mock provider

`mock_provider/` is a fake OpenAI-compatible server used by all tests and benchmarks.
Behaviour is set by env var at startup or per request with headers (`X-Mock-Latency-Ms`,
`X-Mock-TTFT-Ms`, `X-Mock-Tokens-Per-Sec`, `X-Mock-Fail-Rate`, `X-Mock-Fail-Mode`,
`X-Mock-Output-Tokens`, `X-Mock-Seed`). The same seed always produces the same output.
`PUT /mock/behaviour` changes its defaults while it runs, which is how the chaos tests
take one provider down and bring it back. `GET /mock/stats` reports the tokens it served.
See the docstring in [mock_provider/app.py](mock_provider/app.py) for details.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pytest --cov                  # everything, including perf checks
pytest -m "not perf" --cov    # what CI runs: perf checks depend on the machine
ruff check . && ruff format --check . && mypy
```

Integration tests start throwaway Postgres and Redis containers, so they need Docker.

The chaos tests (SPEC §8: X1–X4) run against the compose stack, and only when asked for:
`docker compose up -d --build`, then `pytest tests/chaos`. They take a mock provider down
and restart Redis, so nothing else should be using the stack; X2 waits out a 30 s
breaker cooldown.

`bash bench/run_all.sh` runs the benchmarks (about an hour) in a separate compose project
with fixed CPU and memory limits, and writes the raw k6 output to `bench/results/`;
`python bench/report.py bench/results/<timestamp>` turns it into BENCHMARKS.md's tables.
`bash bench/run_v2.sh` runs B5 and B3 the same way (about 75 minutes), and
`python bench/v2_report.py bench/results/v2-<timestamp>` makes their tables. B5 first needs
its workload's text, downloaded and checked against committed checksums:
`python bench/workloads/make_workload.py --text`.

With `SERVER_TIMING=true`, responses carry a `Server-Timing` header breaking down the
gateway's time per stage (`auth`, `body`, `upstream-*`, plus `other`, `gateway` and
`total`); the perf test prints it.
