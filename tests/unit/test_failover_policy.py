"""Failover policy and routing (SPEC Phase 2.1), without Redis: which results
fail over and which count against a breaker, how a request's plan walks its
providers, and how one usage event covers several attempts."""

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from tests.helpers import MemoryLedger
from tollgate.config import BreakerConfig, ProviderConfig, ProvidersFile
from tollgate.observability.metrics import Metrics, Observation
from tollgate.providers.base import UpstreamError
from tollgate.ratelimit.estimate import estimate_prompt_tokens
from tollgate.routing.circuit_breaker import (
    BreakerSettings,
    BreakerSuccess,
    BreakerView,
    parse_views,
)
from tollgate.routing.router import (
    FAILURES,
    RoutePlan,
    error_result,
    fails_over_error,
    fails_over_status,
    status_result,
)
from tollgate.usage.ledger import Metering


@pytest.mark.parametrize(
    ("status", "result", "fails_over"),
    [
        (200, "success", False),
        (400, "client_error", False),
        (404, "client_error", False),
        (413, "client_error", False),
        (422, "client_error", False),
        (401, "auth_failed", True),  # the provider refused Tollgate's own key
        (403, "auth_failed", True),
        (429, "rate_limited", True),
        (500, "server_error", True),
        (502, "server_error", True),
        (503, "server_error", True),
    ],
)
def test_statuses_fail_over_unless_the_client_is_at_fault(
    status: int, result: str, fails_over: bool
) -> None:
    assert status_result(status) == result
    assert fails_over_status(status) is fails_over
    assert (result in FAILURES) is (result not in ("success", "client_error"))


@pytest.mark.parametrize(
    ("kind", "result"),
    [
        ("unreachable", "connect_error"),
        ("dropped", "connect_error"),  # lost after a stream's headers, before its body
        ("timeout", "timeout"),
        ("bad_response", "bad_response"),
    ],
)
def test_only_failures_before_the_first_byte_fail_over(kind: Any, result: str) -> None:
    before = UpstreamError("p", kind, first_byte=False)
    after = UpstreamError("p", kind, first_byte=True)
    assert error_result(before) == error_result(after) == result
    assert result in FAILURES
    assert fails_over_error(before) and not fails_over_error(after)
    assert before.headers == {"X-Tollgate-Provider": "p"}


# --- config ------------------------------------------------------------------------------


def providers_file(**breaker: Any) -> ProvidersFile:
    return ProvidersFile.model_validate(
        {
            "providers": [
                {"name": "a", "type": "openai_compat", "base_url": "http://a/v1"},
                {
                    "name": "b",
                    "type": "openai_compat",
                    "base_url": "http://b/v1",
                    "timeout_s": 20,
                    "connect_timeout_s": 2,
                    "first_byte_timeout_s": 5,
                    "breaker": {"cooldown_s": 60, "failure_ratio": 0.25},
                },
            ],
            "models": {"m": {"routes": ["a", "b"], "price_per_1m": {"input": 1, "output": 1}}},
            **({"breaker": breaker} if breaker else {}),
        }
    )


def test_a_provider_overrides_the_files_breaker_defaults() -> None:
    config = providers_file(window_s=5)
    a, b = config.providers
    assert config.breaker_for(a) == BreakerConfig(window_s=5)
    assert config.breaker_for(b) == BreakerConfig(window_s=5, cooldown_s=60, failure_ratio=0.25)
    # SPEC Phase 2.1's defaults.
    assert BreakerConfig() == BreakerConfig(
        window_s=10, min_requests=20, failure_ratio=0.5, cooldown_s=30
    )


def test_breaker_settings_in_the_units_the_scripts_take() -> None:
    config = providers_file()
    a, b = config.providers
    assert BreakerSettings.of(config.breaker_for(b), b) == BreakerSettings(
        window_ms=10_000,
        min_requests=20,
        failure_ratio_ppm=250_000,
        cooldown_ms=60_000,
        probe_lease_ms=(2 + 5 + 1) * 1000,  # connect + first byte + a second
    )
    # Without a first-byte timeout the read timeout bounds the first byte.
    assert BreakerSettings.of(config.breaker_for(a), a).probe_lease_ms == (3 + 60 + 1) * 1000


@pytest.mark.parametrize(
    "bad",
    [{"failure_ratio": 0}, {"failure_ratio": 1.5}, {"min_requests": 0}, {"window_s": 0}],
)
def test_breaker_settings_are_validated(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        providers_file(**bad)


def test_a_model_cannot_route_to_a_provider_twice() -> None:
    with pytest.raises(ValidationError, match="twice"):
        ProvidersFile.model_validate(
            {
                "providers": [{"name": "a", "type": "openai_compat", "base_url": "http://a"}],
                "models": {"m": {"routes": ["a", "a"], "price_per_1m": {"input": 1, "output": 1}}},
            }
        )


def test_views_parse_from_the_admission_reply() -> None:
    assert parse_views(["closed", 0, "open", 2500, "probe", 0]) == (
        BreakerView("closed"),
        BreakerView("open", 2.5),
        BreakerView("probe"),
    )
    assert ProviderConfig(name="x", type="openai_compat", base_url="u").first_byte_timeout_s is None


# --- the plan ----------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeBreakers:
    """Records what a plan reports; `claims` decides who wins a probe."""

    def __init__(self, claims: Callable[[str], bool] = lambda _: True) -> None:
        self._claims = claims
        self.calls: list[tuple[str, str]] = []

    async def claim(self, provider: str, request_id: str) -> bool:
        self.calls.append(("claim", provider))
        return self._claims(provider)

    def failed(self, provider: str, request_id: str) -> None:
        self.calls.append(("failed", provider))

    def probe_succeeded(self, provider: str, request_id: str) -> None:
        self.calls.append(("probe_succeeded", provider))

    def release(self, provider: str, request_id: str) -> None:
        self.calls.append(("release", provider))

    def success(self, provider: str) -> BreakerSuccess:
        return BreakerSuccess(provider, 10_000)


def plan(
    *views: BreakerView, breakers: FakeBreakers | None = None
) -> tuple[RoutePlan, FakeBreakers, Metrics, Observation]:
    names = [chr(ord("a") + i) for i in range(len(views))]
    breakers = breakers or FakeBreakers()
    metrics, observation = Metrics(["m"], names), Observation()
    candidates = [(FakeProvider(n), v) for n, v in zip(names, views, strict=True)]
    route = RoutePlan(candidates, breakers, metrics, "req-1", observation)  # type: ignore[arg-type]
    return route, breakers, metrics, observation


def attempts(metrics: Metrics) -> dict[tuple[str, str], float]:
    return {
        (s.labels["provider"], s.labels["result"]): s.value
        for family in metrics.registry.collect()
        if family.name == "tollgate_provider_attempts"
        for s in family.samples
        if s.name.endswith("_total")
    }


CLOSED, OPEN, PROBE = BreakerView("closed"), BreakerView("open", 7.2), BreakerView("probe")


async def test_providers_come_in_priority_order_skipping_open_breakers() -> None:
    route, breakers, metrics, observation = plan(OPEN, CLOSED, CLOSED)
    assert (await route.next()).name == "b"  # type: ignore[union-attr]
    assert observation.provider == "b" and route.attempts == 1
    route.answered("server_error")
    assert (await route.next()).name == "c"  # type: ignore[union-attr]
    route.answered("success")
    assert route.finish("success") == BreakerSuccess("c", 10_000)
    assert breakers.calls == [("failed", "b")]  # no claims: nothing was due a probe
    assert attempts(metrics) == {("b", "server_error"): 1, ("c", "success"): 1}
    assert route.attempts == 2


async def test_when_every_breaker_is_open_nothing_is_tried() -> None:
    route, breakers, _, _ = plan(OPEN, BreakerView("open", 0.3))
    assert await route.next() is None
    error = route.unavailable()
    assert (error.status_code, error.code) == (503, "no_provider_available")
    assert error.headers["Retry-After"] == "1"  # the soonest, rounded up
    assert breakers.calls == []


async def test_a_probe_is_claimed_and_its_verdict_reported_at_once() -> None:
    route, breakers, _, _ = plan(PROBE, CLOSED)
    assert (await route.next()).name == "a"  # type: ignore[union-attr]
    route.answered("success")  # the response started: the verdict
    assert breakers.calls == [("claim", "a"), ("probe_succeeded", "a")]
    # Nothing left to release when the request ends.
    assert route.finish("client_disconnect") is None
    assert breakers.calls == [("claim", "a"), ("probe_succeeded", "a")]


async def test_a_failed_probe_is_a_failure_and_the_request_moves_on() -> None:
    route, breakers, _, _ = plan(PROBE, CLOSED)
    await route.next()
    route.answered("timeout")
    assert (await route.next()).name == "b"  # type: ignore[union-attr]
    assert breakers.calls == [("claim", "a"), ("failed", "a")]


async def test_a_lost_claim_skips_the_provider() -> None:
    route, breakers, _, _ = plan(PROBE, breakers=FakeBreakers(lambda _: False))
    assert await route.next() is None
    assert route.unavailable().headers["Retry-After"] == "1"  # someone else is probing
    assert breakers.calls == [("claim", "a")]


async def test_a_probe_that_never_answers_is_released() -> None:
    route, breakers, _, _ = plan(PROBE)
    await route.next()
    assert route.finish("client_disconnect") is None  # the client left first
    assert breakers.calls == [("claim", "a"), ("release", "a")]


@pytest.mark.parametrize(
    ("result", "status", "counted"),
    [
        ("success", "success", True),
        ("client_error", "provider_error", True),  # the provider is up
        ("success", "client_disconnect", False),  # neither: the client left
        ("success", "gateway_shutdown", False),
    ],
)
async def test_what_the_settle_call_counts_as_a_success(
    result: Any, status: str, counted: bool
) -> None:
    route, breakers, _, _ = plan(CLOSED)
    await route.next()
    route.answered(result)
    assert (route.finish(status) is not None) is counted
    assert breakers.calls == []


async def test_a_stream_that_breaks_counts_as_a_failure_not_a_success() -> None:
    route, breakers, metrics, _ = plan(CLOSED)
    await route.next()
    route.answered("success")
    route.broke()
    assert route.finish("provider_error") is None
    assert breakers.calls == [("failed", "a")]
    assert attempts(metrics) == {("a", "stream_error"): 1}  # each attempt counted once


# --- one usage event for every attempt ---------------------------------------------------

MESSAGES = [{"role": "user", "content": "how many tokens is this prompt anyway"}]


async def test_one_event_sums_every_attempt_a_provider_may_bill() -> None:
    ledger = MemoryLedger()
    meter = Metering(ledger).meter(
        request_id="00000000-0000-0000-0000-000000000001",
        tenant_id="t",
        api_key_id="k",
        started_at=0.0,
        model="gpt-4o-mini",
        provider="a",
        messages=MESSAGES,
    )
    async with meter.measure():
        meter.begin_attempt("a")  # refused at once: never sent, nothing billed
        meter.begin_attempt("b")  # sent, then timed out: it may bill the prompt
        meter.mark_sent()
        meter.begin_attempt("c")  # an error status: nothing billed
        meter.mark_sent()
        meter.reported = (0, 0)
        meter.begin_attempt("d")  # answered in full
        meter.mark_sent()
        meter.reported = (20, 7)
        meter.settle("success")
    (event,) = ledger.events
    prompt = estimate_prompt_tokens(MESSAGES, "gpt-4o-mini")
    assert (event.provider, event.attempts, event.status) == ("d", 4, "success")
    assert (event.prompt_tokens, event.completion_tokens) == (20 + prompt, 7)
    assert event.usage_estimated  # part of it is an estimate


async def test_a_single_attempt_records_as_before() -> None:
    ledger = MemoryLedger()
    meter = Metering(ledger).meter(
        request_id="00000000-0000-0000-0000-000000000002",
        tenant_id="t",
        api_key_id="k",
        started_at=0.0,
        model="gpt-4o-mini",
        provider="a",
        messages=MESSAGES,
    )
    async with meter.measure():
        meter.begin_attempt("a")
        meter.mark_sent()
        meter.reported = (20, 7)
        meter.settle("success")
    (event,) = ledger.events
    assert (event.attempts, event.prompt_tokens, event.completion_tokens) == (1, 20, 7)
    assert not event.usage_estimated
