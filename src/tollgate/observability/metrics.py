"""Prometheus metrics (SPEC Phase 1.7), served at /metrics.

Each chat completion request carries an Observation, filled in as it goes:
the model once the body is read, the provider once routed, and its status:
its usage event's status if it has one (the meter sets it first), otherwise
the code of the error that turned it away. RequestIDMiddleware records it once,
when the request ends. Recording is a few in-memory updates, no I/O; the
ledger's queue depth is only read when /metrics is scraped.

Every label value comes from a fixed set, and anything else is "other", so a
client can't create series by sending made-up model names or by causing new
errors. Metrics are per process: run one uvicorn worker per replica.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from tollgate.db.models import USAGE_STATUSES

# Codes of the errors that turn a chat request away before it has a usage event.
ERROR_STATUSES = (
    "missing_api_key",
    "invalid_api_key",
    "auth_unavailable",
    "invalid_json",
    "invalid_request",
    "body_too_large",
    "model_not_found",
    "unknown_model",
    "internal_error",
)
STATUSES = frozenset((*USAGE_STATUSES, *ERROR_STATUSES))
RATE_LIMITS = {"requests": "rpm", "tokens": "tpm", "too_large": "request_too_large"}
OTHER, NONE = "other", "none"  # not in the set; not known (yet)

CONTENT_TYPE = CONTENT_TYPE_LATEST


@dataclass
class Observation:
    """What the metrics need to know about one chat request."""

    model: str | None = None
    provider: str | None = None
    status: str | None = None  # the first one set wins
    ttft_s: float | None = None
    response_started_at: float | None = None  # time.perf_counter() at the response's start

    def settle(self, status: str | None) -> None:
        if self.status is None:
            self.status = status or OTHER


class Metrics:
    def __init__(self, models: Iterable[str], providers: Iterable[str]) -> None:
        self._models = frozenset(models)
        self._providers = frozenset(providers)
        # One registry per app, not the process-wide default: tests build many.
        self.registry = registry = CollectorRegistry(auto_describe=True)
        self.requests = Counter(
            "tollgate_requests",
            "Chat completion requests, by outcome.",
            ["status", "model", "provider", "cache"],
            registry=registry,
        )
        self.duration = Histogram(
            "tollgate_request_duration_seconds",
            "Chat completion requests, from arrival to the end of the response or stream.",
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
            registry=registry,
        )
        self.overhead = Histogram(
            "tollgate_gateway_overhead_seconds",
            "Time a request that reached a provider spent in the gateway, not waiting on "
            "the provider, until its response started.",
            buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1),
            registry=registry,
        )
        self.ttft = Histogram(
            "tollgate_ttft_seconds",
            "Streamed requests, from arrival to the first token.",
            buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=registry,
        )
        self.ratelimit_rejections = Counter(
            "tollgate_ratelimit_rejections",
            "Requests turned away by a rate limit, by limit.",
            ["limit"],
            registry=registry,
        )
        self.ledger_queue_depth = Gauge(
            "tollgate_ledger_queue_depth",
            "Usage events recorded but not yet written to the ledger.",
            registry=registry,
        )

    def watch_ledger(self, pending: Callable[[], int]) -> None:
        """Read the ledger's queue depth at each scrape."""
        self.ledger_queue_depth.set_function(pending)

    def rate_limited(self, outcome: str) -> None:
        self.ratelimit_rejections.labels(RATE_LIMITS.get(outcome, OTHER)).inc()

    def observe_request(
        self, observation: Observation, started_at: float, ended_at: float, upstream_s: float
    ) -> None:
        o = observation
        status = o.status if o.status in STATUSES else OTHER
        model = NONE if o.model is None else o.model if o.model in self._models else OTHER
        provider = (
            NONE if o.provider is None else o.provider if o.provider in self._providers else OTHER
        )
        self.requests.labels(status, model, provider, "MISS").inc()  # no cache until v2
        self.duration.observe(ended_at - started_at)
        if upstream_s > 0 and o.response_started_at is not None:
            self.overhead.observe(max(0.0, o.response_started_at - started_at - upstream_s))
        if o.ttft_s is not None:
            self.ttft.observe(o.ttft_s)

    def exposition(self) -> bytes:
        return generate_latest(self.registry)
