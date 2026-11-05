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
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from tollgate.db.models import CACHE_STATUSES, USAGE_STATUSES
from tollgate.observability.timing import Timings
from tollgate.routing.router import ATTEMPT_RESULTS

if TYPE_CHECKING:
    from tollgate.cache.pool import ModelPool
    from tollgate.cache.semantic import Found

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
    "no_provider_available",
    "internal_error",
)
STATUSES = frozenset((*USAGE_STATUSES, *ERROR_STATUSES))
RATE_LIMITS = {"requests": "rpm", "tokens": "tpm", "too_large": "request_too_large"}
BREAKER_STATES = frozenset({"closed", "open", "half_open"})
OTHER, NONE = "other", "none"  # not in the set; not known (yet)

CONTENT_TYPE = CONTENT_TYPE_LATEST


@dataclass
class Observation:
    """What the metrics need to know about one chat request."""

    model: str | None = None
    provider: str | None = None
    status: str | None = None  # the first one set wins
    # HIT, MISS or BYPASS (SPEC Phase 2.3): BYPASS until the cache is consulted.
    cache: str = "BYPASS"
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
        self.provider_attempts = Counter(
            "tollgate_provider_attempts",
            "Attempts at a provider, by result: one per provider a request tried, unless "
            "the request ended (its client left) before the attempt had a result.",
            ["provider", "result"],
            registry=registry,
        )
        self.cache_lookups = Counter(
            "tollgate_cache_lookups",
            "Response cache lookups, by layer (exact, semantic) and result: hit or miss, "
            "and for the semantic layer also rejected (its check turned the candidate "
            "down), full (a model's pool had no room), timeout and error; each of the "
            "last three counts as a miss that stores nothing.",
            ["layer", "result"],
            registry=registry,
        )
        self.semantic_similarity = Histogram(
            "tollgate_semantic_cache_similarity",
            "Semantic lookups that found an entry in scope: its cosine similarity to the "
            "question, hit or not. Where the threshold falls among real questions.",
            buckets=(0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.92, 0.94, 0.96, 0.98, 0.99, 1.0),
            registry=registry,
        )
        self.semantic_score = Histogram(
            "tollgate_semantic_cache_check_score",
            "Semantic lookups whose candidate was checked: the check's score, hit or not. "
            "Where the verifier's threshold falls among real questions.",
            buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.99, 1.0),
            registry=registry,
        )
        self.semantic_seconds = Histogram(
            "tollgate_semantic_cache_seconds",
            "Semantic lookups, by stage: miss_path (embedding the question, the wait for "
            "the model included, then the Postgres lookup), and check (the candidate's "
            "check, its wait included).",
            ["stage"],
            buckets=(0.0025, 0.005, 0.01, 0.015, 0.02, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
            registry=registry,
        )
        self.semantic_in_flight = Gauge(
            "tollgate_semantic_cache_in_flight",
            "Calls running or waiting in each semantic cache model's pool (embed, verify).",
            ["pool"],
            registry=registry,
        )
        self.breaker_transitions = Counter(
            "tollgate_breaker_transitions",
            "Circuit breaker state changes this replica made, by the state entered. The "
            "breakers are shared: sum over replicas for a provider's whole history.",
            ["provider", "state"],
            registry=registry,
        )

    def watch_ledger(self, pending: Callable[[], int]) -> None:
        """Read the ledger's queue depth at each scrape."""
        self.ledger_queue_depth.set_function(pending)

    def rate_limited(self, outcome: str) -> None:
        self.ratelimit_rejections.labels(RATE_LIMITS.get(outcome, OTHER)).inc()

    def attempt(self, provider: str, result: str) -> None:
        self.provider_attempts.labels(
            self._provider(provider), result if result in ATTEMPT_RESULTS else OTHER
        ).inc()

    def cache_lookup(self, layer: str, result: str) -> None:
        self.cache_lookups.labels(layer, result).inc()

    def watch_semantic(self, *pools: "ModelPool") -> None:
        """Read the semantic cache's pools at each scrape."""
        for pool in pools:
            self.semantic_in_flight.labels(pool.name).set_function(pool.in_flight)

    def semantic_lookup(self, found: "Found", timings: Timings) -> None:
        """A semantic lookup: how it ended, its scores, and its stages' times."""
        self.cache_lookup("semantic", found.outcome)
        if found.similarity is not None:
            self.semantic_similarity.observe(found.similarity)
        if found.score is not None:
            self.semantic_score.observe(found.score)
        if found.stage not in ("embed", "lookup"):  # the lookup ran
            miss_path = sum(
                timings.seconds(f"semantic-{s}") for s in ("embed-wait", "embed", "lookup")
            )
            self.semantic_seconds.labels("miss_path").observe(miss_path)
        if found.score is not None or (found.stage == "verify" and found.outcome != "full"):
            check = timings.seconds("semantic-verify-wait") + timings.seconds("semantic-verify")
            self.semantic_seconds.labels("check").observe(check)

    def breaker_transition(self, provider: str, state: str) -> None:
        self.breaker_transitions.labels(
            self._provider(provider), state if state in BREAKER_STATES else OTHER
        ).inc()

    def _provider(self, provider: str | None) -> str:
        if provider is None:
            return NONE
        return provider if provider in self._providers else OTHER

    def observe_request(
        self, observation: Observation, started_at: float, ended_at: float, upstream_s: float
    ) -> None:
        o = observation
        status = o.status if o.status in STATUSES else OTHER
        model = NONE if o.model is None else o.model if o.model in self._models else OTHER
        # The provider that answered: with failover, the last one tried.
        cache = o.cache if o.cache in CACHE_STATUSES else OTHER
        self.requests.labels(status, model, self._provider(o.provider), cache).inc()
        self.duration.observe(ended_at - started_at)
        if upstream_s > 0 and o.response_started_at is not None:
            self.overhead.observe(max(0.0, o.response_started_at - started_at - upstream_s))
        if o.ttft_s is not None:
            self.ttft.observe(o.ttft_s)

    def exposition(self) -> bytes:
        return generate_latest(self.registry)
