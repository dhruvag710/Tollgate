import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from tollgate.errors import UnhandledErrorMiddleware, install_error_handlers
from tollgate.main import RequestIDMiddleware


class Body(BaseModel):
    n: int


def _app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/typed")
    async def typed(body: Body) -> dict[str, int]:
        return {"n": body.n}

    @app.get("/raise/{status}")
    async def raise_status(status: int) -> None:
        raise HTTPException(status, "boom")

    return app


async def _get(path: str, **kw: object) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://t"
    ) as c:
        return await c.request("POST" if path == "/typed" else "GET", path, **kw)  # type: ignore[arg-type]


async def test_validation_error_is_openai_shaped_400() -> None:
    r = await _get("/typed", json={"n": "x"})
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"


async def test_http_exception_types() -> None:
    for status, type_ in [
        (401, "authentication_error"),
        (404, "not_found_error"),
        (409, "invalid_request_error"),
        (503, "api_error"),
    ]:
        r = await _get(f"/raise/{status}")
        assert r.status_code == status
        assert r.json() == {
            "error": {"message": "boom", "type": type_, "param": None, "code": None}
        }


async def _cut_off(app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> list[Message]:
    """Run `app` behind the gateway's outer middleware; collect what it sends."""
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    stack = RequestIDMiddleware(UnhandledErrorMiddleware(app))
    with pytest.raises(asyncio.CancelledError):  # the cancellation itself goes on
        await stack({"type": "http", "method": "POST", "path": "/"}, receive, send)
    return sent


async def test_request_cut_off_by_shutdown_gets_503_with_retry_after() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise asyncio.CancelledError  # what uvicorn's graceful-shutdown timeout does

    start, body = await _cut_off(app)
    headers = dict(start["headers"])
    assert start["status"] == 503
    assert headers[b"retry-after"] == b"1"
    assert headers[b"content-type"] == b"application/json"
    uuid.UUID(headers[b"x-request-id"].decode())
    assert json.loads(body["body"]) == {
        "error": {
            "message": "Tollgate is shutting down. Retry the request.",
            "type": "api_error",
            "param": None,
            "code": "gateway_shutdown",
        }
    }


async def test_started_response_cut_off_by_shutdown_gets_nothing_more() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise asyncio.CancelledError

    (start,) = await _cut_off(app)
    assert start["status"] == 200
