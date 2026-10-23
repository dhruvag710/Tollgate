import inspect
import time
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from tests.helpers import TEST_PROVIDERS
from tollgate.config import Settings
from tollgate.main import create_app
from tollgate.observability.timing import Timings


def _metrics(header: str) -> dict[str, float]:
    pairs = (item.strip().partition(";dur=") for item in header.split(","))
    return {name: float(duration) for name, _, duration in pairs}


def test_server_timing_accounts_for_all_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([10.0, 10.002, 10.010])  # auth runs 2 ms; the header is built at 10.010
    monkeypatch.setattr(time, "perf_counter", lambda: next(clock))
    timings = Timings(started_at=9.999)
    with timings.stage("auth"):
        pass
    timings.add("upstream-open", 0.007)
    metrics = _metrics(timings.server_timing())

    assert list(metrics) == ["auth", "upstream-open", "other", "gateway", "total"]
    assert metrics["auth"] == pytest.approx(2.0)
    assert metrics["total"] == pytest.approx(11.0)  # from arrival to the header
    assert metrics["gateway"] == pytest.approx(4.0)  # everything but upstream-*
    assert metrics["other"] == pytest.approx(2.0)  # covered by no stage


def test_stages_accumulate() -> None:
    timings = Timings(time.perf_counter())
    timings.add("auth", 0.001)
    timings.add("auth", 0.002)
    assert _metrics(timings.server_timing())["auth"] == pytest.approx(3.0)


def _api_routes(routes: list[Any]) -> list[APIRoute]:
    """Every APIRoute, including inside included routers (which FastAPI wraps)."""
    found = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            found.extend(_api_routes(inner.routes))
    return found


def _calls(dependant: Dependant) -> list[object]:
    calls = []
    for sub in dependant.dependencies:
        calls.append(sub.call)
        calls.extend(_calls(sub))
    return calls


def test_every_dependency_is_async() -> None:
    """FastAPI runs a plain `def` dependency in its thread pool: a thread round
    trip on every request, which cost the gateway more than its Redis call."""
    app = create_app(Settings(providers_config=TEST_PROVIDERS, key_pepper="p" * 16))
    routes = _api_routes(app.routes)
    assert {"/v1/chat/completions", "/v1/models", "/admin/keys/{key_id}"} <= {
        r.path for r in routes
    }
    for route in routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path
        for call in _calls(route.dependant):
            assert inspect.iscoroutinefunction(call), f"{route.path}: {call}"
