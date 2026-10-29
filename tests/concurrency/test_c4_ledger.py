"""C4 (SPEC §8): 10,000 requests through a gateway into the Postgres ledger;
ledger totals equal the mock provider's own counters exactly, for every status
but client_disconnect. Disconnected streams match their prompt tokens exactly,
and fall short on completion tokens by at most the tokens in flight.

The two kinds of traffic go to two mock processes (two models), so each
ledger total is compared with the counters of the mock that served it alone:
the disconnects' bounded gap can't hide an error in the rest.
"""

import asyncio
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml
from redis.asyncio import Redis

from tests.helpers import (
    in_flight_bound,
    issue_key,
    ledger_rows,
    redis_month,
    run_uvicorn_process,
)
from tollgate.usage.budget import spend_key
from tollgate.usage.pricing import micro_usd

N = 10_000
CONCURRENCY = 64
EXACT, DISCONNECTED = "gpt-4o-mini", "llama3.1:8b"
DISCONNECT_TOKENS_PER_SEC = 100
FAIL, MODE = "X-Mock-Fail-Rate", "X-Mock-Fail-Mode"

# Per 100 requests: (count, stream, mock headers, status the ledger records).
MIX: list[tuple[int, bool, dict[str, str], str]] = [
    (55, False, {}, "success"),
    (20, True, {}, "success"),
    (5, False, {FAIL: "1", MODE: "500"}, "provider_error"),  # error responses: nothing billed
    (3, True, {FAIL: "1", MODE: "429"}, "provider_error"),
    (2, False, {FAIL: "1", MODE: "timeout"}, "provider_error"),  # sent, then cut off
    (1, True, {FAIL: "1", MODE: "timeout"}, "provider_error"),
    (2, False, {FAIL: "1", MODE: "drop_mid_stream"}, "provider_error"),  # response broken
    (3, True, {FAIL: "1", MODE: "drop_mid_stream"}, "provider_error"),
]
DISCONNECTS_PER_100 = 100 - sum(count for count, *_ in MIX)  # 9


def providers_config(tmp_path: Path, exact_url: str, disconnect_url: str) -> Path:
    config = {
        "providers": [
            # A short read timeout, so a hung provider cuts requests off quickly.
            {"name": "mock-a", "type": "openai_compat", "base_url": f"{exact_url}/v1",
             "timeout_s": 0.5, "forward_mock_headers": True},
            {"name": "mock-b", "type": "openai_compat", "base_url": f"{disconnect_url}/v1",
             "forward_mock_headers": True},
        ],
        "models": {
            EXACT: {"routes": ["mock-a"], "price_per_1m": {"input": 0.15, "output": 0.6}},
            DISCONNECTED: {"routes": ["mock-b"], "price_per_1m": {"input": 0.05, "output": 0.1}},
        },
    }  # fmt: skip
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def stats(mock_url: str) -> dict[str, int]:
    stats: dict[str, int] = httpx.get(f"{mock_url}/mock/stats").json()
    return stats


async def send(client: httpx.AsyncClient, i: int) -> None:
    """Request `i` of the run: its kind by its position in each hundred."""
    slot, messages = i % 100, [{"role": "user", "content": "count the tokens " * (1 + i % 7)}]
    for count, stream, headers, _ in MIX:
        if slot < count:
            body = {"model": EXACT, "messages": messages, "stream": stream}
            headers = {"X-Mock-Output-Tokens": str(5 + i % 20), **headers}
            if stream:
                body["stream_options"] = {"include_usage": i % 2 == 0}
                async with client.stream("POST", "/v1/chat/completions", json=body,
                                         headers=headers) as r:  # fmt: skip
                    await r.aread()
            else:
                await client.post("/v1/chat/completions", json=body, headers=headers)
            return
        slot -= count
    # A client that hangs up mid-stream, three tokens in.
    body = {"model": DISCONNECTED, "messages": messages, "stream": True}
    headers = {
        "X-Mock-Output-Tokens": "50",
        "X-Mock-Tokens-Per-Sec": str(DISCONNECT_TOKENS_PER_SEC),
    }
    async with client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as r:
        tokens = 0
        async for line in r.aiter_lines():
            tokens += line.startswith("data: ") and '"content"' in line
            if tokens == 3:
                break


def totals(rows: list[Any], model: str) -> tuple[int, int]:
    mine = [row for row in rows if row.model == model]
    return sum(r.prompt_tokens for r in mine), sum(r.completion_tokens for r in mine)


async def test_c4_ledger_totals_equal_the_providers_counters(tmp_path: Path) -> None:
    issued = await issue_key(
        rpm_limit=10_000_000, tpm_limit=1_000_000_000, monthly_budget_usd=Decimal("1000")
    )
    tenant_id = issued.key.tenant_id
    with (
        run_uvicorn_process("mock_provider.app:app", env={"MOCK_HANG_S": "30"}) as exact_url,
        run_uvicorn_process("mock_provider.app:app") as disconnect_url,
    ):
        env = {
            "PROVIDERS_CONFIG": str(providers_config(tmp_path, exact_url, disconnect_url)),
            "LOG_LEVEL": "ERROR",  # timeouts and drops each log a warning
        }
        with run_uvicorn_process("--factory", "tollgate.main:create_app", env=env) as url:
            limits = httpx.Limits(max_connections=CONCURRENCY)
            auth = {"Authorization": f"Bearer {issued.raw_key}"}
            async with httpx.AsyncClient(
                base_url=url, headers=auth, limits=limits, timeout=30
            ) as client:
                slots = asyncio.Semaphore(CONCURRENCY)

                async def one(i: int) -> None:
                    async with slots:
                        await send(client, i)

                await asyncio.gather(*(one(i) for i in range(N)))

            # The ledger is written in batches; the mocks notice cut-offs late.
            rows: list[Any] = []

            async def settled() -> bool:
                nonlocal rows
                rows = await ledger_rows(tenant_id)
                a, b = stats(exact_url), stats(disconnect_url)
                return (
                    len(rows) == N
                    and totals(rows, EXACT)
                    == (
                        a["prompt_tokens"] + a["partial_prompt_tokens"],
                        a["completion_tokens"] + a["partial_completion_tokens"],
                    )
                    and totals(rows, DISCONNECTED)[0]
                    == b["prompt_tokens"] + b["partial_prompt_tokens"]
                )

            for _ in range(300):
                if await settled():
                    break
                await asyncio.sleep(0.1)
            a, b = stats(exact_url), stats(disconnect_url)

        redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
        try:
            spent = await redis.get(spend_key(str(tenant_id), await redis_month(redis)))
        finally:
            await redis.aclose()

    # One row per request, each with the status its outcome calls for.
    assert len(rows) == N, len(rows)
    expected = Counter({"client_disconnect": N // 100 * DISCONNECTS_PER_100})
    for count, _, _, status in MIX:
        expected[status] += N // 100 * count
    assert Counter(row.status for row in rows) == expected

    # Every outcome but a disconnect: exactly the provider's own counts.
    assert totals(rows, EXACT) == (
        a["prompt_tokens"] + a["partial_prompt_tokens"],
        a["completion_tokens"] + a["partial_completion_tokens"],
    )
    # Disconnected streams: prompts exactly; completions short by at most the
    # tokens in flight when the gateway hung up.
    prompt, completion = totals(rows, DISCONNECTED)
    assert prompt == b["prompt_tokens"] + b["partial_prompt_tokens"]
    served = b["completion_tokens"] + b["partial_completion_tokens"]
    disconnects = expected["client_disconnect"]
    assert 0 <= served - completion <= disconnects * in_flight_bound(DISCONNECT_TOKENS_PER_SEC)

    # And the month's spend counter holds exactly the ledger's costs.
    assert int(spent) == micro_usd(sum((row.cost_usd for row in rows), Decimal(0))) > 0
