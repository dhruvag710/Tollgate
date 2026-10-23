"""Phase 1.1: non-streaming passthrough, driven by the official openai SDK.

Streaming lives in test_streaming.py.
"""

import asyncio
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from fastapi import FastAPI

from mock_provider.app import create_app as create_mock_app
from tests.helpers import (
    TEST_PROVIDERS,
    MemoryLedger,
    assert_internal_error_response,
    run_server,
    serve,
    wait_until,
    write_providers,
)
from tollgate.api.v1 import chat
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.ratelimit.estimate import estimate_prompt_tokens

MESSAGES = [{"role": "user", "content": "hello"}]


def test_sync_sdk_with_only_base_url_changed(tmp_path: Path) -> None:
    """Acceptance 1.1 over real sockets: stock OpenAI client, only base_url differs."""
    with run_server(create_mock_app(env={})) as mock_url:
        config = write_providers(tmp_path, f"{mock_url}/v1")
        with run_server(create_app(Settings(providers_config=config))) as gw_url:
            client = openai.OpenAI(base_url=f"{gw_url}/v1", api_key="anything")
            resp = client.chat.completions.create(model="gpt-4o-mini", messages=MESSAGES)
            models = client.models.list()

    assert resp.object == "chat.completion"
    assert resp.choices[0].message.role == "assistant"
    assert resp.choices[0].message.content
    assert resp.usage is not None and resp.usage.completion_tokens > 0
    assert {m.id for m in models} == {"gpt-4o-mini", "llama3.1:8b"}


async def test_async_sdk_response_and_headers(sdk: openai.AsyncOpenAI) -> None:
    raw = await sdk.chat.completions.with_raw_response.create(
        model="gpt-4o-mini", messages=MESSAGES, seed=3
    )
    resp = raw.parse()
    uuid.UUID(raw.headers["x-request-id"])
    assert raw.headers["x-tollgate-provider"] == "mock"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage is not None
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens


async def test_passthrough_is_deterministic_with_seed(sdk: openai.AsyncOpenAI) -> None:
    a = await sdk.chat.completions.create(model="gpt-4o-mini", messages=MESSAGES, seed=9)
    b = await sdk.chat.completions.create(model="gpt-4o-mini", messages=MESSAGES, seed=9)
    assert a.choices[0].message.content == b.choices[0].message.content


async def test_unknown_model_is_404(sdk: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.NotFoundError) as exc:
        await sdk.chat.completions.create(model="gpt-9", messages=MESSAGES)
    assert exc.value.code == "model_not_found"


async def test_missing_messages_is_400(sdk: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as exc:
        await sdk.chat.completions.create(model="gpt-4o-mini", messages=[])
    assert exc.value.code == "invalid_request"


async def test_success_records_reported_usage(
    sdk: openai.AsyncOpenAI, ledger: MemoryLedger
) -> None:
    raw = await sdk.chat.completions.with_raw_response.create(
        model="gpt-4o-mini", messages=MESSAGES
    )
    resp = raw.parse()
    assert resp.usage is not None
    (event,) = ledger.events
    assert event.request_id == raw.headers["x-request-id"]
    assert (event.model, event.provider, event.status) == ("gpt-4o-mini", "mock", "success")
    assert (event.prompt_tokens, event.completion_tokens) == (
        resp.usage.prompt_tokens,
        resp.usage.completion_tokens,
    )
    assert not event.usage_estimated
    assert event.ttft_ms is None


async def test_upstream_500_passes_through(sdk: openai.AsyncOpenAI, ledger: MemoryLedger) -> None:
    with pytest.raises(openai.InternalServerError) as exc:
        await sdk.chat.completions.create(
            model="gpt-4o-mini",
            messages=MESSAGES,
            extra_headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "500"},
        )
    assert exc.value.response.headers["x-tollgate-provider"] == "mock"
    (event,) = ledger.events
    assert (event.status, event.prompt_tokens, event.completion_tokens) == ("provider_error", 0, 0)


async def test_upstream_429_keeps_retry_after(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": MESSAGES},
        headers={"X-Mock-Fail-Rate": "1", "X-Mock-Fail-Mode": "429"},
    )
    assert r.status_code == 429
    assert r.headers["retry-after"] == "1"
    assert r.json()["error"]["code"] == "rate_limit_exceeded"


async def test_invalid_json_and_non_object(client: httpx.AsyncClient) -> None:
    r1 = await client.post("/v1/chat/completions", content=b"{")
    r2 = await client.post("/v1/chat/completions", json=["x"])
    assert r1.status_code == r2.status_code == 400
    assert r1.json()["error"]["code"] == "invalid_json"


async def test_body_size_limit() -> None:
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, max_request_body_bytes=100),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
    )
    big = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x" * 200}]}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        declared = await c.post("/v1/chat/completions", json=big)

        async def chunks():  # type: ignore[no-untyped-def]
            yield b'{"model": "gpt-4o-mini", "messages": ['
            yield b" " * 200

        streamed = await c.post("/v1/chat/completions", content=chunks())
    assert declared.status_code == streamed.status_code == 413


async def test_healthz_and_unknown_route(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    r = await client.get("/nope")
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "not_found_error"


# --- upstream transport behaviour, with a scripted transport ------------------


def _gateway_with(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    ledger: MemoryLedger | None = None,
    **overrides: Any,
) -> FastAPI:
    config = write_providers(tmp_path, "http://upstream/v1", **overrides)
    return create_app(
        Settings(providers_config=config),
        upstream_transport=httpx.MockTransport(handler),
        ledger=ledger,
    )


async def _post(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.Response:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        return await c.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": MESSAGES},
            headers=headers,
        )


async def test_provider_key_sent_and_client_key_not_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x", "choices": []})

    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-upstream")
    app = _gateway_with(
        tmp_path, handler, api_key_env="TEST_PROVIDER_KEY", forward_mock_headers=False
    )
    r = await _post(app, headers={"Authorization": "Bearer tg_client", "X-Mock-Seed": "1"})
    assert r.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer sk-upstream"
    assert "x-mock-seed" not in seen[0].headers
    assert str(seen[0].url) == "http://upstream/v1/chat/completions"


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (httpx.ConnectError("refused"), 502, "upstream_unreachable"),
        (httpx.ReadTimeout("slow"), 504, "upstream_timeout"),
    ],
)
async def test_transport_failures(tmp_path: Path, exc: Exception, status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    ledger = MemoryLedger()
    r = await _post(_gateway_with(tmp_path, handler, ledger))
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
    assert r.headers["x-tollgate-provider"] == "mock"
    (event,) = ledger.events
    assert (event.status, event.prompt_tokens, event.usage_estimated) == (
        "provider_error",
        0,
        False,
    )


async def test_missing_usage_is_estimated(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        message = {"role": "assistant", "content": "hello world"}
        return httpx.Response(200, json={"id": "x", "choices": [{"index": 0, "message": message}]})

    ledger = MemoryLedger()
    r = await _post(_gateway_with(tmp_path, handler, ledger))
    assert r.status_code == 200
    (event,) = ledger.events
    assert event.status == "success"
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.completion_tokens == 2


@pytest.mark.parametrize(
    ("upstream", "status", "code"),
    [
        (httpx.Response(200, text="not json"), 502, "upstream_bad_response"),
        (httpx.Response(401, json={"error": {"message": "bad key"}}), 502, "upstream_auth_failed"),
        (httpx.Response(503, json={"detail": "down"}), 503, "upstream_error"),
    ],
)
async def test_bad_upstream_responses(
    tmp_path: Path, upstream: httpx.Response, status: int, code: str
) -> None:
    r = await _post(_gateway_with(tmp_path, lambda _: upstream))
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_missing_provider_key_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from tollgate.config import load_providers_file
    from tollgate.providers.registry import ProviderRegistry

    config = load_providers_file(write_providers(tmp_path, "http://u/v1", api_key_env="NOPE_KEY"))
    ProviderRegistry.from_config(config, httpx.AsyncClient(), env={})
    assert "NOPE_KEY" in caplog.text


# --- usage invariant: exactly one event per request that reaches a provider ---


async def test_internal_error_before_any_response(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("bug")  # not an httpx error: the provider layer doesn't map it

    ledger = MemoryLedger()
    r = await _post(_gateway_with(tmp_path, handler, ledger))
    assert_internal_error_response(r, caplog)
    (event,) = ledger.events
    assert event.status == "gateway_error"
    # The transport never reported sending the request: nothing to estimate.
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


async def test_internal_error_after_response_keeps_its_usage(
    client: httpx.AsyncClient,
    ledger: MemoryLedger,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_response(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(chat, "JSONResponse", broken_response)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": MESSAGES},
        headers={"X-Mock-Output-Tokens": "9"},
    )
    assert_internal_error_response(r, caplog)
    (event,) = ledger.events
    assert event.status == "gateway_error"
    assert event.completion_tokens == 9  # reported by the provider, not estimated
    assert not event.usage_estimated


async def test_shutdown_before_the_request_is_sent_records_zero(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        # A transport that never reports sending: like being cut off mid-connect.
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    ledger = MemoryLedger()
    app = _gateway_with(tmp_path, handler, ledger)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        task = asyncio.create_task(
            c.post("/v1/chat/completions", json={"model": "gpt-4o-mini", "messages": MESSAGES})
        )
        await entered.wait()
        task.cancel()  # what uvicorn does to requests still running when its grace period ends
        with pytest.raises(asyncio.CancelledError):
            await task
    (event,) = ledger.events
    assert event.status == "gateway_shutdown"
    assert (event.prompt_tokens, event.completion_tokens, event.usage_estimated) == (0, 0, False)


def test_shutdown_after_the_request_is_sent_estimates_the_prompt(tmp_path: Path) -> None:
    """A real uvicorn shutdown cuts off a request the provider is working on."""
    ledger = MemoryLedger()
    with serve(create_mock_app(env={}), timeout_graceful_shutdown=0.1) as (mock_url, _):
        config = write_providers(tmp_path, f"{mock_url}/v1")
        app = create_app(Settings(providers_config=config), ledger=ledger)
        with (
            serve(app, timeout_graceful_shutdown=0.5) as (url, server),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            reply = pool.submit(
                httpx.post,
                f"{url}/v1/chat/completions",
                json={"model": "gpt-4o-mini", "messages": MESSAGES},
                headers={"X-Mock-Latency-Ms": "30000"},
                timeout=30,
            )
            # Wait until the request has reached the provider.
            assert wait_until(lambda: _mock_stats(mock_url)["requests"] == 1)
            server.should_exit = True  # as on SIGTERM
            assert reply.result(timeout=10).status_code == 500
            assert wait_until(lambda: len(ledger.events) == 1)
        # The mock saw the gateway leave and counts the prompt as received, unfinished.
        assert wait_until(lambda: _mock_stats(mock_url)["partial_prompt_tokens"] > 0)
        stats = _mock_stats(mock_url)
    (event,) = ledger.events
    assert event.status == "gateway_shutdown"
    assert event.usage_estimated
    assert event.prompt_tokens == estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert event.completion_tokens == 0
    assert (stats["partial_prompt_tokens"], stats["completed"]) == (event.prompt_tokens, 0)


def _mock_stats(mock_url: str) -> dict[str, Any]:
    stats: dict[str, Any] = httpx.get(f"{mock_url}/mock/stats").json()
    return stats
