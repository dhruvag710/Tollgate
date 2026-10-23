"""OpenAI-shaped error responses, so client SDKs parse them natively."""

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate.providers.base import UpstreamResponse

log = logging.getLogger(__name__)


class TollgateError(Exception):
    def __init__(
        self,
        status_code: int,
        message: str,
        type_: str,
        code: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type_
        self.code = code
        self.headers = dict(headers or {})


def error_body(message: str, type_: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": None, "code": code}}


def internal_error_message(request_id: str | None) -> str:
    """What a client is told about an internal error: nothing but how to report it."""
    return f"Tollgate hit an internal error. Request ID: {request_id}"


def error_response(exc: TollgateError) -> JSONResponse:
    return JSONResponse(
        error_body(exc.message, exc.type, exc.code),
        status_code=exc.status_code,
        headers=exc.headers,
    )


def upstream_error_response(upstream: UpstreamResponse, provider: str) -> JSONResponse:
    """Relay an upstream error status to the client in OpenAI shape."""
    headers = {"X-Tollgate-Provider": provider}
    if upstream.status_code in (401, 403):
        # A provider auth failure is our misconfiguration, not the client's bad key.
        return JSONResponse(
            error_body(
                f"Upstream provider {provider!r} rejected Tollgate's credentials",
                "api_error",
                "upstream_auth_failed",
            ),
            status_code=502,
            headers=headers,
        )
    if upstream.retry_after:
        headers["Retry-After"] = upstream.retry_after
    body = upstream.body
    if not isinstance(body.get("error"), dict):
        body = error_body(
            f"Upstream provider {provider!r} returned HTTP {upstream.status_code}",
            "api_error",
            "upstream_error",
        )
    return JSONResponse(body, status_code=upstream.status_code, headers=headers)


def _type_for_status(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 404:
        return "not_found_error"
    if status_code >= 500:
        return "api_error"
    return "invalid_request_error"


async def _tollgate_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, TollgateError)
    return error_response(exc)


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(error_body(str(exc), "invalid_request_error"), status_code=400)


async def _http_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return JSONResponse(
        error_body(str(exc.detail), _type_for_status(exc.status_code)),
        status_code=exc.status_code,
        headers=exc.headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(TollgateError, _tollgate_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)


class InternalErrorMiddleware:
    """Turns an unhandled exception into an OpenAI-shaped 500, and logs it with
    its traceback under the request id. The response body carries neither the
    traceback nor the exception's message. Must run inside RequestIDMiddleware.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            request_id = scope.get("state", {}).get("request_id")
            log.exception(
                "internal error, request_id=%s", request_id, extra={"request_id": request_id}
            )
            if started:
                return  # too late for a status code; a stream ends itself with an error event
            body = error_body(internal_error_message(request_id), "api_error", "internal_error")
            await JSONResponse(body, status_code=500)(scope, receive, send)
