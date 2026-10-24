"""A gateway's first burst: its Redis connections were opened at startup, so 500
requests arriving at once all get through, none turned away with a 503."""

from collections import Counter
from pathlib import Path

from tests.concurrency.burst import burst
from tests.helpers import issue_key, run_uvicorn_process, write_providers


async def test_a_cold_gateway_takes_a_burst_of_500(tmp_path: Path) -> None:
    issued = await issue_key(rpm_limit=1_000_000, tpm_limit=1_000_000_000)
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    with run_uvicorn_process("mock_provider.app:app") as mock_url:
        env = {
            "PROVIDERS_CONFIG": str(write_providers(tmp_path, f"{mock_url}/v1")),
            "LOG_LEVEL": "WARNING",
        }
        # Freshly started, as deployed: nothing has used its Redis pool but the
        # two requests that cache the key.
        with run_uvicorn_process("--factory", "tollgate.main:create_app", env=env) as url:
            replies, span = await burst(url, issued.raw_key, 500, body)
    statuses = Counter(r.status_code for r in replies)
    errors = Counter(r.json()["error"]["code"] for r in replies if r.status_code != 200)
    assert statuses == {200: 500}, f"{dict(statuses)} over {span:.3f}s: {dict(errors)}"
