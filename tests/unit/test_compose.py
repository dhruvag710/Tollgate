"""The compose stack's healthchecks: cheap to run, and right about health."""

import re
import subprocess
import sys
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi import FastAPI, Response

from tests.helpers import ROOT, run_server


def healthchecks() -> dict[str, list[str]]:
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    return {name: s["healthcheck"]["test"] for name, s in services.items() if "healthcheck" in s}


def test_no_healthcheck_starts_a_python_interpreter() -> None:
    # A check runs inside its container's CPU limit every few seconds. Starting
    # Python for each one cost the gateway a share of its single CPU and got it
    # throttled by the CFS quota (BENCHMARKS.md, healthcheck fix).
    checks = healthchecks()
    assert {"gateway", "mock_provider", "mock_provider_secondary"} <= checks.keys()
    for name, test in checks.items():
        assert "python" not in " ".join(test).lower(), name


@pytest.mark.skipif(sys.platform != "linux", reason="runs the checks with a Linux bash (/dev/tcp)")
@pytest.mark.parametrize("service", ["gateway", "mock_provider", "mock_provider_secondary"])
def test_the_http_check_passes_on_200_and_fails_otherwise(service: str) -> None:
    healthy = True
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> Response:
        return Response(status_code=200 if healthy else 503)

    kind, shell, flag, script = healthchecks()[service]
    assert (kind, shell, flag) == ("CMD", "bash", "-c")
    with run_server(app) as url:
        script = re.sub(r"/dev/tcp/127\.0\.0\.1/\d+", f"/dev/tcp/127.0.0.1/{urlsplit(url).port}",
                        script.replace("$$", "$"))  # fmt: skip  # $$ is compose's escape for $
        assert subprocess.run(["bash", "-c", script], timeout=10).returncode == 0
        healthy = False
        assert subprocess.run(["bash", "-c", script], timeout=10).returncode != 0
    # Nothing listening at all.
    assert subprocess.run(["bash", "-c", script], timeout=10, stderr=subprocess.DEVNULL).returncode
