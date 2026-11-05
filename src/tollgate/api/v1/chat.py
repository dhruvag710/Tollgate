"""POST /v1/chat/completions: streaming and non-streaming passthrough, with
failover across the model's providers (SPEC Phase 2.1) and answers from the
exact-match cache (Phase 2.3) and the semantic cache (Phase 2.4)."""

import json
from functools import partial
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.auth.cache import KeyCache
from tollgate.auth.context import TenantContext
from tollgate.auth.dependency import authenticate, presented_key
from tollgate.cache import exact, policy
from tollgate.cache.replay import fresh
from tollgate.cache.semantic import SemanticCache, question_of
from tollgate.config import Settings
from tollgate.deps import (
    get_budgets,
    get_key_cache,
    get_limiter,
    get_metering,
    get_metrics,
    get_router,
    get_semantic,
    get_sessions,
    get_settings,
)
from tollgate.errors import TollgateError, upstream_error_response
from tollgate.observability.metrics import Metrics, Observation
from tollgate.observability.timing import Timings
from tollgate.providers.base import UpstreamError
from tollgate.ratelimit.estimate import generated_text, reservation_tokens
from tollgate.ratelimit.limiter import RateLimiter, Reservation
from tollgate.routing.router import (
    RoutePlan,
    Router,
    error_result,
    fails_over_error,
    fails_over_status,
    model_not_found,
    status_result,
)
from tollgate.streaming.sse import ReplayResponse, SSEProxyResponse
from tollgate.usage.budget import Budgets
from tollgate.usage.ledger import CacheStatus, Metering, UsageMeter, parse_usage

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
    budgets: Annotated[Budgets, Depends(get_budgets)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
    semantic: Annotated[SemanticCache | None, Depends(get_semantic)],
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

    observation: Observation = request.state.observation
    observation.model = req.model
    # The reservation must be known before the one Redis call that authenticates,
    # budget checks and rate limits, so the body is read first.
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
    # The exact-cache key of a request the cacheability policy allows; the
    # tenant's own setting is checked in the admission call. Hashing a large
    # body, like estimating it, is done off the event loop.
    cache_key = ""
    if policy.cacheable(body, request.headers, settings.cache_max_temperature):
        with timings.stage("cache-key"):
            if size <= INLINE_ESTIMATE_MAX_BYTES:
                cache_key = policy.exact_key(body)
            else:
                cache_key = await anyio.to_thread.run_sync(policy.exact_key, body)
    # The same call reads the breakers of the model's providers, for routing,
    # and the request's exact-cache entry.
    tenant, limits = await authenticate(
        request,
        raw_key,
        settings,
        sessions,
        cache,
        limiter,
        budgets=budgets,
        reservation=reserve,
        routes=router_.routes(req.model),
        cache_key=cache_key,
    )
    assert limits is not None  # asked for with a reservation
    if not limits.allowed:
        if limits.outcome != "budget":
            metrics.rate_limited(limits.outcome)
        await metering.record_rejected(
            request_id=request.state.request_id,
            tenant_id=tenant.tenant_id,
            api_key_id=tenant.api_key_id,
            started_at=request.state.started_at,
            model=req.model,
            status="budget_exceeded" if limits.outcome == "budget" else "rate_limited",
            observation=observation,
        )
        raise limits.error()

    reservation = Reservation(tenant.tenant_id, tenant.tpm_limit, limits.reserved)
    if not tenant.allows(req.model) or not router_.routes(req.model):
        metering.release(reservation)  # never reached a provider: no usage event
        raise model_not_found(req.model)  # even if an old cache entry has it

    consulted = bool(cache_key) and tenant.cache_enabled
    cached = exact.decode(limits.cached) if consulted and limits.cached else None
    if consulted:
        metrics.cache_lookup("exact", "hit" if cached is not None else "miss")
    # The exact layer missed: ask the semantic one, by the last user message.
    question = question_of(body) if consulted and cached is None and semantic else None
    found = None
    if question is not None:
        assert semantic is not None
        with timings.stage("cache-semantic"):
            found = await semantic.find(tenant.tenant_id, question)
        metrics.semantic_lookup(found.completion is not None, found.embedding, found.similarity)
        if found.completion is not None:
            assert found.entry_id is not None
            semantic.touch(found.entry_id)
            cached = found.completion
    if cached is not None:
        observation.cache = "HIT"
        return await _answer_from_cache(
            request, req, tenant, cached, metering, reservation, observation, limits.headers()
        )
    cache_status: CacheStatus = "MISS" if consulted else "BYPASS"
    observation.cache = cache_status

    plan = router_.plan(
        req.model, limits.breakers, request_id=request.state.request_id, observation=observation
    )
    provider = await plan.next()
    if provider is None:
        metering.release(reservation)  # never reached a provider: no usage event
        raise plan.unavailable()
    meter = metering.meter(
        request_id=request.state.request_id,
        tenant_id=tenant.tenant_id,
        api_key_id=tenant.api_key_id,
        started_at=request.state.started_at,
        model=req.model,
        provider=provider.name,
        messages=req.messages,
        reservation=reservation,  # settled when its usage is recorded
        observation=observation,
    )
    meter.finish = plan.finish
    meter.cache_status = cache_status
    if consulted:
        meter.cache_key = cache_key  # a miss: store the answer, once complete
    if question is not None and found is not None and found.embedding is not None:
        assert semantic is not None  # and in the semantic layer, by this embedding
        meter.semantic_store = partial(semantic.store, tenant.tenant_id, question, found.embedding)
    headers = {**limits.headers(), "X-Tollgate-Cache": cache_status}
    if not req.stream:
        async with meter.measure():
            return await _complete(
                request, body, plan, meter, timings, settings.server_timing, headers
            )

    client_options = req.stream_options or {}
    return SSEProxyResponse(
        plan=plan,
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
    plan: RoutePlan,
    meter: UsageMeter,
    timings: Timings,
    server_timing: bool,
    extra_headers: dict[str, str],
) -> Response:
    """Try the plan's providers in turn until one answers or none is left.
    Nothing reaches the client before an answer, so any failure the policy
    allows may still be retried."""
    provider = plan.current
    assert provider is not None, "the plan has a first provider"
    while True:
        meter.begin_attempt(provider.name)
        try:
            with timings.stage("upstream-response"):
                upstream = await provider.chat_completion(
                    body, client_headers=request.headers, on_sent=meter.mark_sent
                )
        except UpstreamError as exc:
            plan.answered(error_result(exc))
            nxt = await plan.next() if fails_over_error(exc) else None
            if nxt is None:
                meter.settle("provider_error")
                raise
            provider = nxt
            continue
        plan.answered(status_result(upstream.status_code))
        if upstream.status_code < 400:
            break
        meter.reported = (0, 0)  # an error response: nothing billed
        nxt = await plan.next() if fails_over_status(upstream.status_code) else None
        if nxt is None:
            meter.settle("provider_error")
            return upstream_error_response(upstream, provider.name)
        provider = nxt

    meter.reported = parse_usage(upstream.body.get("usage"))
    choices = upstream.body.get("choices")
    meter.generated = "".join(
        generated_text(c.get("message"))
        for c in (choices if isinstance(choices, list) else [])
        if isinstance(c, dict)
    )
    if meter.cache_key is not None and exact.storable(upstream.body):
        meter.cache_completion = upstream.body
    headers = {**extra_headers, "X-Tollgate-Provider": provider.name}
    if server_timing:
        headers["Server-Timing"] = timings.server_timing()
    response = JSONResponse(upstream.body, status_code=upstream.status_code, headers=headers)
    meter.settle("success")
    return response


async def _answer_from_cache(
    request: Request,
    req: ChatCompletionRequest,
    tenant: TenantContext,
    cached: dict[str, Any],
    metering: Metering,
    reservation: Reservation,
    observation: Observation,
    limit_headers: dict[str, str],
) -> Response:
    """A cache hit: the cached completion, as JSON or replayed as a stream. It
    reached no provider, so it records a HIT event of its own: nothing billed,
    its TPM reservation given back (SPEC Phase 2.3)."""
    headers = {**limit_headers, "X-Tollgate-Cache": "HIT"}
    usage = parse_usage(cached.get("usage"))
    assert usage is not None, "only completions with usage are cached"
    record = partial(
        metering.record_hit,
        request_id=request.state.request_id,
        tenant_id=tenant.tenant_id,
        api_key_id=tenant.api_key_id,
        started_at=request.state.started_at,
        model=req.model,
        usage=usage,
        reservation=reservation,
        observation=observation,
    )
    if req.stream:
        include_usage = bool((req.stream_options or {}).get("include_usage"))
        return ReplayResponse(cached, include_usage=include_usage, record=record, headers=headers)
    response = JSONResponse(fresh(cached), headers=headers)
    await record()
    return response
