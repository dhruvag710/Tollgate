"""POST /v1/chat/completions. Phase 1.1: non-streaming passthrough."""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tollgate.config import Settings
from tollgate.deps import get_router, get_settings
from tollgate.errors import TollgateError, error_body
from tollgate.routing.router import Router

router = APIRouter()


class ChatCompletionRequest(BaseModel):
    """Only the fields Tollgate reads; everything else passes through untouched."""

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False


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
) -> JSONResponse:
    body = await read_json_body(request, settings.max_request_body_bytes)
    try:
        req = ChatCompletionRequest.model_validate(body)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err["loc"])
        raise TollgateError(
            400, f"Invalid request: {loc}: {err['msg']}", "invalid_request_error", "invalid_request"
        ) from exc
    if req.stream:
        # Streaming passthrough arrives in phase 1.3.
        raise TollgateError(
            400, "stream=true is not supported yet", "invalid_request_error", "stream_not_supported"
        )

    provider = router_.route(req.model)
    upstream = await provider.chat_completion(body, client_headers=request.headers)
    headers = {"X-Tollgate-Provider": provider.name}

    if upstream.status_code < 400:
        return JSONResponse(upstream.body, status_code=upstream.status_code, headers=headers)

    if upstream.status_code in (401, 403):
        # A provider auth failure is our misconfiguration, not the client's bad key.
        return JSONResponse(
            error_body(
                f"Upstream provider {provider.name!r} rejected Tollgate's credentials",
                "api_error",
                "upstream_auth_failed",
            ),
            status_code=502,
            headers=headers,
        )
    if upstream.retry_after:
        headers["Retry-After"] = upstream.retry_after
    body_out = upstream.body
    if not isinstance(body_out.get("error"), dict):
        body_out = error_body(
            f"Upstream provider {provider.name!r} returned HTTP {upstream.status_code}",
            "api_error",
            "upstream_error",
        )
    return JSONResponse(body_out, status_code=upstream.status_code, headers=headers)
