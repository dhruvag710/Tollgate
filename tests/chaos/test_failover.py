"""X1-X3 (SPEC §8) against the compose stack: gpt-4o-mini routes to mock
(mock_provider, localhost:8001) and then mock-secondary
(mock_provider_secondary, localhost:8002)."""

import asyncio
import json
import threading
import time
from typing import Any

import httpx

from tests.chaos.conftest import (
    COOLDOWN_S,
    GATEWAY,
    MIN_REQUESTS,
    PRIMARY,
    SECONDARY,
    TIMEOUT,
    Tenant,
    behave,
    breaker,
    stats,
)

MESSAGES = [{"role": "user", "content": "is anyone up?"}]


def chat(tenant: Tenant, *, stream: bool = False, **headers: str) -> httpx.Response:
    body = {"model": "gpt-4o-mini", "messages": MESSAGES, "stream": stream}
    return httpx.post(
        f"{GATEWAY}/v1/chat/completions",
        json=body,
        headers={**tenant.auth, **headers},
        timeout=TIMEOUT,
    )


def eventually(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def open_primary(tenant: Tenant) -> None:
    """X1's outage: the primary fails every request until its breaker opens."""
    behave(PRIMARY, fail_rate=1, fail_mode="500")
    for _ in range(MIN_REQUESTS):
        assert chat(tenant).status_code == 200
    assert eventually(lambda: breaker()["state"] == "open")


async def test_x1_requests_succeed_via_the_secondary_and_the_breaker_opens(
    tenant: Tenant, ledger: Any
) -> None:
    behave(PRIMARY, fail_rate=1, fail_mode="500")
    served = []
    for i in range(MIN_REQUESTS):
        assert breaker()["state"] == "closed", i  # min_requests not reached yet
        r = chat(tenant, stream=i % 2 == 1)
        assert r.status_code == 200, r.text
        served.append(r.headers["x-tollgate-provider"])
    # 20 failures out of 20: the breaker opens on the last of them.
    assert eventually(lambda: breaker()["state"] == "open")
    tried = stats(PRIMARY)["requests"]
    assert tried == MIN_REQUESTS
    for i in range(20):
        r = chat(tenant, stream=i % 2 == 1)
        assert r.status_code == 200, r.text
        served.append(r.headers["x-tollgate-provider"])
    assert served == ["mock-secondary"] * (MIN_REQUESTS + 20)  # every request succeeded
    assert stats(PRIMARY)["requests"] == tried  # once open, nobody tried it
    assert stats(SECONDARY)["completed"] == MIN_REQUESTS + 20

    # One usage event per request: two attempts each until the breaker
    # opened, one after; all served, and billed, by the secondary.
    rows: list[Any] = []

    async def recorded() -> bool:
        nonlocal rows
        rows = await ledger(tenant.id)
        return len(rows) == MIN_REQUESTS + 20

    for _ in range(50):
        if await recorded():
            break
        await asyncio.sleep(0.1)
    assert [(r.status, r.provider) for r in rows] == [("success", "mock-secondary")] * len(rows)
    assert [r.attempts for r in rows] == [2] * MIN_REQUESTS + [1] * 20
    secondary = stats(SECONDARY)
    assert sum(r.prompt_tokens for r in rows) == secondary["prompt_tokens"]
    assert sum(r.completion_tokens for r in rows) == secondary["completion_tokens"]


def test_x2_the_primary_recovers_through_one_probe_and_takes_traffic_back(
    tenant: Tenant,
) -> None:
    open_primary(tenant)
    opened_at, tried = time.monotonic(), stats(PRIMARY)["requests"]
    # Recovered, and slow enough that the probe can be watched in flight.
    behave(PRIMARY, latency_ms=1500)
    # Until the cooldown is over, the primary gets nothing.
    assert chat(tenant).headers["x-tollgate-provider"] == "mock-secondary"
    assert eventually(lambda: breaker()["retry_in_s"] == 0, timeout=COOLDOWN_S + 5)
    assert time.monotonic() - opened_at > COOLDOWN_S - 5
    assert stats(PRIMARY)["requests"] == tried

    with httpx.Client(timeout=TIMEOUT) as client:
        probe = client.build_request(
            "POST",
            f"{GATEWAY}/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": MESSAGES},
            headers=tenant.auth,
        )
        results: list[httpx.Response] = []
        sender = threading.Thread(target=lambda: results.append(client.send(probe)))
        sender.start()
        # HALF_OPEN while the probe is out; everyone else still goes elsewhere.
        assert eventually(lambda: breaker()["state"] == "half_open")
        assert chat(tenant).headers["x-tollgate-provider"] == "mock-secondary"
        sender.join(timeout=30)
    (r,) = results
    assert (r.status_code, r.headers["x-tollgate-provider"]) == (200, "mock")
    assert eventually(lambda: breaker()["state"] == "closed")  # reported as the probe answered

    behave(PRIMARY)
    for _ in range(5):
        assert chat(tenant).headers["x-tollgate-provider"] == "mock"  # traffic is back
    assert stats(PRIMARY)["requests"] == tried + 1 + 5  # exactly one probe


async def test_x3_a_dropped_stream_ends_with_an_error_event_and_is_not_retried(
    tenant: Tenant, ledger: Any
) -> None:
    drop = {"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "drop_mid_stream"}
    r = chat(tenant, stream=True, **drop, **{"X-Mock-Output-Tokens": "10"})
    assert r.status_code == 200  # the stream had started
    lines = [line for line in r.text.splitlines() if line.startswith("data: ")]
    last = json.loads(lines[-1][6:])
    assert last["error"]["code"] == "upstream_stream_error"  # a terminating error event
    assert "data: [DONE]" not in lines
    # No retry: the secondary, sent the same failure headers, never saw it.
    assert stats(SECONDARY)["requests"] == 0

    rows: list[Any] = []
    for _ in range(50):
        rows = await ledger(tenant.id)
        if rows:
            break
        await asyncio.sleep(0.1)
    (row,) = rows
    assert (row.status, row.provider, row.attempts) == ("provider_error", "mock", 1)
    # Partial usage, exactly what the primary sent before it dropped.
    primary = stats(PRIMARY)
    assert row.usage_estimated
    assert (row.prompt_tokens, row.completion_tokens) == (
        primary["partial_prompt_tokens"],
        primary["partial_completion_tokens"],
    )
    assert row.completion_tokens == 5
