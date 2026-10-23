"""Test helpers shared across suites."""

import math
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
import yaml

from tollgate.usage.ledger import UsageEvent

ROOT = Path(__file__).resolve().parent.parent
TEST_PROVIDERS = ROOT / "config" / "providers.test.yaml"


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
def run_uvicorn_process(*args: str, env: dict[str, str] | None = None) -> Iterator[str]:
    """Run `python -m uvicorn <args>` as a separate process, as in deployment."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            *args,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env={**os.environ, **(env or {})},
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


def write_providers(tmp_path: Path, base_url: str, **provider_overrides: Any) -> Path:
    """Copy of the test providers config with every base_url replaced."""
    data = yaml.safe_load(TEST_PROVIDERS.read_text())
    for p in data["providers"]:
        p["base_url"] = base_url
        p.update(provider_overrides)
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


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
