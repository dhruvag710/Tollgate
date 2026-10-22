"""Test helpers shared across suites."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEST_PROVIDERS = ROOT / "config" / "providers.test.yaml"


@contextmanager
def run_server(app: Any) -> Iterator[str]:
    """Serve an ASGI app on a real localhost socket in a background thread."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server failed to start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def write_providers(tmp_path: Path, base_url: str, **provider_overrides: Any) -> Path:
    """Copy of the test providers config with every base_url replaced."""
    data = yaml.safe_load(TEST_PROVIDERS.read_text())
    for p in data["providers"]:
        p["base_url"] = base_url
        p.update(provider_overrides)
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(data))
    return path
