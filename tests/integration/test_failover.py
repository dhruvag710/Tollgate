"""Failover and circuit breakers end to end (SPEC Phase 2.1), with a gateway in
process and two mock providers on real sockets, so timeouts, drops and
refused connections happen as they would in deployment.

The chaos tests X1-X3 (tests/chaos/) check the same against the compose
stack; these run in CI, with a breaker that opens after 4 attempts and cools
down in half a second.
"""

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from prometheus_client.parser import text_string_to_metric_families

from mock_provider.app import Stats
from mock_provider.app import create_app as create_mock_app
from tests.helpers import ADMIN, MemoryLedger, issue_key, serve, wait_until
from tests.integration.test_rate_limit import RoundTrips, remaining
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.usage.ledger import UsageEvent

MIN_REQUESTS, COOLDOWN_S = 4, 0.5
MESSAGES = [{"role": "user", "content": "route me somewhere healthy"}]
FAIL_500 = {"fail_rate": 1, "fail_mode": "500"}


def providers_config(
    tmp_path: Path,
    primary: str,
    secondary: str,
    *,
    secondary_overrides: dict[str, Any] | None = None,
    **primary_overrides: Any,
) -> Path:
    config = {
        "providers": [
            {"name": "mock", "type": "openai_compat", "base_url": f"{primary}/v1",
             "forward_mock_headers": True, **primary_overrides},
            {"name": "mock-secondary", "type": "openai_compat", "base_url": f"{secondary}/v1",
             "forward_mock_headers": True, **(secondary_overrides or {})},
        ],
        "models": {
            "gpt-4o-mini": {"routes": ["mock", "mock-secondary"],
                            "price_per_1m": {"input": 0.15, "output": 0.6}},
            "llama3.1:8b": {"routes": ["mock"], "price_per_1m": {"input": 0, "output": 0}},
        },
        "breaker": {"min_requests": MIN_REQUESTS, "window_s": 10, "cooldown_s": COOLDOWN_S},
    }  # fmt: skip
    path = tmp_path / f"providers-{time.monotonic_ns()}.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


class Mock:
    """A mock provider on a real socket."""

    def __init__(self, app: FastAPI, url: str) -> None:
        self.app, self.url = app, url

    @property
    def stats(self) -> Stats:
        stats: Stats = self.app.state.stats
        return stats

    async def behave(self, **knobs: Any) -> None:
        """Every request from now on behaves so; none restores the defaults."""
        async with httpx.AsyncClient() as client:
            r = await client.put(f"{self.url}/mock/behaviour", json=knobs)
        assert r.status_code == 200, r.text


@contextmanager
def mock_providers() -> Iterator[tuple[Mock, Mock]]:
    primary, secondary = create_mock_app(env={"MOCK_HANG_S": "5"}), create_mock_app(env={})
    with serve(primary) as (p_url, _), serve(secondary) as (s_url, _):
        yield Mock(primary, p_url), Mock(secondary, s_url)


class Gateway:
    def __init__(self, app: FastAPI, client: httpx.AsyncClient, ledger: MemoryLedger) -> None:
        self.app, self.client, self.ledger = app, client, ledger

    async def chat(
        self, *, stream: bool = False, model: str = "gpt-4o-mini", **fields: Any
    ) -> httpx.Response:
        body = {"model": model, "messages": MESSAGES, "stream": stream, **fields}
        r = await self.client.post("/v1/chat/completions", json=body)
        await self.settled()
        return r

    async def settled(self) -> None:
        """Every usage event recorded, and every breaker report made."""
        await self.app.state.metering.wait_idle()
        await self.app.state.router.breakers.wait_idle()

    async def breaker(self, provider: str = "mock") -> dict[str, Any]:
        r = await self.client.get("/admin/providers", headers=ADMIN)
        assert r.status_code == 200, r.text
        (state,) = [p for p in r.json()["providers"] if p["name"] == provider]
        return dict(state)

    def last(self) -> UsageEvent:
        return self.ledger.events[-1]

    async def metric(self, name: str, **labels: str) -> float:
        text = (await self.client.get("/metrics")).text
        for family in text_string_to_metric_families(text):
            for s in family.samples:
                if s.name == name and s.labels == labels:
                    return float(s.value)
        return 0.0


@asynccontextmanager
async def gateway(config: Path, **tenant: Any) -> AsyncIterator[Gateway]:
    issued = await issue_key(**tenant)
    ledger = MemoryLedger()
    app = create_app(Settings(providers_config=config), ledger=ledger)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://tollgate",
            headers={"Authorization": f"Bearer {issued.raw_key}"},
            timeout=30,
        ) as client,
    ):
        yield Gateway(app, client, ledger)


@pytest.fixture
def mocks() -> Iterator[tuple[Mock, Mock]]:
    with mock_providers() as pair:
        yield pair


@pytest.fixture
async def gw(tmp_path: Path, mocks: tuple[Mock, Mock]) -> AsyncIterator[Gateway]:
    primary, secondary = mocks
    async with gateway(providers_config(tmp_path, primary.url, secondary.url)) as g:
        yield g


async def eventually(predicate: Any, within_s: float = 2.0) -> bool:
    """wait_until, without blocking the event loop the gateway runs on."""
    deadline = time.monotonic() + within_s
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def events_of(r: httpx.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line[6:])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


# --- what fails over ---------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "streamed"])
@pytest.mark.parametrize("mode", ["500", "429"])
async def test_a_5xx_or_429_fails_over_to_the_next_provider(
    gw: Gateway, mocks: tuple[Mock, Mock], stream: bool, mode: str
) -> None:
    primary, secondary = mocks
    await primary.behave(fail_rate=1, fail_mode=mode)
    r = await gw.chat(stream=stream)
    assert r.status_code == 200, r.text
    assert r.headers["x-tollgate-provider"] == "mock-secondary"
    assert primary.stats.failures[mode] == 1 and secondary.stats.completed == 1
    event = gw.last()
    assert (event.provider, event.attempts, event.status) == ("mock-secondary", 2, "success")
    # The error response billed nothing: exactly the secondary's usage.
    usage = (secondary.stats.prompt_tokens, secondary.stats.completion_tokens)
    assert (event.prompt_tokens, event.completion_tokens) == usage
    assert not event.usage_estimated


@contextmanager
def refusing(status: int) -> Iterator[str]:
    """A provider that refuses Tollgate's credentials."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def refuse() -> JSONResponse:
        error = {"message": "Incorrect API key provided", "type": "invalid_request_error"}
        return JSONResponse({"error": error}, status_code=status)

    with serve(app) as (url, _):
        yield url


@pytest.mark.parametrize("status", [401, 403])
async def test_an_upstream_auth_failure_fails_over(
    tmp_path: Path, mocks: tuple[Mock, Mock], status: int
) -> None:
    _, secondary = mocks
    with refusing(status) as url:
        async with gateway(providers_config(tmp_path, url, secondary.url)) as g:
            r = await g.chat()
    assert r.status_code == 200, r.text
    assert (g.last().attempts, g.last().provider) == (2, "mock-secondary")


def dead_url() -> str:
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "streamed"])
async def test_an_unreachable_provider_fails_over_and_bills_nothing(
    tmp_path: Path, mocks: tuple[Mock, Mock], stream: bool
) -> None:
    _, secondary = mocks
    async with gateway(providers_config(tmp_path, dead_url(), secondary.url)) as g:
        r = await g.chat(stream=stream)
    assert r.status_code == 200, r.text
    event = g.last()
    assert (event.attempts, event.provider, event.usage_estimated) == (2, "mock-secondary", False)
    usage = (secondary.stats.prompt_tokens, secondary.stats.completion_tokens)
    assert (event.prompt_tokens, event.completion_tokens) == usage


@pytest.mark.parametrize(
    ("stream", "slow"),
    [(False, {"latency_ms": 3000}), (True, {"ttft_ms": 3000})],
    ids=["plain", "streamed"],
)
async def test_a_timeout_before_the_first_byte_fails_over_and_bills_the_prompt(
    tmp_path: Path, mocks: tuple[Mock, Mock], stream: bool, slow: dict[str, int]
) -> None:
    primary, secondary = mocks
    await primary.behave(**slow)
    config = providers_config(tmp_path, primary.url, secondary.url, first_byte_timeout_s=0.3)
    async with gateway(config) as g:
        started = time.perf_counter()
        r = await g.chat(stream=stream)
        took = time.perf_counter() - started
    assert r.status_code == 200, r.text
    assert took < 2.5  # didn't wait the 3 s out
    event = g.last()
    assert (event.attempts, event.provider, event.status) == (2, "mock-secondary", "success")
    # The primary may bill the prompt it was sent: the event carries an
    # estimate of it, which matches what the mock counts.
    assert event.usage_estimated
    prompt = estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.prompt_tokens == secondary.stats.prompt_tokens + prompt
    assert event.completion_tokens == secondary.stats.completion_tokens
    assert wait_until(lambda: primary.stats.partial_prompt_tokens == prompt)


@contextmanager
def dropping() -> Iterator[str]:
    """A provider that starts a stream, then drops the connection before any of
    its body: past the headers, still before the first byte of the response."""
    app = FastAPI()

    async def nothing() -> AsyncIterator[bytes]:
        raise ConnectionResetError("dropped")
        yield b""  # pragma: no cover  (an async generator, for StreamingResponse)

    @app.post("/v1/chat/completions")
    async def drop() -> StreamingResponse:
        return StreamingResponse(nothing(), media_type="text/event-stream")

    with serve(app) as (url, _):
        yield url


async def test_a_stream_dropped_before_its_first_byte_fails_over(
    tmp_path: Path, mocks: tuple[Mock, Mock]
) -> None:
    _, secondary = mocks
    with dropping() as url:
        async with gateway(providers_config(tmp_path, url, secondary.url)) as g:
            r = await g.chat(stream=True)
    assert r.status_code == 200, r.text
    assert r.headers["x-tollgate-provider"] == "mock-secondary"
    assert events_of(r)[-1]["choices"][0]["finish_reason"] == "stop"
    event = g.last()
    assert (event.attempts, event.status) == (2, "success")
    # Sent, never answered: the dropped provider may bill the prompt.
    prompt = estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.usage_estimated
    assert event.prompt_tokens == secondary.stats.prompt_tokens + prompt


@pytest.mark.parametrize("stream", [False, True], ids=["plain", "streamed"])
async def test_when_every_provider_fails_the_last_answer_is_relayed(
    gw: Gateway, mocks: tuple[Mock, Mock], stream: bool
) -> None:
    for mock in mocks:
        await mock.behave(**FAIL_500)
    r = await gw.chat(stream=stream)
    assert r.status_code == 500, r.text
    assert r.headers["x-tollgate-provider"] == "mock-secondary"
    assert r.json()["error"]["message"] == "Mock provider internal error"  # the provider's own
    event = gw.last()
    assert (event.attempts, event.status, event.provider) == (2, "provider_error", "mock-secondary")
    # Error responses: nothing billed, by either.
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_when_every_provider_is_unreachable_the_last_error_is_returned(
    tmp_path: Path,
) -> None:
    async with gateway(providers_config(tmp_path, dead_url(), dead_url())) as g:
        r = await g.chat()
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "upstream_unreachable"
    assert r.headers["x-tollgate-provider"] == "mock-secondary"
    event = g.last()
    assert (event.attempts, event.status) == (2, "provider_error")
    assert (event.prompt_tokens, event.usage_estimated) == (0, False)  # never sent


@pytest.mark.parametrize(
    ("stream", "slow"),
    [(False, {"latency_ms": 3000}), (True, {"ttft_ms": 3000})],
    ids=["plain", "streamed"],
)
async def test_when_every_provider_times_out_the_last_timeout_is_returned(
    tmp_path: Path, mocks: tuple[Mock, Mock], stream: bool, slow: dict[str, int]
) -> None:
    primary, secondary = mocks
    for mock in mocks:
        await mock.behave(**slow)
    fast = {"first_byte_timeout_s": 0.3}
    config = providers_config(
        tmp_path, primary.url, secondary.url, secondary_overrides=fast, **fast
    )
    async with gateway(config) as g:
        started = time.perf_counter()
        r = await g.chat(stream=stream)
        took = time.perf_counter() - started
        # Both timeouts count against their breakers.
        breakers = [await g.breaker(p) for p in ("mock", "mock-secondary")]
    assert [b["failures"] for b in breakers] == [1, 1]
    assert r.status_code == 504, r.text
    assert r.json()["error"]["code"] == "upstream_timeout"
    assert r.headers["x-tollgate-provider"] == "mock-secondary"
    assert took < 2.5  # one first-byte timeout each, not the mocks' 3 s
    event = g.last()
    assert (event.attempts, event.status, event.provider) == (2, "provider_error", "mock-secondary")
    # Both were sent the prompt and never answered: each may bill it, and the
    # one event carries both estimates, which match what each mock counted.
    prompt = estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert (event.prompt_tokens, event.completion_tokens) == (2 * prompt, 0)
    assert event.usage_estimated
    for mock in mocks:
        assert wait_until(lambda m=mock: m.stats.partial_prompt_tokens == prompt)  # type: ignore[misc]
        assert mock.stats.completed == 0


# --- what doesn't ------------------------------------------------------------------------


async def test_a_client_error_is_relayed_not_retried(gw: Gateway, mocks: tuple[Mock, Mock]) -> None:
    _, secondary = mocks
    r = await gw.chat(n=2)  # the mock serves only n=1: a 400
    assert r.status_code == 400, r.text
    assert secondary.stats.requests == 0
    event = gw.last()
    assert (event.attempts, event.provider, event.status) == (1, "mock", "provider_error")
    # The provider is up: a success as far as its breaker is concerned.
    assert await gw.breaker() == {
        "name": "mock", "state": "closed", "requests": 1, "failures": 0, "retry_in_s": None,
    }  # fmt: skip


async def test_a_stream_that_drops_midway_is_ended_not_retried(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, secondary = mocks
    await primary.behave(fail_rate=1, fail_mode="drop_mid_stream", output_tokens=10)
    r = await gw.chat(stream=True)
    assert r.status_code == 200
    *chunks, last = events_of(r)
    assert last["error"]["code"] == "upstream_stream_error"
    assert any(c["choices"][0]["delta"].get("content") for c in chunks)  # some got through
    assert secondary.stats.requests == 0  # no retry after the first byte
    event = gw.last()
    assert (event.attempts, event.status, event.usage_estimated) == (1, "provider_error", True)
    # Partial usage: what the mock sent before it dropped.
    assert wait_until(lambda: primary.stats.partial_completion_tokens == 5)
    assert (event.prompt_tokens, event.completion_tokens) == (
        primary.stats.partial_prompt_tokens,
        primary.stats.partial_completion_tokens,
    )
    assert (await gw.breaker())["failures"] == 1


async def test_a_response_broken_after_its_first_byte_is_not_retried(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, secondary = mocks
    await primary.behave(fail_rate=1, fail_mode="drop_mid_stream")
    r = await gw.chat()  # not streamed: the mock sends half its JSON, then drops
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "upstream_unreachable"
    assert secondary.stats.requests == 0
    assert gw.last().attempts == 1


# --- the breaker -------------------------------------------------------------------------


async def open_primary(gw: Gateway, primary: Mock) -> None:
    """Fail MIN_REQUESTS requests at the primary: its breaker opens."""
    await primary.behave(**FAIL_500)
    for _ in range(MIN_REQUESTS):
        r = await gw.chat()
        assert (r.status_code, gw.last().attempts) == (200, 2), r.text
    assert (await gw.breaker())["state"] == "open"


async def test_the_breaker_opens_and_requests_skip_the_provider(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, secondary = mocks
    await primary.behave(**FAIL_500)
    for i in range(MIN_REQUESTS):
        assert (await gw.breaker())["state"] == "closed"
        assert (await gw.chat()).status_code == 200
        assert primary.stats.requests == i + 1
    state = await gw.breaker()
    assert state["state"] == "open" and 0 < state["retry_in_s"] <= COOLDOWN_S
    # Open: requests go straight to the secondary, one attempt each.
    for stream in (False, True, False):
        r = await gw.chat(stream=stream)
        assert (r.status_code, r.headers["x-tollgate-provider"]) == (200, "mock-secondary")
        assert gw.last().attempts == 1
    assert primary.stats.requests == MIN_REQUESTS
    assert secondary.stats.completed == MIN_REQUESTS + 3
    labels = {"provider": "mock", "result": "server_error"}
    assert await gw.metric("tollgate_provider_attempts_total", **labels) == MIN_REQUESTS
    opened = {"provider": "mock", "state": "open"}
    assert await gw.metric("tollgate_breaker_transitions_total", **opened) == 1


async def test_after_the_cooldown_one_probe_brings_traffic_back(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, _ = mocks
    await open_primary(gw, primary)
    await primary.behave(latency_ms=400)  # recovered, and slow enough to watch
    await asyncio.sleep(COOLDOWN_S + 0.1)
    probe = asyncio.create_task(gw.chat())
    assert await eventually(lambda: primary.stats.requests == MIN_REQUESTS + 1)
    assert (await gw.breaker())["state"] == "half_open"
    # While the probe is out, everyone else goes to the secondary.
    others = await asyncio.gather(*(gw.chat() for _ in range(3)))
    assert [r.headers["x-tollgate-provider"] for r in others] == ["mock-secondary"] * 3
    r = await probe
    assert (r.status_code, r.headers["x-tollgate-provider"]) == (200, "mock")
    assert (await gw.breaker())["state"] == "closed"
    await primary.behave()
    r = await gw.chat()
    assert r.headers["x-tollgate-provider"] == "mock"
    assert primary.stats.requests == MIN_REQUESTS + 2  # exactly one probe, then traffic
    closed = {"provider": "mock", "state": "closed"}
    assert await gw.metric("tollgate_breaker_transitions_total", **closed) == 1


async def test_a_failed_probe_opens_it_again(gw: Gateway, mocks: tuple[Mock, Mock]) -> None:
    primary, _ = mocks
    await open_primary(gw, primary)
    await asyncio.sleep(COOLDOWN_S + 0.1)
    r = await gw.chat()  # the probe: still failing, so it fails over
    assert (r.status_code, r.headers["x-tollgate-provider"]) == (200, "mock-secondary")
    assert gw.last().attempts == 2
    state = await gw.breaker()
    assert state["state"] == "open" and state["retry_in_s"] > COOLDOWN_S / 2  # a new cooldown
    assert primary.stats.requests == MIN_REQUESTS + 1


async def test_a_probe_cut_off_before_its_answer_is_released(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, _ = mocks
    await open_primary(gw, primary)
    await primary.behave(latency_ms=2000)
    await asyncio.sleep(COOLDOWN_S + 0.1)
    body = {"model": "gpt-4o-mini", "messages": MESSAGES}
    probe = asyncio.create_task(gw.client.post("/v1/chat/completions", json=body))
    assert await eventually(lambda: primary.stats.requests == MIN_REQUESTS + 1)
    assert (await gw.breaker())["state"] == "half_open"
    probe.cancel()  # cut off mid-attempt, no verdict
    with suppress(asyncio.CancelledError):
        await probe
    await gw.settled()
    # Back to open, and due a probe at once rather than after the lease.
    assert await gw.breaker() == {
        "name": "mock", "state": "open", "requests": 0, "failures": 0, "retry_in_s": 0,
    }  # fmt: skip
    await primary.behave()
    r = await gw.chat()
    assert r.headers["x-tollgate-provider"] == "mock"  # the next request probed, and closed it
    assert (await gw.breaker())["state"] == "closed"
    # A release isn't an opening.
    opened = {"provider": "mock", "state": "open"}
    assert await gw.metric("tollgate_breaker_transitions_total", **opened) == 1


async def test_concurrent_requests_send_only_one_probe(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, _ = mocks
    await open_primary(gw, primary)
    await primary.behave(latency_ms=300)
    await asyncio.sleep(COOLDOWN_S + 0.1)
    responses = await asyncio.gather(*(gw.chat() for _ in range(10)))
    providers = sorted(r.headers["x-tollgate-provider"] for r in responses)
    assert providers == ["mock"] + ["mock-secondary"] * 9
    assert primary.stats.requests == MIN_REQUESTS + 1


async def test_with_every_breaker_open_nothing_is_called(
    gw: Gateway, mocks: tuple[Mock, Mock]
) -> None:
    primary, _ = mocks
    await primary.behave(**FAIL_500)
    # llama3.1:8b has one route: its failures are the client's 500s.
    for _ in range(MIN_REQUESTS):
        assert (await gw.chat(model="llama3.1:8b")).status_code == 500
    events = len(gw.ledger.events)
    r = await gw.chat(model="llama3.1:8b")
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "no_provider_available"
    assert r.headers["retry-after"] == "1"
    assert primary.stats.requests == MIN_REQUESTS
    assert len(gw.ledger.events) == events  # it never reached a provider


async def test_a_refused_request_gives_its_tpm_reservation_back(
    tmp_path: Path, mocks: tuple[Mock, Mock]
) -> None:
    primary, secondary = mocks
    config = providers_config(tmp_path, primary.url, secondary.url)
    async with gateway(config, tpm_limit=100_000) as g:
        await primary.behave(**FAIL_500)
        for _ in range(MIN_REQUESTS):
            await g.chat(model="llama3.1:8b", max_tokens=1000)
        assert (await g.chat(model="llama3.1:8b", max_tokens=50_000)).status_code == 503
        r = await g.chat(max_tokens=1000)  # routed to the secondary
    reserved = estimate_prompt_tokens(MESSAGES, "gpt-4o-mini") + 1000
    assert remaining(r)[1] == 100_000 - reserved  # the 503's 50,000 came back


async def test_breakers_are_shared_by_every_replica(
    tmp_path: Path, mocks: tuple[Mock, Mock]
) -> None:
    primary, secondary = mocks
    config = providers_config(tmp_path, primary.url, secondary.url)
    async with gateway(config) as a, gateway(config) as b:
        await open_primary(a, primary)
        r = await b.chat()
        assert r.headers["x-tollgate-provider"] == "mock-secondary"
        assert b.last().attempts == 1  # b never tried the primary
    assert primary.stats.requests == MIN_REQUESTS


async def test_routing_past_an_open_breaker_costs_no_round_trip(
    gw: Gateway, mocks: tuple[Mock, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    primary, _ = mocks
    await open_primary(gw, primary)  # also fills the key cache
    trips = RoundTrips(monkeypatch)
    r = await gw.chat()
    monkeypatch.undo()
    assert r.headers["x-tollgate-provider"] == "mock-secondary"
    # The lookup that admitted the request also read the breakers; the settle
    # call counted the secondary's success. Nothing else.
    assert trips.sent == [
        [("EVALSHA", gw.app.state.key_cache._lookup.sha)],
        [("EVALSHA", gw.app.state.limiter._settle.sha)],
    ]
