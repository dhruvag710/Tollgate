import time

import pytest

from tollgate.observability.metrics import Metrics, Observation


def metrics() -> Metrics:
    return Metrics(models=["gpt-4o-mini"], providers=["mock"])


def value(m: Metrics, name: str, **labels: str) -> float | None:
    return m.registry.get_sample_value(name, labels)


def test_a_request_is_counted_under_its_labels() -> None:
    m = metrics()
    o = Observation(model="gpt-4o-mini", provider="mock", status="success")
    m.observe_request(o, started_at=10.0, ended_at=10.5, upstream_s=0.0)
    labels = {"status": "success", "model": "gpt-4o-mini", "provider": "mock", "cache": "MISS"}
    assert value(m, "tollgate_requests_total", **labels) == 1
    assert value(m, "tollgate_request_duration_seconds_count") == 1
    assert value(m, "tollgate_request_duration_seconds_sum") == 0.5


def test_label_values_outside_the_fixed_sets_are_other() -> None:
    m = metrics()
    made_up = Observation(model="gpt-9-turbo-max", provider="elsewhere", status="weird_error")
    m.observe_request(made_up, 0.0, 1.0, 0.0)
    m.observe_request(Observation(status=None), 0.0, 1.0, 0.0)  # nothing settled it
    m.observe_request(Observation(status="missing_api_key"), 0.0, 1.0, 0.0)
    other = {"status": "other", "model": "other", "provider": "other", "cache": "MISS"}
    assert value(m, "tollgate_requests_total", **other) == 1
    # Not known at all (turned away before the body was read) is "none".
    unread = {"status": "other", "model": "none", "provider": "none", "cache": "MISS"}
    assert value(m, "tollgate_requests_total", **unread) == 1
    unread["status"] = "missing_api_key"
    assert value(m, "tollgate_requests_total", **unread) == 1


def test_overhead_is_time_to_the_response_less_time_upstream() -> None:
    m = metrics()
    reached = Observation(status="success", response_started_at=10.3)
    m.observe_request(reached, started_at=10.0, ended_at=12.0, upstream_s=0.25)
    # Never reached a provider: no overhead to speak of.
    m.observe_request(Observation(status="invalid_api_key", response_started_at=10.1), 10, 11, 0)
    assert value(m, "tollgate_gateway_overhead_seconds_count") == 1
    assert abs(value(m, "tollgate_gateway_overhead_seconds_sum") - 0.05) < 1e-9  # type: ignore[operator]


def test_ttft_only_for_requests_with_a_first_token() -> None:
    m = metrics()
    m.observe_request(Observation(status="success", ttft_s=0.2), 0, 1, 0.1)
    m.observe_request(Observation(status="success"), 0, 1, 0.1)
    assert value(m, "tollgate_ttft_seconds_count") == 1


def test_rate_limit_rejections_by_limit() -> None:
    m = metrics()
    for outcome in ("requests", "requests", "tokens", "too_large", "something_new"):
        m.rate_limited(outcome)
    assert value(m, "tollgate_ratelimit_rejections_total", limit="rpm") == 2
    assert value(m, "tollgate_ratelimit_rejections_total", limit="tpm") == 1
    assert value(m, "tollgate_ratelimit_rejections_total", limit="request_too_large") == 1
    assert value(m, "tollgate_ratelimit_rejections_total", limit="other") == 1


def test_ledger_queue_depth_is_read_at_each_scrape() -> None:
    m, depth = metrics(), [3]
    m.watch_ledger(lambda: depth[0])
    assert value(m, "tollgate_ledger_queue_depth") == 3
    depth[0] = 0
    assert b"tollgate_ledger_queue_depth 0.0" in m.exposition()


def test_the_first_status_wins() -> None:
    o = Observation()
    o.settle("provider_error")  # the usage event
    o.settle("upstream_timeout")  # the error the client then got
    assert o.status == "provider_error"


def test_each_app_has_its_own_registry() -> None:
    first, second = metrics(), metrics()  # no "duplicated timeseries" error
    first.rate_limited("requests")
    assert value(second, "tollgate_ratelimit_rejections_total", limit="rpm") is None


@pytest.mark.perf
def test_recording_a_request_costs_microseconds() -> None:
    """Metrics sit on every request's path; they must stay far below the TTFT
    budget (SPEC B1b: p99 < 20 ms added). A guard, not a benchmark."""
    m, n = metrics(), 20_000
    started = time.perf_counter()
    for _ in range(n):
        o = Observation(model="gpt-4o-mini", provider="mock", ttft_s=0.01, response_started_at=1.1)
        o.settle("success")
        m.observe_request(o, 1.0, 1.5, 0.05)
    per_request = (time.perf_counter() - started) / n
    assert per_request < 100e-6, f"{per_request * 1e6:.1f} us per request"
