# Tollgate — Project Specification

> A self-hosted, OpenAI-compatible LLM gateway with multi-tenant auth, atomic rate limiting, usage metering, provider failover, and a semantic cache.

Apps point their OpenAI SDK at Tollgate instead of a provider by changing only the `base_url`. Tollgate authenticates the tenant, enforces limits and budgets, serves from cache when safe, routes to a healthy provider, streams the response back, and records exactly what it cost.

---

## 0. Ground rules

1. **Every number on the resume comes from `BENCHMARKS.md`.** Every number in `BENCHMARKS.md` comes from a script in `bench/`, with the hardware and workload stated next to it. Targets in this spec are *goals*, not results.
2. **Tests and load tests never hit real providers.** They use `mock_provider/`. Real keys are only for manual smoke tests.
3. **Build in milestone order.** A milestone is done only when all its acceptance criteria pass in CI.

---

## 1. Goals and non-goals

**Goals**
- Drop-in OpenAI compatibility for `/v1/chat/completions` (streaming and non-streaming)
- Correct rate limiting under concurrency, across multiple gateway replicas
- Accurate per-tenant usage and cost accounting, with no lost events
- Graceful handling of provider failures (timeouts, 5xx, 429s)
- A semantic cache whose false-hit rate is measured, not assumed
- Low, measured overhead added by the gateway

**Non-goals**
- Not a production SaaS. No signup flow, payments, or multi-region.
- No embeddings, images, or audio endpoints in v1/v2. Chat completions only.
- No custom model hosting. Local models are reached via Ollama's OpenAI-compatible API.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | Python 3.12, FastAPI, Uvicorn | Async I/O for streaming, familiar |
| HTTP client | `httpx.AsyncClient` (pooled) | Streaming support, connection reuse |
| DB | PostgreSQL 16 + pgvector | Tenants, keys, usage ledger, cache vectors |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic | |
| Cache / limits | Redis 7 | Lua scripts for atomic limiting, key cache, breaker state, streams |
| Token counting | `tiktoken` | Prompt token estimation for TPM reservations |
| Embeddings (v2) | `sentence-transformers`, small model chosen by benchmark | |
| Testing | pytest, pytest-asyncio, httpx, testcontainers | Real Postgres/Redis in tests |
| Load testing | k6 | Scriptable, accurate latency percentiles |
| Observability | prometheus-client, structlog (JSON logs) | |
| Infra | Docker Compose, nginx (multi-replica tests), GitHub Actions | |

---

## 3. Repository structure

```
tollgate/
├── CLAUDE.md                      # instructions for Claude Code
├── SPEC.md                        # this file
├── README.md                      # pitch, architecture diagram, quickstart, headline numbers
├── BENCHMARKS.md                  # measured results only, with hardware + workload
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml             # gateway, postgres, redis, mock_provider
├── docker-compose.scale.yml       # 3 gateway replicas behind nginx
├── nginx/nginx.conf
├── .env.example
├── config/
│   ├── providers.yaml             # base URLs, models, prices, timeouts
│   └── providers.test.yaml        # everything points at mock_provider
├── migrations/                    # alembic
│   └── versions/
├── src/tollgate/
│   ├── main.py                    # app factory, lifespan (pools, clients)
│   ├── config.py                  # pydantic-settings
│   ├── deps.py                    # FastAPI dependencies
│   ├── errors.py                  # OpenAI-style error responses
│   ├── api/
│   │   ├── v1/chat.py             # POST /v1/chat/completions
│   │   ├── v1/models.py           # GET /v1/models
│   │   ├── admin/tenants.py
│   │   ├── admin/keys.py
│   │   ├── admin/usage.py
│   │   └── health.py              # /healthz, /readyz, /metrics
│   ├── auth/
│   │   ├── keys.py                # generation, hashing, verification
│   │   └── dependency.py          # resolves API key -> TenantContext
│   ├── ratelimit/
│   │   ├── lua/
│   │   │   ├── token_bucket.lua
│   │   │   ├── sliding_window.lua     # v2
│   │   │   ├── tpm_reserve.lua
│   │   │   └── tpm_reconcile.lua
│   │   ├── limiter.py             # loads scripts (EVALSHA), exposes check()
│   │   └── estimate.py            # prompt token estimation
│   ├── providers/
│   │   ├── base.py                # Provider protocol
│   │   ├── openai_compat.py       # OpenAI, OpenRouter, Ollama, mock
│   │   ├── gemini.py              # v2 (via OpenAI-compatible endpoint or adapter)
│   │   └── registry.py            # builds providers from YAML
│   ├── routing/
│   │   ├── router.py              # v1: static; v2: priority + failover
│   │   └── circuit_breaker.py     # v2, state in Redis
│   ├── cache/                     # v2
│   │   ├── exact.py               # hash-based exact-match layer
│   │   ├── semantic.py            # pgvector similarity layer
│   │   ├── embedder.py
│   │   ├── policy.py              # what is cacheable
│   │   └── replay.py              # cached text -> SSE chunks
│   ├── usage/
│   │   ├── pricing.py
│   │   ├── ledger.py              # v1: batched background writer
│   │   ├── stream_consumer.py     # v2: Redis Streams consumer
│   │   └── budget.py
│   ├── streaming/sse.py           # passthrough, usage capture, disconnect handling
│   ├── db/
│   │   ├── models.py
│   │   └── session.py
│   └── observability/
│       ├── metrics.py
│       └── logging.py
├── mock_provider/
│   ├── app.py                     # fake OpenAI-compatible server
│   └── Dockerfile
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   ├── concurrency/
│   └── chaos/
├── bench/
│   ├── k6/
│   │   ├── overhead.js
│   │   ├── ratelimit_storm.js
│   │   ├── cache_workload.js      # v2
│   │   └── failover.js            # v2
│   ├── cache_eval/                # v2
│   │   ├── build_pairs.py
│   │   ├── adversarial_pairs.jsonl
│   │   ├── eval_threshold.py
│   │   └── compare_embedders.py
│   ├── workloads/make_workload.py
│   ├── run_all.sh
│   └── results/                   # raw JSON outputs, committed
├── scripts/
│   ├── create_tenant.py
│   └── seed_dev.py
└── .github/workflows/ci.yml
```

---

## 4. Request lifecycle

```
client
  │  POST /v1/chat/completions   (Authorization: Bearer tg_live_...)
  ▼
[1] request_id assigned, timer started
[2] Auth: hash key -> Redis key-cache (TTL 60s) -> fallback Postgres -> TenantContext
[3] Budget check: month-to-date spend < monthly budget          (v1)
[4] Rate limit (one Lua call): RPM token bucket + TPM reservation
        └─ reject -> 429 + Retry-After + X-RateLimit-* headers
[5] Cache lookup (v2): exact layer, then semantic layer
        └─ HIT -> return cached response (replayed as SSE if stream=true)
[6] Route: pick provider by model + priority, skip OPEN breakers (v2)
[7] Call provider; stream chunks straight through
        └─ failure BEFORE first byte -> retry on next provider (v2)
        └─ failure AFTER first byte  -> terminate stream with error event (no retry)
[8] On completion: actual usage -> reconcile TPM reservation
[9] Emit usage event (async) -> ledger
[10] Store in cache if cacheable (v2)
```

**Response headers added:** `X-Request-ID`, `X-Tollgate-Provider`, `X-Tollgate-Cache: HIT|MISS|BYPASS`, `X-RateLimit-Limit-Requests`, `X-RateLimit-Remaining-Requests`, `X-RateLimit-Remaining-Tokens`, `Retry-After` (on 429).

**Error format:** always the OpenAI shape, so SDKs parse it:
```json
{"error": {"message": "...", "type": "rate_limit_exceeded", "code": "rpm_exceeded"}}
```

---

## 5. Data model

### 5.1 PostgreSQL

```sql
CREATE TABLE tenants (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                  TEXT NOT NULL UNIQUE,
  rpm_limit             INT  NOT NULL DEFAULT 60,
  tpm_limit             INT  NOT NULL DEFAULT 100000,
  monthly_budget_usd    NUMERIC(12,4),            -- NULL = unlimited
  allowed_models        TEXT[],                   -- NULL = all
  cache_enabled         BOOLEAN NOT NULL DEFAULT TRUE,   -- v2
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  key_prefix  TEXT NOT NULL,                      -- e.g. "tg_live_ab12cd"
  key_hash    TEXT NOT NULL UNIQUE,               -- HMAC-SHA256(pepper, raw_key)
  name        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at TIMESTAMPTZ,
  revoked_at  TIMESTAMPTZ
);

CREATE TABLE usage_events (
  id                 BIGSERIAL PRIMARY KEY,
  request_id         UUID NOT NULL UNIQUE,        -- idempotent inserts
  tenant_id          UUID NOT NULL,
  api_key_id         UUID NOT NULL,
  provider           TEXT,
  model              TEXT NOT NULL,
  prompt_tokens      INT  NOT NULL DEFAULT 0,
  completion_tokens  INT  NOT NULL DEFAULT 0,
  cost_usd           NUMERIC(12,6) NOT NULL DEFAULT 0,
  saved_cost_usd     NUMERIC(12,6) NOT NULL DEFAULT 0,   -- v2: cache hits
  cache_status       TEXT NOT NULL DEFAULT 'MISS',       -- HIT | MISS | BYPASS
  status             TEXT NOT NULL,              -- success | provider_error | client_disconnect | rate_limited
  attempts           INT  NOT NULL DEFAULT 1,
  latency_ms         INT,
  ttft_ms            INT,                        -- time to first token
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON usage_events (tenant_id, created_at);

-- v2
CREATE TABLE cache_entries (
  id              BIGSERIAL PRIMARY KEY,
  tenant_id       UUID NOT NULL,
  scope_hash      TEXT NOT NULL,     -- hash(model, system prompt, prior turns, params)
  query_text      TEXT NOT NULL,     -- last user message
  embedding       VECTOR(384) NOT NULL,
  response_json   JSONB NOT NULL,
  completion_tokens INT NOT NULL,
  hit_count       INT NOT NULL DEFAULT 0,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX ON cache_entries USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON cache_entries (tenant_id, scope_hash);
```

### 5.2 Redis keys

| Key | Type | Purpose | TTL |
|---|---|---|---|
| `tg:key:{key_hash}` | hash | cached TenantContext | 60s, deleted on revoke |
| `tg:rl:rpm:{tenant_id}` | hash `{tokens, ts}` | token bucket | 2× refill window |
| `tg:rl:tpm:{tenant_id}` | hash `{tokens, ts}` | token budget bucket | 2× window |
| `tg:rl:sw:{tenant_id}` | zset | sliding window log (v2 comparison) | window |
| `tg:spend:{tenant_id}:{yyyymm}` | string (float) | month-to-date spend | 40 days |
| `tg:cb:{provider}` | hash `{state, failures, opened_at}` | circuit breaker (v2) | none |
| `tg:exact:{tenant_id}:{hash}` | string | exact-match cache (v2) | entry TTL |
| `tg:usage` | stream | usage events (v2) | trimmed by MAXLEN |

**Use server time inside Lua (`redis.call('TIME')`),** never the app's clock. Clock skew between replicas will otherwise break the limiter.

### 5.3 `config/providers.yaml`

```yaml
providers:
  - name: openai
    type: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    timeout_s: 60
    connect_timeout_s: 3
  - name: openrouter
    type: openai_compat
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY
  - name: ollama
    type: openai_compat
    base_url: http://ollama:11434/v1

models:
  gpt-4o-mini:
    routes: [openai, openrouter]          # priority order (failover in v2)
    price_per_1m: {input: 0.15, output: 0.60}
  llama3.1:8b:
    routes: [ollama]
    price_per_1m: {input: 0, output: 0}
```

Prices are config values. Verify them against provider pricing pages before reporting cost numbers.

---

## 6. API

### Public (tenant API key)

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI-compatible; `stream`, `stream_options.include_usage`, `temperature`, `max_tokens` |
| GET | `/v1/models` | Models allowed for this tenant |

### Admin (`Authorization: Bearer $ADMIN_TOKEN`)

| Method | Path | Notes |
|---|---|---|
| POST | `/admin/tenants` | Create with limits and budget |
| PATCH | `/admin/tenants/{id}` | Update limits (invalidates key cache) |
| POST | `/admin/tenants/{id}/keys` | Returns the raw key **once** |
| DELETE | `/admin/keys/{id}` | Revoke; must take effect within 1s |
| GET | `/admin/tenants/{id}/usage?from=&to=&group_by=day\|model` | Tokens, cost, saved cost, cache hit rate |
| GET | `/admin/providers` | Breaker states (v2) |

### Ops

`GET /healthz` (process alive), `GET /readyz` (Postgres + Redis reachable), `GET /metrics` (Prometheus).

---

## 7. Milestones

### v1 — Core gateway

**Phase 1.1: Skeleton and passthrough**
- App factory, settings, pooled `httpx.AsyncClient` created in lifespan
- Docker Compose: gateway, postgres, redis, mock_provider
- Non-streaming passthrough to one provider
- ✅ Accept: the official `openai` Python SDK with only `base_url` changed gets a valid response

**Phase 1.2: Mock provider** (build this early; everything else is tested against it)
- OpenAI-compatible `/v1/chat/completions` and `/v1/models`
- Configurable via env and per-request headers: `X-Mock-Latency-Ms`, `X-Mock-TTFT-Ms`, `X-Mock-Tokens-Per-Sec`, `X-Mock-Fail-Rate`, `X-Mock-Fail-Mode` (`500|429|timeout|drop_mid_stream`), `X-Mock-Output-Tokens`
- Returns accurate `usage` objects, including in the final stream chunk
- ✅ Accept: deterministic output when given a seed

**Phase 1.3: Streaming**
- True passthrough: forward each SSE chunk as it arrives (no buffering)
- Inject `stream_options.include_usage = true` upstream; capture usage from the final chunk
- Detect client disconnect, cancel the upstream request, and record `client_disconnect` with partial usage
- ✅ Accept: TTFT via gateway is within the overhead budget of TTFT direct to mock; a disconnect test shows the upstream request is cancelled

**Phase 1.4: Tenants and keys**
- Key format `tg_live_` + 32 bytes base62; store `HMAC-SHA256(pepper, key)`; compare in constant time
- Redis key cache with 60s TTL, explicitly deleted on revoke or limit change
- Admin endpoints and `scripts/create_tenant.py`
- ✅ Accept: revoked key is rejected within 1s; Postgres is not queried on cache-hit auth (assert via query counter)

**Phase 1.5: Rate limiting**
- `token_bucket.lua` for RPM. `tpm_reserve.lua` reserves `estimated_prompt_tokens + max_tokens` (with a default cap if `max_tokens` is absent). RPM and TPM checks in **one** Lua call, so a request never consumes one limit while failing the other.
- `tpm_reconcile.lua` refunds or charges the difference once actual usage is known
- 429 with `Retry-After` computed from bucket state
- ✅ Accept: see concurrency tests C1–C3 below

**Phase 1.6: Usage ledger and budgets**
- Usage events go to an in-process queue; a background task batch-inserts every 500ms or 200 events
- `INSERT ... ON CONFLICT (request_id) DO NOTHING`
- Month-to-date spend kept in Redis and checked before the call; requests are rejected with 402-style error once over budget
- Graceful shutdown flushes the queue
- ✅ Accept: after a load test, `SUM(usage_events)` matches the mock provider's own served-token counter exactly

**Phase 1.7: Observability and multi-replica**
- Prometheus metrics: `tollgate_requests_total{status,model,provider,cache}`, `tollgate_request_duration_seconds` histogram, `tollgate_gateway_overhead_seconds` histogram, `tollgate_ttft_seconds`, `tollgate_ratelimit_rejections_total`, `tollgate_ledger_queue_depth`
- JSON logs with `request_id`; prompts are **not** logged by default
- `docker-compose.scale.yml`: 3 replicas behind nginx
- ✅ Accept: C2 passes against 3 replicas

**v1 benchmarks:** B1 and B2 (section 9).

---

### v2 — Resilience and caching

**Phase 2.1: Failover and circuit breakers**
- Router tries providers in configured priority order
- Per-provider breaker in Redis (shared across replicas): CLOSED → OPEN after N failures in a window → HALF_OPEN after cooldown → one probe request → CLOSED or back to OPEN
- Retry on the next provider **only if no bytes have been sent to the client**
- Retry on: connect error, timeout before first byte, 5xx, upstream 429. Never retry 4xx client errors.
- `attempts` recorded in the usage event
- ✅ Accept: see chaos tests X1–X3

**Phase 2.2: Durable usage pipeline**
- Replace the in-process queue with a Redis Stream (`XADD`) and a consumer group worker (`XREADGROUP` → batch insert → `XACK`)
- Pending entries are reclaimed (`XAUTOCLAIM`) if a worker dies
- ✅ Accept: kill the consumer mid-load-test, restart it, and the ledger still matches mock counters exactly (zero lost, zero duplicated)

**Phase 2.3: Exact-match cache**
- Cache key = hash of (tenant, model, full messages, temperature, max_tokens, tools)
- ✅ Accept: identical requests hit; changing any param misses

**Phase 2.4: Semantic cache**
- **Cacheability policy** (`policy.py`): only when `temperature <= 0.3` (configurable), no tools/function calls, tenant has cache enabled, and the client didn't send `X-Tollgate-Cache: bypass`
- **Scope:** `scope_hash` = hash(model, system prompt, all messages except the last user message, params). Only the **last user message** is embedded. Entries only match within the same scope and tenant. This prevents "same question, different conversation" false hits.
- Lookup: top-1 by cosine similarity within scope; hit if `similarity >= threshold`
- Threshold is chosen from the cache evaluation (section 9, B4), not by vibes
- Stream replay: cached response re-emitted as SSE chunks so streaming clients work unchanged
- TTL per entry (default 24h); usage event records `saved_cost_usd`
- ✅ Accept: cache lookup p99 meets target; false-hit rate at chosen threshold meets target on the eval set

**Phase 2.5: Sliding window limiter (comparison)**
- Implement `sliding_window.lua` (sorted-set log), selectable per tenant
- Benchmark against token bucket: Redis memory per tenant, latency, burst behavior at window boundaries
- ✅ Accept: comparison table in `BENCHMARKS.md`

**v2 benchmarks:** B3, B4, B5.

---

### v3 — Polish (optional)

- Complexity-based routing: a simple classifier or heuristic sends easy prompts to a cheap model. Measure cost saved vs. quality loss on a small labeled set with an LLM-as-judge, and report both numbers or skip the feature.
- Grafana dashboard provisioned in Compose
- Per-tenant monthly invoice export (CSV/PDF) from the ledger
- OpenTelemetry tracing across gateway → provider
- Small admin web UI

---

## 8. Testing strategy

**Target:** ≥ 85% line coverage on `src/tollgate/`, and all of the named tests below passing in CI.

### Unit (`tests/unit/`)
- Key generation, hashing, constant-time verification
- Token estimation vs. `tiktoken` reference
- Pricing math (including zero-price local models)
- Cacheability policy and scope hashing
- Circuit breaker state transitions (time mocked)
- SSE chunk parsing and replay

### Integration (`tests/integration/`): real Postgres + Redis via testcontainers, mock provider in-process
- OpenAI SDK compatibility: sync, async, streaming, error parsing
- Auth: missing / invalid / revoked key; key cache invalidation
- 429 responses contain correct headers
- Budget exhaustion
- Usage event content for success, provider error, and client disconnect
- Admin API CRUD

### Concurrency (`tests/concurrency/`)
- **C1** Single-instance exactness: tenant `rpm_limit=100`, fire 500 concurrent requests → exactly 100 succeed, 400 get 429
- **C2** Multi-replica exactness: same as C1, spread across 3 replicas → exactly 100 succeed
- **C3** TPM reservation: concurrent requests whose reserved tokens sum past the TPM limit → over-limit requests rejected; after reconciliation, bucket balance equals `limit - actual_used`
- **C4** Ledger accuracy: 10,000 requests → ledger totals equal mock provider counters exactly

### Chaos (`tests/chaos/`)
- **X1** Primary returns 500 for every request → all requests succeed via secondary; breaker opens after N failures
- **X2** Primary recovers → breaker goes HALF_OPEN → CLOSED; traffic returns to primary
- **X3** Provider drops connection mid-stream → client receives a terminating error event; no retry; usage records partial tokens
- **X4** Redis restarted during load → gateway fails closed with 503 (not open), recovers without restart
- **X5** Ledger consumer killed mid-run → zero lost / duplicated events after restart (v2)

### CI (`.github/workflows/ci.yml`)
Lint (ruff), type check (mypy), unit + integration + concurrency tests, coverage report. Chaos and load tests run locally via `bench/run_all.sh`.

---

## 9. Benchmarks and target numbers

**Rules:** run against `mock_provider`, never real APIs. Record the machine (CPU, RAM, OS), Docker resource limits, gateway worker count, and k6 parameters in every result. Raw k6 JSON goes in `bench/results/`. Report p50, p95, and p99, not just averages.

| ID | Benchmark | How | Target (goal) |
|---|---|---|---|
| **B1** | Gateway overhead | Same k6 load straight to mock vs. through gateway (cache off); overhead = difference in latency | p50 < 5 ms, p99 < 20 ms at 500 RPS |
| **B1b** | Streaming TTFT overhead | Same, measuring time to first chunk | p99 < 20 ms added |
| **B2** | Throughput ceiling | Ramp RPS until p99 overhead > 50 ms or errors > 0.1% | Report max sustained RPS (1 and 3 replicas) |
| **B3** | Failover | Kill primary during a 300 RPS run | Error rate during failover < 1%; time to steady state reported |
| **B4** | Semantic cache accuracy | See below | False-hit rate ≤ 2% at chosen threshold; report recall at that threshold |
| **B5** | Cache impact | Replay a realistic workload with cache on vs. off | Report hit rate, $ saved, p50 latency on hits; cache lookup p99 < 25 ms |
| **B6** | Limiter comparison | Token bucket vs. sliding window | Table of memory/tenant, p99 latency, boundary burst |

### B4: Semantic cache evaluation (the most important benchmark)

**Datasets**
1. **Quora Question Pairs (QQP):** labeled duplicate / non-duplicate question pairs. Sample 5,000 balanced pairs.
2. **Adversarial near-misses** (`adversarial_pairs.jsonl`, ~200 hand-written): pairs that are lexically similar but need different answers. Examples: "What's 2+2?" vs. "What's 2+3?", "capital of Austria" vs. "capital of Australia", "how to start a Docker container" vs. "how to stop a Docker container", negations, different numbers, different units, different years.

**Procedure** (`eval_threshold.py`)
- Embed both sides of each pair, compute cosine similarity
- Sweep thresholds 0.80 → 0.99 in 0.01 steps
- At each threshold: false-hit rate (non-duplicates that would hit) and recall (duplicates that would hit), reported separately for QQP and adversarial sets
- Choose the lowest threshold where false-hit rate ≤ 2% on **both** sets
- Plot the curves; commit the chart

**Embedding model comparison** (`compare_embedders.py`)

| Model | Dim |
|---|---|
| BAAI/bge-small-en-v1.5 | 384 |
| sentence-transformers/all-MiniLM-L6-v2 | 384 |
| intfloat/e5-small-v2 | 384 |
| thenlper/gte-small | 384 |

Compare on recall at 2% false-hit rate, CPU embed latency (p50/p99 per query), and memory. The winner becomes the default. This is the "I picked the embedding model from my own benchmarks" story, so keep the table.

### B5: Realistic workload

`make_workload.py` builds a 20,000-request stream:
- Base queries sampled from MS MARCO queries with a Zipf distribution (s ≈ 1.1), so popular questions repeat like real traffic
- 15% of repeats replaced with a QQP paraphrase of the original
- 5% adversarial near-misses mixed in

State this workload alongside every hit-rate and cost number. Hit rate on a different workload will differ, and the README should say so.

---

## 10. Security and correctness notes

- Never log raw API keys, provider keys, or prompts by default (`LOG_PROMPTS=false`)
- Provider API keys come only from env, never from the DB or config files
- Admin endpoints require a separate admin token
- Rate limiting and auth fail **closed** if Redis is down (503), not open
- Request body size limit (e.g. 1 MB)
- Upstream timeouts: connect 3s, first byte configurable, total configurable

---

## 11. Definition of done (per milestone)

- [ ] All acceptance criteria for every phase pass
- [ ] Named tests (C*, X*) pass
- [ ] Coverage ≥ 85%
- [ ] Benchmarks for the milestone run and recorded in `BENCHMARKS.md` with hardware + workload
- [ ] README updated: architecture diagram, quickstart (`docker compose up` → working curl in under 5 minutes), headline numbers linked to `BENCHMARKS.md`

---

## 12. Resume bullet templates

Fill these in **only** with numbers from `BENCHMARKS.md`:

- Built **Tollgate**, an OpenAI-compatible LLM gateway (FastAPI, PostgreSQL, Redis) with multi-tenant API keys, SSE streaming passthrough, and per-tenant usage metering, adding p99 overhead of __ ms at __ RPS.
- Implemented atomic RPM/TPM rate limiting with Redis Lua scripts, enforcing exact limits under __ concurrent requests across 3 gateway replicas.
- Added provider failover with Redis-backed circuit breakers, keeping error rate under __% during a simulated provider outage.
- Designed a scoped semantic cache on pgvector; benchmarked 4 embedding models on __k labeled pairs plus adversarial near-misses to select a threshold with ≤ __% false-hit rate, saving __% of LLM spend at __% hit rate on a Zipf-distributed workload.
- Built a durable usage pipeline on Redis Streams with idempotent inserts, verified to lose zero events under consumer crashes across __k requests.
