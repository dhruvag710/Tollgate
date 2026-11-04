"""Model -> provider routing (request lifecycle step 6) and failover (step 7),
SPEC Phase 2.1.

A request's RoutePlan hands out its model's providers in priority order,
skipping those whose circuit breaker is open. The breakers come from the
request's admission call, so routing costs no Redis round trip. Only a
provider whose cooldown is over costs one: the call that claims its probe,
which one request across all replicas wins.

Each attempt ends in a result. A failure counts against the provider's breaker,
and fails over to the next provider only when nothing of the response has
arrived yet, and so nothing has reached the client either. A success or a
client error (the provider is up) counts in the request's settle call, which
it makes anyway. A probe's verdict is reported as soon as it is known.
"""

import math
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, get_args

from tollgate.errors import TollgateError
from tollgate.providers.base import Provider, UpstreamError
from tollgate.providers.registry import ProviderRegistry
from tollgate.routing.circuit_breaker import CLOSED, Breakers, BreakerSuccess, BreakerView

if TYPE_CHECKING:  # metrics imports this module's results
    from tollgate.observability.metrics import Metrics, Observation

AttemptResult = Literal[
    "success",
    "client_error",  # the provider refused the request itself (4xx): it is up
    "connect_error",  # unreachable, or the connection was lost
    "timeout",
    "server_error",  # 5xx
    "rate_limited",  # 429: the provider is limiting Tollgate's account
    "auth_failed",  # 401/403: the provider refused Tollgate's own key
    "bad_response",  # success, with a body that isn't one
    "stream_error",  # a stream that broke after it started
]
ATTEMPT_RESULTS: tuple[AttemptResult, ...] = get_args(AttemptResult)
# Results that count against a provider's breaker.
FAILURES = frozenset(r for r in ATTEMPT_RESULTS if r not in ("success", "client_error"))
# Error statuses that are complete answers, nothing billed, so another
# provider may try.
_RETRIED_STATUSES = frozenset({"server_error", "rate_limited", "auth_failed"})


def model_not_found(model: str) -> TollgateError:
    """Also the answer for a model the tenant may not use: no hint that it exists."""
    return TollgateError(
        404,
        f"The model `{model}` does not exist or you do not have access to it.",
        "invalid_request_error",
        "model_not_found",
    )


def status_result(status_code: int) -> AttemptResult:
    """What an upstream HTTP status says about the attempt."""
    if status_code < 400:
        return "success"
    if status_code in (401, 403):
        return "auth_failed"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "client_error"


def error_result(exc: UpstreamError) -> AttemptResult:
    if exc.kind == "timeout":
        return "timeout"
    if exc.kind == "bad_response":
        return "bad_response"
    return "connect_error"


def fails_over_status(status_code: int) -> bool:
    """Whether an error status may be retried on the next provider: 5xx, 429
    and 401/403, never another 4xx (the client's own error)."""
    return status_result(status_code) in _RETRIED_STATUSES


def fails_over_error(exc: UpstreamError) -> bool:
    """Whether a failure may be retried on the next provider: only one before
    the provider's first byte, which comes before the client's."""
    return not exc.first_byte


class Router:
    def __init__(self, registry: ProviderRegistry, breakers: Breakers, metrics: "Metrics") -> None:
        self.registry = registry
        self.breakers = breakers
        self.metrics = metrics

    def routes(self, model: str) -> list[str]:
        """The model's providers in priority order; none for an unknown model."""
        cfg = self.registry.models.get(model)
        return [] if cfg is None else list(cfg.routes)

    def plan(
        self,
        model: str,
        breakers: Sequence[BreakerView],
        *,
        request_id: str,
        observation: "Observation | None" = None,
    ) -> "RoutePlan":
        """`breakers`: the views of routes(model), from the admission call."""
        names = self.routes(model)
        if not names:
            raise model_not_found(model)
        candidates = [
            (self.registry.providers[name], breakers[i] if i < len(breakers) else CLOSED)
            for i, name in enumerate(names)
        ]
        return RoutePlan(candidates, self.breakers, self.metrics, request_id, observation)


class RoutePlan:
    """One request's way through its model's providers."""

    def __init__(
        self,
        candidates: Sequence[tuple[Provider, BreakerView]],
        breakers: Breakers,
        metrics: "Metrics",
        request_id: str,
        observation: "Observation | None",
    ) -> None:
        self._pending = deque(candidates)
        self._breakers = breakers
        self._metrics = metrics
        self._request_id = request_id
        self._observation = observation
        self._waits = [view.wait_s for _, view in candidates if view.state == "open"]
        self.current: Provider | None = None
        self.attempts = 0
        self._probe = False  # this request holds the current provider's probe
        self._result: AttemptResult | None = None  # the current attempt's, once known

    async def next(self) -> Provider | None:
        """The next provider to try, or None if none is left. Ends the attempt
        before, which must have failed."""
        if self.current is not None:
            self._close_attempt()
        while self._pending:
            provider, view = self._pending.popleft()
            if view.state == "open":
                continue
            probe = view.state == "probe"
            if probe and not await self._breakers.claim(provider.name, self._request_id):
                self._waits.append(1.0)  # someone else is probing; it won't be long
                continue
            self.current, self._probe, self._result = provider, probe, None
            self.attempts += 1
            if self._observation is not None:
                self._observation.provider = provider.name
            return provider
        self.current = None
        return None

    def unavailable(self) -> TollgateError:
        """The answer when no provider can be tried: every breaker is open."""
        wait = max(1, math.ceil(min(self._waits, default=1.0)))
        return TollgateError(
            503,
            "No provider is available for this model right now; retry the request.",
            "api_error",
            "no_provider_available",
            headers={"Retry-After": str(wait)},
        )

    def answered(self, result: AttemptResult) -> None:
        """The current attempt has its result: a response (success, an error
        status) or a failure before one."""
        assert self.current is not None, "no attempt in progress"
        self._result = result
        if result in FAILURES:
            self._breakers.failed(self.current.name, self._request_id)
            self._probe = False  # its verdict is in
        elif self._probe:
            self._breakers.probe_succeeded(self.current.name, self._request_id)
            self._probe = False

    def broke(self) -> None:
        """The current attempt's stream failed after it had started."""
        self.answered("stream_error")

    def finish(self, status: str) -> BreakerSuccess | None:
        """End the request, whose usage status is `status`. Returns the success
        its settle call should count against the provider's breaker, if any."""
        if self.current is None:
            return None
        provider, result, probe = self.current, self._result, self._probe
        self._close_attempt()
        self.current = None
        if probe:
            # Claimed, never answered: the client left, or the gateway failed.
            self._breakers.release(provider.name, self._request_id)
        if result == "client_error" or (result == "success" and status == "success"):
            return self._breakers.success(provider.name)
        return None

    def _close_attempt(self) -> None:
        assert self.current is not None
        if self._result is not None:
            self._metrics.attempt(self.current.name, self._result)
