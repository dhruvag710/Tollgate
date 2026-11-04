import pytest


@pytest.fixture(autouse=True)
def _backing_services(backing_services: None, closed_breakers: None) -> None:
    """Every test here runs against real Postgres and Redis (tests/conftest.py),
    with every circuit breaker closed."""
