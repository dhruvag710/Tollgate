"""Test helpers shared across suites."""

import hashlib
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml
from alembic.config import Config
from sqlalchemy import select

from tollgate.config import Settings
from tollgate.db import tenants
from tollgate.db.models import UsageEventRow
from tollgate.db.session import create_engine, create_sessionmaker
from tollgate.observability.metrics import Metrics
from tollgate.routing.circuit_breaker import CLOSED, BreakerSuccess
from tollgate.routing.router import RoutePlan
from tollgate.usage.ledger import UsageEvent

ROOT = Path(__file__).resolve().parent.parent
TEST_PROVIDERS = ROOT / "config" / "providers.test.yaml"

# How long any HTTP call a test makes over a real socket may take. Well past
# httpx's 5 s default: a loaded machine (coverage, the whole suite, Docker
# Desktop) mustn't fail a test by being slow, while a hung server still fails
# it rather than stalling the run. Tests that time out on purpose set their
# own; the in-process ASGI transport ignores timeouts altogether.
HTTP_TIMEOUT = httpx.Timeout(60.0)

# Exported as KEY_PEPPER and ADMIN_TOKEN by tests/integration/conftest.py.
TEST_PEPPER = "integration-suite-pepper-not-a-secret"
TEST_ADMIN_TOKEN = "integration-suite-admin-token"
ADMIN = {"Authorization": f"Bearer {TEST_ADMIN_TOKEN}"}
# The key of the suite's shared tenant, seeded by tests/integration/conftest.py.
TEST_KEY = "tg_live_" + "IntegrationSuiteKey0123456789abcdefghijklmno"[:43]
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


def alembic_config() -> Config:
    """The migrations' config, for running them from the test process: without
    alembic.ini's logging, which would add a plain-text handler to the root
    logger and print every warning the suite logs a second time."""
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["configure_logger"] = False
    return config


async def issue_key(*, raw_key: str | None = None, **tenant_fields: Any) -> tenants.IssuedKey:
    """A new tenant (uniquely named unless `name` is given) and a key for it, in
    the test database."""
    tenant_fields.setdefault("name", f"tenant-{uuid.uuid4().hex[:12]}")
    engine = create_engine(Settings(providers_config=TEST_PROVIDERS))
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            tenant = await tenants.create_tenant(session, **tenant_fields)
            issued = await tenants.create_api_key(
                session, tenant.id, pepper=TEST_PEPPER, raw_key=raw_key
            )
    finally:
        await engine.dispose()
    assert issued is not None
    return issued


def in_flight_bound(tokens_per_sec: float) -> int:
    """Most completion tokens a stream at `tokens_per_sec` can have in flight when
    the gateway abandons it (SPEC C4): the one being sent, plus what the provider
    emits while the hang-up propagates, budgeted at 200 ms."""
    return 1 + math.ceil(tokens_per_sec * 0.2)


class MemoryLedger:
    """Ledger that keeps events in a list (safe to read from the test thread)."""

    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll `predicate` until it is true or `timeout` seconds pass."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@contextmanager
def serve(app: Any, **config: Any) -> Iterator[tuple[str, uvicorn.Server]]:
    """Serve an ASGI app on a real localhost socket in a background thread.

    Yields the base URL and the server; set `server.should_exit` to start a
    shutdown, as SIGTERM would.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on", **config))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server failed to start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@contextmanager
def run_server(app: Any) -> Iterator[str]:
    with serve(app) as (url, _):
        yield url


@contextmanager
def run_uvicorn_process(
    *args: str,
    env: dict[str, str] | None = None,
    host: str = "127.0.0.1",
    output: Path | None = None,
) -> Iterator[str]:
    """Run `python -m uvicorn <args>` as a separate process, as in deployment.
    It listens on `host`; the URL yielded is always the loopback one. With
    `output`, everything the process prints goes to that file."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sink = None if output is None else output.open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            *args,
            "--host",
            host,
            "--port",
            str(port),
            "--log-level",
            "warning",
            # As the Dockerfile sets it: longer than any client keeps a connection.
            "--timeout-keep-alive",
            "75",
        ],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
        stdout=sink,
        stderr=subprocess.STDOUT if sink is not None else None,
    )
    url = f"http://127.0.0.1:{port}"

    def healthy() -> bool:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn {args} exited with {proc.returncode}")
        try:
            return httpx.get(f"{url}/healthz", timeout=1).status_code == 200
        except httpx.TransportError:
            return False

    try:
        if not wait_until(healthy, timeout=60):
            raise RuntimeError(f"uvicorn {args} did not become healthy")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        if sink is not None:
            sink.close()


def write_providers(
    tmp_path: Path, base_url: str, *, single_route: bool = False, **provider_overrides: Any
) -> Path:
    """Copy of the test providers config with every base_url replaced.

    With `single_route`, every model routes to its first provider only. All the
    providers here share one upstream, so a failure a test injects into it fails
    over and fails again: a test of how a single provider's failure reaches the
    client needs the one route."""
    data = yaml.safe_load(TEST_PROVIDERS.read_text())
    for p in data["providers"]:
        p["base_url"] = base_url
        p.update(provider_overrides)
    if single_route:
        for model in data["models"].values():
            model["routes"] = model["routes"][:1]
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


class WordEmbedder:
    """A deterministic stand-in for the semantic cache's model: a bag of words,
    each word hashed to one of 384 dimensions, scaled to unit length. Questions
    sharing most of their words come out close: one word added to six scores
    0.93, one word of six changed ("Austria", "Australia") 0.83. `texts`
    records what it was asked to embed; set `fail` to make it raise."""

    dimensions = 384

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.fail = False

    def embed(self, text: str) -> list[float]:
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("the embedder is down")
        vector = [0.0] * self.dimensions
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.sha256(word.encode()).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]


class NoBreakers:
    """Breakers that are never consulted: a plan for one provider, always closed."""

    async def claim(self, provider: str, request_id: str) -> bool:
        raise AssertionError("a closed breaker has no probe to claim")

    def failed(self, provider: str, request_id: str) -> None:
        pass

    def probe_succeeded(self, provider: str, request_id: str) -> None:
        pass

    def release(self, provider: str, request_id: str) -> None:
        pass

    def success(self, provider: str) -> BreakerSuccess:
        return BreakerSuccess(provider, 10_000)


async def single_provider_plan(provider: Any, model: str = "gpt-4o-mini") -> RoutePlan:
    """A request's route through `provider` alone, its first attempt begun."""
    plan = RoutePlan(
        [(provider, CLOSED)],
        NoBreakers(),  # type: ignore[arg-type]
        Metrics([model], [provider.name]),
        "request",
        None,
    )
    assert await plan.next() is provider
    return plan


def assert_internal_error_response(r: httpx.Response, caplog: pytest.LogCaptureFixture) -> None:
    """An OpenAI-shaped 500 that gives nothing away, logged in full under the request id."""
    request_id = r.headers["x-request-id"]
    assert r.status_code == 500
    assert r.json() == {
        "error": {
            "message": f"Tollgate hit an internal error. Request ID: {request_id}",
            "type": "api_error",
            "param": None,
            "code": "internal_error",
        }
    }
    assert "bug" not in r.text
    assert "Traceback" not in r.text
    (record,) = [rec for rec in caplog.records if rec.name == "tollgate.errors"]
    assert request_id in record.getMessage()
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "bug"


async def ledger_rows(tenant_id: Any) -> list[UsageEventRow]:
    """The tenant's rows in the test database's ledger, oldest first."""
    engine = create_engine(Settings(providers_config=TEST_PROVIDERS))
    try:
        async with create_sessionmaker(engine)() as session:
            rows = await session.scalars(
                select(UsageEventRow)
                .where(UsageEventRow.tenant_id == tenant_id)
                .order_by(UsageEventRow.id)
            )
            return list(rows)
    finally:
        await engine.dispose()


async def redis_month(redis: Any) -> str:
    """'YYYYMM' (UTC) by Redis's clock, as the budget scripts see it."""
    seconds, _ = await redis.time()
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y%m")
