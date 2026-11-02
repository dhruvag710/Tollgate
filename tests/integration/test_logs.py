"""Logs (SPEC Phase 1.7, §10): one JSON object per line, each with the
request_id of the request it was logged during; and never a prompt or a key,
whatever path a request takes, even at DEBUG."""

import asyncio
import io
import json
import logging
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mock_provider.app import create_app as create_mock_app
from tests.helpers import (
    HTTP_TIMEOUT,
    TEST_PEPPER,
    TEST_PROVIDERS,
    MemoryLedger,
    issue_key,
    run_server,
    run_uvicorn_process,
)
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.observability.logging import JsonFormatter

PROMPT = f"zebra-canary-prompt-{secrets.token_hex(8)}"
PROVIDER_KEY = f"sk-canary-provider-{secrets.token_hex(8)}"
ADMIN_TOKEN = f"admin-canary-{secrets.token_hex(8)}"
MESSAGES = [{"role": "user", "content": PROMPT}]


def echoing_provider() -> FastAPI:
    """A provider whose error response repeats the prompt and the key it got:
    real ones do echo input back in errors."""
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request) -> JSONResponse:
        prompt = (await request.json())["messages"][0]["content"]
        echoed = f"rejected {prompt!r} sent with {request.headers['authorization']}"
        return JSONResponse({"error": {"message": echoed, "type": "invalid_request_error"}}, 400)

    return app


def providers_config(tmp_path: Path, mock_url: str, echo_url: str) -> Path:
    config = {
        "providers": [
            {"name": "mock", "type": "openai_compat", "base_url": f"{mock_url}/v1",
             "api_key_env": "CANARY_PROVIDER_KEY", "forward_mock_headers": True},
            {"name": "echo", "type": "openai_compat", "base_url": f"{echo_url}/v1",
             "api_key_env": "CANARY_PROVIDER_KEY"},
        ],
        "models": {
            "gpt-4o-mini": {"routes": ["mock"], "price_per_1m": {"input": 0.15, "output": 0.6}},
            "llama3.1:8b": {"routes": ["echo"], "price_per_1m": {"input": 0, "output": 0}},
        },
    }  # fmt: skip
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_logs_are_json_with_request_ids_and_hold_no_prompt_or_key(tmp_path: Path) -> None:
    good = asyncio.run(issue_key())
    limited = asyncio.run(issue_key(rpm_limit=1))
    revoked = asyncio.run(issue_key())
    unknown_key = "tg_live_" + secrets.token_urlsafe(32).replace("-", "a").replace("_", "b")[:43]
    malformed_key = f"malformed-canary-{secrets.token_hex(8)}"
    output = tmp_path / "gateway.log"

    with (
        run_server(create_mock_app(env={})) as mock_url,
        run_server(echoing_provider()) as echo_url,
    ):
        env = {
            "PROVIDERS_CONFIG": str(providers_config(tmp_path, mock_url, echo_url)),
            "LOG_LEVEL": "DEBUG",
            "CANARY_PROVIDER_KEY": PROVIDER_KEY,
            "ADMIN_TOKEN": ADMIN_TOKEN,
            "MAX_REQUEST_BODY_BYTES": "4096",
        }
        with (
            run_uvicorn_process(
                "--factory", "tollgate.main:create_app", env=env, output=output
            ) as url,
            httpx.Client(base_url=url, timeout=HTTP_TIMEOUT) as client,
        ):

            def chat(key: str | None, body: Any = None, **headers: str) -> httpx.Response:
                auth = {"Authorization": f"Bearer {key}"} if key else {}
                body = body or {"model": "gpt-4o-mini", "messages": MESSAGES}
                return client.post("/v1/chat/completions", json=body, headers={**auth, **headers})

            success = chat(good.raw_key)
            assert success.status_code == 200
            stream = {"model": "gpt-4o-mini", "messages": MESSAGES, "stream": True}
            assert chat(good.raw_key, stream).status_code == 200
            with client.stream(  # a client that hangs up mid-stream
                "POST",
                "/v1/chat/completions",
                json=stream,
                headers={"Authorization": f"Bearer {good.raw_key}", "X-Mock-Tokens-Per-Sec": "20",
                         "X-Mock-Output-Tokens": "100"},
            ) as r:  # fmt: skip
                lines = r.iter_lines()
                next(lines), next(lines)
            fail = {"X-Mock-Fail-Rate": "1"}
            assert chat(good.raw_key, **fail, **{"X-Mock-Fail-Mode": "500"}).status_code == 500
            chat(good.raw_key, stream, **fail, **{"X-Mock-Fail-Mode": "drop_mid_stream"})
            echoed = chat(good.raw_key, {"model": "llama3.1:8b", "messages": MESSAGES})
            assert echoed.status_code == 400
            assert (
                chat(good.raw_key, {"model": "gpt-4o-mini", "messages": PROMPT}).status_code == 400
            )
            huge = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": PROMPT * 300}]}
            assert chat(good.raw_key, huge).status_code == 413
            in_query = client.post(f"/v1/chat/completions?api_key={good.raw_key}", json=stream)
            assert in_query.status_code == 401
            assert chat(unknown_key).status_code == 401
            assert chat(malformed_key).status_code == 401
            admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
            assert client.delete(f"/admin/keys/{revoked.key.id}", headers=admin).status_code == 204
            assert chat(revoked.raw_key).status_code == 401
            wrong_admin = {"Authorization": f"Bearer {ADMIN_TOKEN}-wrong"}
            assert (
                client.delete(f"/admin/keys/{uuid.uuid4()}", headers=wrong_admin).status_code == 401
            )
            assert chat(limited.raw_key).status_code == 200
            assert chat(limited.raw_key).status_code == 429
            assert client.get(
                "/v1/models", headers={"Authorization": f"Bearer {good.raw_key}"}
            ).is_success

    text = output.read_text(encoding="utf-8")
    secrets_ = {
        "prompt": PROMPT,
        "tenant key": good.raw_key,
        "rate-limited tenant's key": limited.raw_key,
        "revoked key": revoked.raw_key,
        "unknown key": unknown_key,
        "malformed key": malformed_key,
        "provider key": PROVIDER_KEY,
        "admin token": ADMIN_TOKEN,
        "key pepper": TEST_PEPPER,
    }
    leaked = [name for name, value in secrets_.items() if value in text]
    assert leaked == [], f"logged: {leaked}"

    records = [json.loads(line) for line in text.splitlines() if line.strip()]  # all JSON
    assert len(records) > 20
    assert all({"ts", "level", "logger", "message", "request_id"} <= r.keys() for r in records)
    access = [r for r in records if r["logger"] == "uvicorn.access"]
    assert access and all(r["request_id"] for r in access)
    mine = [r for r in records if r["request_id"] == success.headers["x-request-id"]]
    assert any(r["logger"] == "uvicorn.access" for r in mine)  # logged under its own id


@pytest.fixture
def json_logs() -> Iterator[io.StringIO]:
    """Every record, formatted as the gateway formats it, at DEBUG."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield stream
    root.removeHandler(handler)
    root.setLevel(level)


async def test_an_internal_error_is_logged_without_the_prompt_or_key(
    json_logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = await issue_key()
    app = create_app(
        Settings(providers_config=TEST_PROVIDERS, log_level="DEBUG"),
        upstream_transport=httpx.ASGITransport(app=create_mock_app(env={})),
        ledger=MemoryLedger(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        provider = app.state.router.route("gpt-4o-mini")

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("bug in the gateway")

        monkeypatch.setattr(provider, "chat_completion", broken)
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o-mini", "messages": MESSAGES},
            headers={"Authorization": f"Bearer {issued.raw_key}"},
        )
    assert r.status_code == 500
    text = json_logs.getvalue()
    assert PROMPT not in text and issued.raw_key not in text
    (error,) = [
        json.loads(line)
        for line in text.splitlines()
        if json.loads(line)["logger"] == "tollgate.errors"
    ]
    assert error["request_id"] == r.headers["x-request-id"]
    assert "RuntimeError: bug in the gateway" in error["exc_info"]
