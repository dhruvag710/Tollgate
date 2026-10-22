# Tollgate

A self-hosted, OpenAI-compatible LLM gateway. Point your OpenAI SDK at Tollgate by
changing only `base_url`. See [SPEC.md](SPEC.md) for the full design.

**Status:** phases 1.1 (skeleton + non-streaming passthrough) and 1.2 (mock provider).
Auth, rate limiting, streaming and metering come in later phases.

## Quickstart

```sh
docker compose up --build
curl -s localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}'
```

The default stack routes every model to `mock_provider`, so no real keys are needed.
To use real providers, copy `.env.example` to `.env`, set
`PROVIDERS_CONFIG=config/providers.yaml` and fill in the keys.

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="anything")
client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
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
pytest --cov
ruff check . && ruff format --check . && mypy
```
