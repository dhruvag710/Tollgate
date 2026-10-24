import pytest


@pytest.fixture(autouse=True)
def _backing_services(backing_services: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here runs against real Postgres and Redis (tests/conftest.py).
    The gateways started here by the dozen don't open a Redis pool at startup
    (the concurrency tests, which burst, keep the default)."""
    monkeypatch.setenv("REDIS_MIN_CONNECTIONS", "0")
