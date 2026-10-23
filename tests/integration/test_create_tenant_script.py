"""scripts/create_tenant.py against the suite's database."""

import re
import uuid

import httpx
import pytest

from scripts import create_tenant
from tests.helpers import TEST_PROVIDERS, run_server
from tollgate.auth import keys
from tollgate.config import Settings
from tollgate.main import create_app


def test_script_issues_a_working_key(capsys: pytest.CaptureFixture[str]) -> None:
    name = f"script-{uuid.uuid4().hex[:8]}"
    argv = ["--name", name, "--rpm", "120", "--budget", "25", "--models", "gpt-4o-mini"]
    assert create_tenant.main(argv) == 0
    out, err = capsys.readouterr()
    match = re.search(r"api_key:\s+(\S+)", out)
    assert match is not None
    raw_key = match.group(1)
    assert keys.is_well_formed(raw_key)
    assert "not shown again" in err

    with run_server(create_app(Settings(providers_config=TEST_PROVIDERS))) as url:
        r = httpx.get(f"{url}/v1/models", headers={"Authorization": f"Bearer {raw_key}"})
    assert r.status_code == 200
    assert [m["id"] for m in r.json()["data"]] == ["gpt-4o-mini"]


def test_script_refuses_a_taken_name(capsys: pytest.CaptureFixture[str]) -> None:
    name = f"script-{uuid.uuid4().hex[:8]}"
    assert create_tenant.main(["--name", name]) == 0
    assert create_tenant.main(["--name", name]) == 1
    assert "already exists" in capsys.readouterr().err


def test_script_refuses_unknown_models() -> None:
    with pytest.raises(SystemExit, match="gpt-9"):
        create_tenant.main(["--name", f"script-{uuid.uuid4().hex[:8]}", "--models", "gpt-9"])
