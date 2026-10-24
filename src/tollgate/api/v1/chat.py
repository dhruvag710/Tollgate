"""POST /v1/chat/completions: streaming and non-streaming passthrough."""

import json
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth.cache import KeyCache
from tollgate.auth.dependency import authenticate, presented_key
from tollgate.config import Settings
from tollgate.deps import (
    get_key_cache,
    get_limiter,
    get_metering,
    get_router,
    get_sessions,
    get_settings,
)
from tollgate.errors import TollgateError, upstream_error_response
from tollgate.observability.timing import Timings
from tollgate.providers.base import Provider
from tollgate.ratelimit.estimate import generated_text, reservation_tokens
from tollgate.ratelimit.limiter import RateLimiter, Reservation
from tollgate.routing.router import Router, model_not_found
from tollgate.streaming.sse import SSEProxyResponse
from tollgate.usage.ledger import Metering, UsageMeter, parse_usage

router = APIRouter()


class ChatCompletionRequest(BaseModel):
    """Only the fields Tollgate reads; everything else passes through untouched."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    max_tokens: int | None = Field(default=None, ge=0)
    max_completion_tokens: int | None = Field(default=None, ge=0)


# Bodies past this size are tokenized off the event loop; below it a thread hop
# costs more than the estimate itself.
INLINE_ESTIMATE_MAX_BYTES = 32_768


async def read_json_body(request: Request, limit: int) -> tuple[dict[str, Any], int]:
    """The body, and its size in bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise _too_large(limit)
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise TollgateError(
            400, "Request body is not valid JSON", "invalid_request_error", "invalid_json"
        ) from exc
    if not isinstance(body, dict):
        raise TollgateError(
            400, "Request body must be a JSON object", "invalid_request_error", "invalid_json"
        )
    return body, len(raw)


def _too_large(limit: int) -> TollgateError:
    return TollgateError(
        413, f"Request body exceeds {limit} bytes", "invalid_request_error", "body_too_large"
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    router_: Annotated[Router, Depends(get_router)],
    metering: Annotated[Metering, Depends(get_metering)],
    sessions: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessions)],
    cache: Annotated[KeyCache, Depends(get_key_cache)],
    limiter: Annotated[RateLimiter, Depends(get_limiter)],
) -> Response:
    # Before the body is read: a missing or malformed key costs nothing.
    raw_key = presented_key(request)
    timings: Timings = request.state.timings
    with timings.stage("body"):
        body, size = await read_json_body(request, settings.max_request_body_bytes)
        try:
            req = ChatCompletionRequest.model_validate(body)
        except ValidationError as exc:
            err = exc.errors()[0]
            loc = ".".join(str(p) for p in err["loc"])
            raise TollgateError(
                400,
                f"Invalid request: {loc}: {err['msg']}",
                "invalid_request_error",
                "invalid_request",
            ) from exc

    # The reservation must be known before the one Redis call that authenticates
    # and rate limits, so the body is read first.
    with timings.stage("estimate"):
        estimate = partial(
            reservation_tokens,
            req.messages,
            req.model,
            req.max_completion_tokens if req.max_completion_tokens is not None else req.max_tokens,
            settings.tpm_default_max_tokens,
        )
        if size <= INLINE_ESTIMATE_MAX_BYTES:
            reserve = estimate()
        else:
            reserve = await anyio.to_thread.run_sync(estimate)
    tenant, limits = await authenticate(
        request, raw_key, settings, sessions, cache, limiter, reservation=reserve
    )
    assert limits is not None  # asked for with a reservation
    if not limits.allowed:
        await metering.record_rejected(
            request_id=request.state.request_id,
            tenant_id=tenant.tenant_id,
            api_key_id=tenant.api_key_id,
            started_at=request.state.started_at,
            model=req.model,
            status="rate_limited",
        )
        raise limits.error()

    reservation = Reservation(tenant.tenant_id, tenant.tpm_limit, limits.reserved)
    try:
        if not tenant.allows(req.model):
            raise model_not_found(req.model)
        provider = router_.route(req.model)
    except TollgateError:
        metering.release(reservation)
        raise
    meter = metering.meter(
        request_id=request.state.request_id,
        tenant_id=tenant.tenant_id,
        api_key_id=tenant.api_key_id,
        started_at=request.state.started_at,
        model=req.model,
        provider=provider.name,
        messages=req.messages,
        reservation=reservation,  # settled when its usage is recorded
    )
    headers = limits.headers()
    if not req.stream:
        async with meter.measure():
            return await _complete(
                request, body, provider, meter, timings, settings.server_timing, headers
            )

    client_options = req.stream_options or {}
    return SSEProxyResponse(
        provider=provider,
        # Always ask upstream for the final usage chunk; keep the client's other options.
        body={**body, "stream_options": {**client_options, "include_usage": True}},
        client_headers=request.headers,
        forward_usage_chunk=bool(client_options.get("include_usage")),
        meter=meter,
        timings=timings,
        server_timing=settings.server_timing,
        headers=headers,
    )


async def _complete(
    request: Request,
    body: dict[str, Any],
    provider: Provider,
    meter: UsageMeter,
    timings: Timings,
    server_timing: bool,
    extra_headers: dict[str, str],
) -> Response:
    try:
        with timings.stage("upstream-response"):
            upstream = await provider.chat_completion(
                body, client_headers=request.headers, on_sent=meter.mark_sent
            )
    except TollgateError:
        meter.settle("provider_error")
        raise
    if upstream.status_code >= 400:
        meter.reported = (0, 0)
        meter.settle("provider_error")
        return upstream_error_response(upstream, provider.name)

    meter.reported = parse_usage(upstream.body.get("usage"))
    choices = upstream.body.get("choices")
    meter.generated = "".join(
        generated_text(c.get("message"))
        for c in (choices if isinstance(choices, list) else [])
        if isinstance(c, dict)
    )
    headers = {**extra_headers, "X-Tollgate-Provider": provider.name}
    if server_timing:
        headers["Server-Timing"] = timings.server_timing()
    response = JSONResponse(upstream.body, status_code=upstream.status_code, headers=headers)
    meter.settle("success")
    return response
