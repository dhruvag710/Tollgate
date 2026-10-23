# Tollgate

A self-hosted, OpenAI-compatible LLM gateway. Point your OpenAI SDK at Tollgate by
changing only `base_url`. See [SPEC.md](SPEC.md) for the full design.

**Status:** phases 1.1–1.4: passthrough for streaming and non-streaming chat
completions, the mock provider, and tenants with API keys. Rate limiting and the
persistent usage ledger come in later phases; until then each request's usage event is
written to the log.

Streaming is a true passthrough: each SSE event is forwarded as it arrives. Tollgate
always asks the provider for the final usage chunk, but only forwards it if the client
asked for it too. If the client disconnects, the upstream request is cancelled and the
partial usage is recorded, estimated with tiktoken. An upstream failure after the first
event ends the stream with an OpenAI-style error event; a failure before it is a normal
HTTP error.

Every request that reaches a provider produces exactly one usage event, whatever happens:
`success`, `provider_error`, `client_disconnect`, `gateway_shutdown` (cut off by a shutdown)
or `gateway_error` (an internal error), with whatever usage is known. See SPEC.md §4.
Internal errors return an OpenAI-shaped 500 that carries only the request ID; a request
cut off by a shutdown gets a 503 with `Retry-After`, which OpenAI SDKs retry.

## Quickstart

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

The default stack routes every model to `mock_provider`, so no provider keys are needed,
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

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="tg_live_...")
client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])

for chunk in client.chat.completions.create(
    model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}], stream=True
):
    print(chunk.choices[0].delta.content or "", end="")
```

## Mock provider

`mock_provider/` is a fake OpenAI-compatible server used by all tests and benchmarks.
Behaviour is set by env var at startup or per request with headers (`X-Mock-Latency-Ms`,
`X-Mock-TTFT-Ms`, `X-Mock-Tokens-Per-Sec`, `X-Mock-Fail-Rate`, `X-Mock-Fail-Mode`,
`X-Mock-Output-Tokens`, `X-Mock-Seed`). The same seed always produces the same output.
`GET /mock/stats` reports the tokens it served. See the docstring in
[mock_provider/app.py](mock_provider/app.py) for details.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pytest --cov                  # everything, including perf checks
pytest -m "not perf" --cov    # what CI runs: perf checks depend on the machine
ruff check . && ruff format --check . && mypy
```

Integration tests start throwaway Postgres and Redis containers, so they need Docker.

With `SERVER_TIMING=true`, responses carry a `Server-Timing` header breaking down the
gateway's time per stage (`auth`, `body`, `upstream-*`, plus `other`, `gateway` and
`total`); the perf test prints it.
