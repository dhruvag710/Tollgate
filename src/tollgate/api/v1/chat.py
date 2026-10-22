"""POST /v1/chat/completions: streaming and non-streaming passthrough."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tollgate.config import Settings
from tollgate.deps import get_ledger, get_router, get_settings, get_streams
from tollgate.errors import TollgateError, upstream_error_response
from tollgate.providers.base import Provider
from tollgate.ratelimit.estimate import generated_text
from tollgate.routing.router import Router
from tollgate.streaming.sse import OpenStreams, SSEProxyResponse
from tollgate.usage.ledger import Ledger, UsageMeter, parse_usage

router = APIRouter()


class ChatCompletionRequest(BaseModel):
    """Only the fields Tollgate reads; everything else passes through untouched."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None


async def read_json_body(request: Request, limit: int) -> dict[str, Any]:
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
    return body


def _too_large(limit: int) -> TollgateError:
    return TollgateError(
        413, f"Request body exceeds {limit} bytes", "invalid_request_error", "body_too_large"
    )


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    router_: Annotated[Router, Depends(get_router)],
    ledger: Annotated[Ledger, Depends(get_ledger)],
    streams: Annotated[OpenStreams, Depends(get_streams)],
) -> Response:
    body = await read_json_body(request, settings.max_request_body_bytes)
    try:
        req = ChatCompletionRequest.model_validate(body)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err["loc"])
        raise TollgateError(
            400, f"Invalid request: {loc}: {err['msg']}", "invalid_request_error", "invalid_request"
        ) from exc

    provider = router_.route(req.model)
    meter = UsageMeter(
        ledger=ledger,
        request_id=request.state.request_id,
        started_at=request.state.started_at,
        model=req.model,
        provider=provider.name,
        messages=req.messages,
    )
    if not req.stream:
        return await _complete(request, body, provider, meter)

    client_options = req.stream_options or {}
    return SSEProxyResponse(
        provider=provider,
        # Always ask upstream for the final usage chunk; keep the client's other options.
        body={**body, "stream_options": {**client_options, "include_usage": True}},
        client_headers=request.headers,
        forward_usage_chunk=bool(client_options.get("include_usage")),
        meter=meter,
        streams=streams,
    )


async def _complete(
    request: Request, body: dict[str, Any], provider: Provider, meter: UsageMeter
) -> Response:
    try:
        upstream = await provider.chat_completion(body, client_headers=request.headers)
    except TollgateError:
        await meter.record("provider_error")
        raise
    if upstream.status_code >= 400:
        await meter.record("provider_error")
        return upstream_error_response(upstream, provider.name)

    reported = parse_usage(upstream.body.get("usage"))
    generated = None
    if reported is None:
        choices = upstream.body.get("choices")
        generated = "".join(
            generated_text(c.get("message"))
            for c in (choices if isinstance(choices, list) else [])
            if isinstance(c, dict)
        )
    await meter.record("success", reported=reported, generated=generated)
    return JSONResponse(
        upstream.body,
        status_code=upstream.status_code,
        headers={"X-Tollgate-Provider": provider.name},
    )
